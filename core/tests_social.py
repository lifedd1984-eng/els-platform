"""소셜 로그인(카카오) — 스위치와 계정 자동 연결 방어를 못 박는다.

여기서 지키려는 것
  ① 키가 없으면 아무 일도 안 일어난다
     .env 에 KAKAO_CLIENT_ID 가 없는 상태(= 지금 운영 상태이자 로컬 기본값)에서
     화면에 버튼이 안 뜨고, 주소를 직접 쳐도 에러 화면이 아니라 로그인 화면으로
     되돌아온다. 키를 못 받은 채 배포되는 일이 생겨도 서비스가 멀쩡해야 한다.
  ② 키가 있으면 버튼이 뜨고, 실제로 카카오로 넘어간다
  ③ ⚠ 검증되지 않은 이메일로는 기존 계정에 붙지 않는다
     이게 계정 탈취 방어의 핵심이다. 카카오 프로필의 이메일란은 누구나 남의
     주소를 적어 넣을 수 있으므로, '이메일이 같다'만으로 붙이면 그대로 남의
     계정 문이 열린다. 검증된 이메일만 붙고, 검증 안 된 이메일은 안내 화면으로
     간다(core/socialauth.py).
  ④ 기존 아이디·비밀번호 로그인이 그대로 돈다
     소셜 로그인 도입 이전부터 있던 계정이 깨지면 안 된다. 비밀번호가
     조용히 사라지지 않는 것까지 함께 본다(core/migrations/0031 참조).
  ⑤ 사이트맵이 그대로다
     allauth 를 붙이면서 django.contrib.sites 를 일부러 넣지 않았다. 넣는 순간
     sitemap 뷰가 Site 테이블을 보게 되어 도메인이 example.com 으로 나가거나
     사이트맵이 깨진다. 그 결정이 유지되는지 여기서 막는다.

테스트가 실제 카카오 서버를 부르지 않는 이유
  아래 계정 연결 테스트는 '카카오에서 프로필을 받아 온 직후'부터의 흐름만
  태운다(run_kakao_login). 토큰 교환은 카카오 쪽 일이라 우리가 검증할 것이
  없고, 우리가 판단하는 부분은 전부 그 다음에 일어난다.
"""

from importlib import import_module
from types import SimpleNamespace
from urllib.parse import urlparse
from xml.etree import ElementTree

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.socialaccount.adapter import get_adapter as get_social_adapter
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount

from core.sitemaps import SITEMAPS

User = get_user_model()

SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# settings.py 는 .env 의 KAKAO_CLIENT_ID 유무만 보고 아래 세 값을 만든다.
# 테스트에서는 .env 에 무엇이 들어 있든 상관없도록 결과값을 통째로 갈아 끼운다.
# (조 팀장이 나중에 로컬 .env 에 실제 키를 넣어도 이 테스트들은 그대로 돈다)
KEYS_PRESENT = dict(
    SOCIAL_PROVIDERS={"kakao": {"label": "카카오", "enabled": True}},
    SOCIAL_LOGIN_ENABLED=True,
    SOCIALACCOUNT_PROVIDERS={
        "kakao": {
            "APPS": [{
                "client_id": "test-client-id",
                "secret": "test-secret",
                "key": "",
            }],
            "SCOPE": ["account_email", "profile_nickname"],
            "EMAIL_AUTHENTICATION": True,
        },
    },
)

# 키를 못 받은 상태. settings.py 가 만드는 모양 그대로 — APPS 키 자체가 없다.
KEYS_ABSENT = dict(
    SOCIAL_PROVIDERS={"kakao": {"label": "카카오", "enabled": False}},
    SOCIAL_LOGIN_ENABLED=False,
    SOCIALACCOUNT_PROVIDERS={
        "kakao": {
            "SCOPE": ["account_email", "profile_nickname"],
            "EMAIL_AUTHENTICATION": True,
        },
    },
)

KAKAO_BUTTON_LABEL = "카카오 로그인"


def kakao_payload(uid="1000001", email=None, verified=None, nickname="레이더"):
    """카카오 /v2/user/me 응답의 우리가 쓰는 부분만 흉내 낸다.

    email 을 None 으로 두면 '이메일 동의를 안 한 사용자'가 된다.
    verified 는 kakao_account.is_email_verified 로 그대로 실린다 —
    allauth 의 kakao provider 가 이 값을 EmailAddress.verified 에 옮긴다.
    """
    account = {"profile": {"nickname": nickname}}
    if email is not None:
        account["email"] = email
        account["is_email_verified"] = verified
    return {"id": uid, "kakao_account": account}


def run_kakao_login(payload):
    """카카오에서 프로필을 받은 직후의 allauth 흐름을 태운다.

    실제 콜백 뷰가 하는 일과 같은 순서다 — provider 로 SocialLogin 을 만들고
    complete_social_login 에 넘긴다. 그 안에서 lookup(기존 계정 대조) →
    우리 어댑터의 pre_social_login → 로그인 또는 가입이 일어난다.

    돌려주는 request 의 user 를 보면 누구로 로그인됐는지 알 수 있다
    (django.contrib.auth.login 이 request.user 를 채운다).
    """
    request = RequestFactory().get("/accounts/kakao/login/callback/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    MessageMiddleware(lambda r: None).process_request(request)
    request.user = AnonymousUser()
    # allauth.account.middleware.AccountMiddleware 가 해 주는 두 가지.
    # 요청을 직접 만들었으므로 여기서 대신 세워 준다.
    request.allauth = SimpleNamespace()
    with context.request_context(request):
        provider = get_social_adapter().get_provider(request, "kakao")
        sociallogin = provider.sociallogin_from_response(request, payload)
        response = complete_social_login(request, sociallogin)
    return request, response


# ── ① 키가 없을 때 ────────────────────────────────
@override_settings(**KEYS_ABSENT)
class NoKeysTests(TestCase):
    """키를 못 받은 상태에서 소셜 로그인이 통째로 숨는지."""

    def test_login_page_has_no_kakao_button(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn(KAKAO_BUTTON_LABEL, body)
        # 구분선('또는')도 함께 사라져야 한다 — 버튼 없이 선만 남으면 흉하다.
        self.assertNotIn("social-sep", body)

    def test_signup_page_has_no_kakao_button(self):
        resp = self.client.get(reverse("signup"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(KAKAO_BUTTON_LABEL, resp.content.decode())

    def test_kakao_login_url_returns_to_login_page(self):
        """주소를 직접 쳐도 에러 화면이 아니라 로그인 화면으로 되돌린다.

        키가 없으면 allauth 는 소셜 앱을 못 찾아 예외를 던진다. 그 앞에
        core.views.kakao_login_entry 가 서 있다.
        """
        resp = self.client.get("/accounts/kakao/login/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("login"))

    def test_kakao_login_url_shows_guidance_message(self):
        resp = self.client.get("/accounts/kakao/login/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "카카오 로그인은 현재 준비 중입니다")

    def test_kakao_callback_url_returns_to_login_page(self):
        """콜백도 같다 — 키를 지운 뒤 옛 링크로 돌아오는 경우가 있다."""
        resp = self.client.get("/accounts/kakao/login/callback/?code=dummy")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("login"))

    def test_kakao_login_post_also_blocked(self):
        """버튼이 POST 폼이므로 POST 경로도 함께 막혀 있어야 한다."""
        resp = self.client.post("/accounts/kakao/login/", {"process": "login"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("login"))

    def test_password_change_page_hides_connection_link(self):
        """'계정 연결 관리' 링크도 키가 없으면 나오지 않는다."""
        User.objects.create_user(username="plain", password="pw-abcd-1234")
        self.client.login(username="plain", password="pw-abcd-1234")
        resp = self.client.get(reverse("password_change"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("계정 연결 관리", resp.content.decode())


# ── ② 키가 있을 때 ────────────────────────────────
@override_settings(**KEYS_PRESENT)
class WithKeysTests(TestCase):
    """키를 넣으면 버튼이 뜨고 실제로 카카오로 넘어가는지."""

    def test_login_page_shows_kakao_button(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(KAKAO_BUTTON_LABEL, body)
        # 버튼은 반드시 POST 폼이다. GET 한 줄로 로그인이 시작되면
        # 남이 만든 링크를 누르는 것만으로 흐름이 시작된다.
        self.assertIn('action="/accounts/kakao/login/"', body)
        self.assertIn('method="post"', body)
        self.assertIn("csrfmiddlewaretoken", body)

    def test_signup_page_shows_kakao_button(self):
        resp = self.client.get(reverse("signup"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(KAKAO_BUTTON_LABEL, resp.content.decode())

    def test_kakao_login_get_does_not_start_the_flow(self):
        """GET 은 확인 화면까지만 — 로그인 화면으로 튕기지도, 바로 넘어가지도 않는다."""
        resp = self.client.get("/accounts/kakao/login/")
        self.assertEqual(resp.status_code, 200)

    def test_kakao_login_post_redirects_to_kakao(self):
        """POST 하면 카카오 인증 주소로 넘어간다 — 키가 실제로 쓰인다는 증거."""
        resp = self.client.post("/accounts/kakao/login/", {"process": "login"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            resp["Location"].startswith("https://kauth.kakao.com/oauth/authorize"),
            f"카카오로 가지 않았다: {resp['Location']}")
        self.assertIn("client_id=test-client-id", resp["Location"])

    def test_password_change_page_shows_connection_link(self):
        User.objects.create_user(username="plain", password="pw-abcd-1234")
        self.client.login(username="plain", password="pw-abcd-1234")
        resp = self.client.get(reverse("password_change"))
        self.assertIn("계정 연결 관리", resp.content.decode())


# ── ③ 계정 자동 연결 (핵심) ────────────────────────
@override_settings(**KEYS_PRESENT)
class AutoConnectTests(TestCase):
    """검증된 이메일만 기존 계정에 붙는다."""

    def setUp(self):
        # 소셜 로그인 이전부터 있던 계정. 마이그레이션 0031 이 EmailAddress 를
        # 심어 둔 상태를 그대로 재현한다.
        self.oldie = User.objects.create_user(
            username="oldie", email="oldie@example.com", password="pw-abcd-1234")
        EmailAddress.objects.create(
            user=self.oldie, email="oldie@example.com",
            verified=True, primary=True)

    def test_verified_email_connects_to_existing_account(self):
        """정상 경로 — 카카오가 검증한 이메일이 기존 계정과 같으면 그 계정으로 들어간다."""
        request, _ = run_kakao_login(kakao_payload(
            email="oldie@example.com", verified=True))

        self.assertTrue(request.user.is_authenticated)
        self.assertEqual(request.user.pk, self.oldie.pk)
        # 계정이 새로 생기지 않았다
        self.assertEqual(User.objects.count(), 1)
        # 다음 로그인부터는 이메일이 바뀌어도 이어지도록 실제로 붙여 둔다
        self.assertTrue(SocialAccount.objects.filter(
            user=self.oldie, provider="kakao", uid="1000001").exists())

    def test_unverified_email_does_not_connect(self):
        """⚠ 계정 탈취 방어 — 검증 안 된 이메일은 기존 계정에 붙지 않는다.

        카카오 프로필의 이메일란에 남의 주소를 적어 두는 것만으로 그 사람의
        계정에 들어갈 수 있으면 안 된다. 이 테스트가 깨지면 그 문이 열린 것이다.
        """
        request, response = run_kakao_login(kakao_payload(
            email="oldie@example.com", verified=False))

        # 로그인되지 않았다
        self.assertFalse(request.user.is_authenticated)
        # 소셜 계정이 붙지도 않았다
        self.assertFalse(SocialAccount.objects.filter(user=self.oldie).exists())
        # 같은 이메일로 계정이 하나 더 생기지도 않았다
        self.assertEqual(User.objects.count(), 1)
        # 안내 화면으로 보냈다
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('social_connect_help')}?reason=unverified")

    def test_unverified_email_of_a_stranger_is_left_alone(self):
        """기존 계정과 겹치지 않는 미검증 이메일까지 막지는 않는다.

        겹치지 않으면 탈취할 대상이 없다. 여기까지 막으면 카카오에서 이메일
        인증을 안 한 사람은 가입 자체를 못 한다.
        """
        request, _ = run_kakao_login(kakao_payload(
            uid="2000002", email="newcomer@example.com", verified=False))

        self.assertTrue(request.user.is_authenticated)
        self.assertEqual(request.user.email, "newcomer@example.com")
        self.assertNotEqual(request.user.pk, self.oldie.pk)

    def test_missing_email_goes_to_help_page(self):
        """이메일을 아예 못 받으면 기존 회원인지 판별할 수 없다 → 안내 화면."""
        request, response = run_kakao_login(kakao_payload(uid="3000003"))

        self.assertFalse(request.user.is_authenticated)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('social_connect_help')}?reason=no-email")

    def test_second_login_reuses_the_connected_account(self):
        """한 번 붙은 뒤에는 uid 로 바로 그 계정에 들어간다."""
        run_kakao_login(kakao_payload(email="oldie@example.com", verified=True))
        request, _ = run_kakao_login(kakao_payload(
            email="oldie@example.com", verified=True))

        self.assertEqual(request.user.pk, self.oldie.pk)
        self.assertEqual(SocialAccount.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)


# ── ④ 안내 화면 ──────────────────────────────────
class SocialConnectHelpTests(TestCase):
    """자동 연결을 멈췄을 때 보여 주는 화면. 키 없이도 열려야 한다 —
    이미 안내 화면에 와 있는 사람을 또 튕겨 내면 갈 곳이 없어진다."""

    @override_settings(**KEYS_ABSENT)
    def test_help_page_opens_without_keys(self):
        resp = self.client.get(reverse("social_connect_help"))
        self.assertEqual(resp.status_code, 200)

    @override_settings(**KEYS_PRESENT)
    def test_unverified_reason_explains_the_block(self):
        resp = self.client.get(reverse("social_connect_help"),
                               {"reason": "unverified"})
        self.assertContains(resp, "카카오에서 확인되지 않은 주소")

    @override_settings(**KEYS_PRESENT)
    def test_no_email_reason_explains_the_block(self):
        resp = self.client.get(reverse("social_connect_help"),
                               {"reason": "no-email"})
        self.assertContains(resp, "이메일을 받지 못했습니다")

    @override_settings(**KEYS_PRESENT)
    def test_unknown_reason_falls_back(self):
        """주소창에 아무 값이나 넣어도 화면이 깨지지 않는다."""
        resp = self.client.get(reverse("social_connect_help"),
                               {"reason": "<script>x</script>"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "카카오에서 확인되지 않은 주소")


# ── ⑤ 기존 아이디·비밀번호 로그인 ─────────────────
class ExistingLoginUnaffectedTests(TestCase):
    """소셜 로그인을 붙였다고 기존 계정이 깨지면 안 된다."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="oldie", email="oldie@example.com", password="pw-abcd-1234")
        EmailAddress.objects.create(
            user=self.user, email="oldie@example.com",
            verified=True, primary=True)

    @override_settings(**KEYS_ABSENT)
    def test_password_login_works_without_keys(self):
        resp = self.client.post(reverse("login"), {
            "username": "oldie", "password": "pw-abcd-1234"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/weekly/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    @override_settings(**KEYS_PRESENT)
    def test_password_login_works_with_keys(self):
        """AUTHENTICATION_BACKENDS 에 allauth 백엔드가 끼어들어도 그대로다."""
        resp = self.client.post(reverse("login"), {
            "username": "oldie", "password": "pw-abcd-1234"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    @override_settings(**KEYS_PRESENT)
    def test_wrong_password_still_rejected(self):
        resp = self.client.post(reverse("login"), {
            "username": "oldie", "password": "wrong-password"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(**KEYS_PRESENT)
    def test_password_survives_kakao_login(self):
        """카카오로 한 번 들어와도 비밀번호가 사라지지 않는다.

        allauth 는 검증된 이메일로 기존 계정에 붙일 때, 그 계정의 이메일이
        우리 쪽에서도 검증돼 있지 않으면 비밀번호를 사용 불가로 만든다
        (wipe_password). 마이그레이션 0031 이 기존 계정의 이메일을 미리
        검증됨으로 등록해 두는 이유가 이것이다 — setUp 의 EmailAddress 가
        그 상태를 재현한다.
        """
        run_kakao_login(kakao_payload(email="oldie@example.com", verified=True))

        self.user.refresh_from_db()
        self.assertTrue(self.user.has_usable_password())
        self.assertTrue(self.user.check_password("pw-abcd-1234"))

        self.client.logout()
        resp = self.client.post(reverse("login"), {
            "username": "oldie", "password": "pw-abcd-1234"})
        self.assertEqual(resp.status_code, 302)


class Migration0031Tests(TestCase):
    """기존 계정의 이메일을 '검증됨'으로 심는 마이그레이션.

    실제 운영 DB에는 계정 5개가 이미 있고, 마이그레이션은 그 위에서 돈다.
    테스트 DB는 마이그레이션 시점에 계정이 없으므로 아무 일도 하지 않는다 —
    그래서 여기서는 마이그레이션이 하는 일(forwards)을 직접 불러 확인한다.
    """

    # 모듈명이 숫자로 시작해 import 문으로는 못 불러온다.
    MODULE = "core.migrations.0031_seed_emailaddress_for_existing_users"

    def _run(self, func):
        # apps.get_model 만 쓰므로 실제 앱 레지스트리를 그대로 넘길 수 있다.
        func(django_apps, None)

    def test_forwards_marks_existing_emails_verified(self):
        mod = import_module(self.MODULE)

        user = User.objects.create_user(
            username="oldie", email="oldie@example.com", password="pw-abcd-1234")
        no_email = User.objects.create_user(
            username="nomail", password="pw-abcd-1234")

        self._run(mod.forwards)

        row = EmailAddress.objects.get(user=user)
        self.assertEqual(row.email, "oldie@example.com")
        self.assertTrue(row.verified)
        self.assertTrue(row.primary)
        # 이메일이 없는 계정은 건드리지 않는다
        self.assertFalse(EmailAddress.objects.filter(user=no_email).exists())

    def test_forwards_is_idempotent(self):
        """두 번 돌아도 행이 늘지 않는다 — 재배포 때 다시 도는 경우 대비."""
        mod = import_module(self.MODULE)

        User.objects.create_user(
            username="oldie", email="oldie@example.com", password="pw-abcd-1234")
        self._run(mod.forwards)
        self._run(mod.forwards)
        self.assertEqual(EmailAddress.objects.count(), 1)


# ── ⑥ 사이트맵 ───────────────────────────────────
@override_settings(**KEYS_PRESENT)
class SitemapStillIntactTests(TestCase):
    """allauth 를 붙여도 사이트맵이 그대로인지.

    django.contrib.sites 를 넣지 않은 것이 여기서 지키려는 결정이다.
    넣으면 sitemap 뷰가 RequestSite 폴백을 그만두고 Site 테이블을 보게 되어,
    도메인이 example.com 으로 나가거나 Site.DoesNotExist 로 깨진다.
    """

    def locs(self):
        resp = self.client.get("/sitemap.xml")
        root = ElementTree.fromstring(resp.content)
        return resp, [e.text for e in root.iter(f"{SM_NS}loc")]

    def test_sites_framework_is_not_installed(self):
        from django.conf import settings
        self.assertNotIn("django.contrib.sites", settings.INSTALLED_APPS)

    def test_sitemap_is_still_200(self):
        resp, _ = self.locs()
        self.assertEqual(resp.status_code, 200)

    def test_sitemap_urls_use_the_request_host(self):
        """도메인이 요청 Host 그대로여야 한다. sites 를 넣으면 example.com 이 된다.

        프로토콜(https)은 Django 기본값이라 여기서 따지지 않는다 — 보는 것은
        호스트가 요청에서 왔는지(RequestSite) 뿐이다.
        """
        _, locs = self.locs()
        self.assertTrue(locs)
        for loc in locs:
            self.assertEqual(urlparse(loc).netloc, "testserver",
                             f"요청 호스트가 아닌 도메인이 나왔다: {loc}")

    def test_sitemap_url_count_matches_the_registry(self):
        """URL 수가 sitemaps.py 가 내놓는 항목 수와 정확히 같다.

        allauth 가 URL 을 얹으면서 사이트맵에 새 항목이 끼어들거나
        기존 항목이 빠지지 않았는지 본다.
        """
        _, locs = self.locs()
        expected = sum(len(sm().items()) for sm in SITEMAPS.values())
        self.assertEqual(len(locs), expected)

    def test_account_urls_are_not_in_the_sitemap(self):
        """로그인·소셜 관련 주소는 색인될 이유가 없다."""
        _, locs = self.locs()
        for loc in locs:
            self.assertNotIn("/accounts/", loc)


@override_settings(**KEYS_PRESENT)
class AdapterIsWhatBlocksItTests(TestCase):
    """막고 있는 것이 정말 우리 어댑터인지 확인한다.

    core/socialauth.py 를 빼고 allauth 기본 어댑터로 돌리면 흐름이 어디로
    가는지 대조해 둔다. 위의 '미검증 이메일은 붙지 않는다' 테스트가 우연히
    통과하는 것이 아니라 이 파일 덕분이라는 것을 못 박는 대조군이다.
    """

    def setUp(self):
        self.oldie = User.objects.create_user(
            username="oldie", email="oldie@example.com", password="pw-abcd-1234")
        EmailAddress.objects.create(
            user=self.oldie, email="oldie@example.com",
            verified=True, primary=True)

    @override_settings(
        SOCIALACCOUNT_ADAPTER="allauth.socialaccount.adapter.DefaultSocialAccountAdapter")
    def test_default_adapter_does_not_send_to_the_help_page(self):
        _, response = run_kakao_login(kakao_payload(
            email="oldie@example.com", verified=False))
        help_url = reverse("social_connect_help")
        location = response.get("Location", "") if response.status_code == 302 else ""
        self.assertNotIn(help_url, location,
                         "기본 어댑터가 안내 화면으로 보냈다면 위 테스트는 우리 코드를 검증하지 못한다")


class PrivacyPolicyMentionsKakaoTests(TestCase):
    """카카오에서 받는 항목이 개인정보처리방침에 적혀 있는지.

    수집 항목이 늘었는데 방침이 그대로면 그 자체가 문제다. 코드만 고치고
    문서를 잊는 일이 흔해서 여기서 함께 묶어 둔다.
    """

    def test_collected_items_are_listed(self):
        resp = self.client.get(reverse("privacy"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        for item in ("카카오 로그인", "이메일", "닉네임", "카카오 회원번호"):
            self.assertIn(item, body, f"개인정보처리방침에 '{item}' 이 없다")

    def test_token_storage_is_stated(self):
        """토큰을 저장하지 않는다는 설명과 실제 설정이 어긋나면 안 된다."""
        from django.conf import settings
        self.assertFalse(settings.SOCIALACCOUNT_STORE_TOKENS)
        resp = self.client.get(reverse("privacy"))
        self.assertContains(resp, "접근 토큰은 저장하지 않습니다")
