"""베타 피드백 배너 + 의견 수집 폼.

배경 (2026-08-13, 사용자확보 기획안 D5)
  지인 초대 경로가 막히면서 콘텐츠·커뮤니티로 들어오는 소수를 놓치지 않는 게
  더 중요해졌다. 원래 W2 예정이던 피드백 배너를 앞당겨 붙인다.

여기서 지키려는 것
  ① 로그인한 사람에게 주간 청약(로그인 직후 도착 화면) 상단에 배너가 뜬다
  ② 닫으면 **다시 뜨지 않는다** — 세션이 아니라 계정에 기록하므로
     로그아웃/다른 기기에서도 그대로다 (매번 뜨는 배너가 배너 없는 것보다 나쁘다)
  ③ 이미 의견을 보낸 사람에게는 애초에 안 뜬다
  ④ 폼은 본문 하나만 필수 — 연락처는 선택, 인터뷰 의향은 체크박스 하나
  ⑤ 제출되면 텔레그램으로 본문과 인터뷰 의향이 간다 (기본값 켜짐)
  ⑥ 텔레그램에는 연락처 **원문을 싣지 않는다** — 공용 채널이라 다른 사람도 본다
  ⑦ 비로그인은 폼도 닫기도 막힌다
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    _RADAR_POOL_CACHE, Feedback, FeedbackBannerDismissal, Product,
)

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())

BANNER_MARK = 'id="fbBanner"'


class _Base(TestCase):
    """시세 조회를 끊고 배지 캐시를 비운다 (tests_weekly_params와 같은 이유)."""

    def setUp(self):
        _RADAR_POOL_CACHE.clear()
        patcher = mock.patch("core.market.resolve_ticker", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_RADAR_POOL_CACHE.clear)

        self.user = get_user_model().objects.create_user(
            username="beta1", password="pw-test-1234", email="beta1@example.com")
        Product.objects.create(
            issuer="키움증권", product_no="9001", name="키움 ELS", product_type="ELS",
            yield_rate=12.0, ki=25, is_no_ki=False, barrier_first=85, barrier_last=65,
            assets_raw="KOSPI200 Index", asset_type="지수형", sub_end=TODAY,
            currency="KRW", issue_date=MONDAY + timedelta(days=3), period_months=6,
        )

    def weekly_html(self):
        r = self.client.get(reverse("weekly"))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()


class 배너노출(_Base):

    def test_로그인하면_주간청약에_배너가_뜬다(self):
        self.client.force_login(self.user)
        html = self.weekly_html()
        self.assertIn(BANNER_MARK, html)
        self.assertIn("커피 기프티콘", html)
        self.assertIn(reverse("feedback"), html)

    def test_비로그인에게는_안_뜬다(self):
        """가입도 안 한 사람에게 '쓰면서 불편한 점'을 물을 게 없다."""
        self.assertNotIn(BANNER_MARK, self.weekly_html())

    def test_배너는_주간청약에만_둔다(self):
        """도배 방지 — 다른 화면에는 배너를 넣지 않는다(푸터 상시 링크는 별개)."""
        self.client.force_login(self.user)
        for name in ("portfolio", "watchlist", "calendar"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn(BANNER_MARK, html, f"{name}에 배너가 새어 들어갔다")

    def test_닫은_뒤에도_푸터_링크는_남는다(self):
        """재노출을 안 하는 대신 상시 경로를 열어둔다."""
        self.client.force_login(self.user)
        self.client.post(reverse("feedback_dismiss"))
        html = self.weekly_html()
        self.assertNotIn(BANNER_MARK, html)
        self.assertIn("의견 보내기", html)          # 푸터 링크


class 배너닫기(_Base):

    def test_닫으면_다시_뜨지_않는다(self):
        self.client.force_login(self.user)
        self.assertIn(BANNER_MARK, self.weekly_html())

        r = self.client.post(reverse("feedback_dismiss"), {"next": "/weekly/?w=0"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/weekly/?w=0")

        self.assertNotIn(BANNER_MARK, self.weekly_html())

    def test_로그아웃하고_다시_들어와도_안_뜬다(self):
        """세션이 아니라 계정에 기록해야 '닫기가 안 먹는 배너'가 되지 않는다."""
        self.client.force_login(self.user)
        self.client.post(reverse("feedback_dismiss"))
        self.client.logout()
        self.client.force_login(self.user)
        self.assertNotIn(BANNER_MARK, self.weekly_html())

    def test_기록은_계정당_한_줄(self):
        self.client.force_login(self.user)
        self.client.post(reverse("feedback_dismiss"))
        self.client.post(reverse("feedback_dismiss"))
        self.assertEqual(FeedbackBannerDismissal.objects.filter(user=self.user).count(), 1)

    def test_fetch로_닫으면_JSON만_준다(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("feedback_dismiss"),
                             headers={"x-requested-with": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"dismissed": True})

    def test_next가_외부주소면_무시한다(self):
        """열린 리다이렉트 방지 — 사용자가 보낸 값을 그대로 믿지 않는다."""
        self.client.force_login(self.user)
        r = self.client.post(reverse("feedback_dismiss"), {"next": "https://evil.example/"})
        self.assertEqual(r["Location"], reverse("weekly"))

    def test_GET으로는_닫히지_않는다(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("feedback_dismiss"))
        self.assertEqual(r.status_code, 405)
        self.assertFalse(FeedbackBannerDismissal.objects.exists())

    def test_비로그인은_닫을_수_없다(self):
        r = self.client.post(reverse("feedback_dismiss"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])
        self.assertFalse(FeedbackBannerDismissal.objects.exists())


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c")
class 의견제출(_Base):
    """⚠ 텔레그램 실발송 금지 — send_message를 전부 모킹한다."""

    def setUp(self):
        super().setUp()
        self.send = mock.patch("core.telegram.send_message", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.client.force_login(self.user)

    def post(self, **kw):
        data = {"body": "낙인이 무슨 뜻인지 모르겠어요"}
        data.update(kw)
        return self.client.post(reverse("feedback"), data)

    def test_폼이_열린다(self):
        r = self.client.get(reverse("feedback"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("의견 보내기", html)
        self.assertIn('name="body"', html)
        self.assertIn('name="contact"', html)
        self.assertIn('name="interview_ok"', html)

    def test_저장된다(self):
        r = self.post(contact="010-0000-0000", interview_ok="1")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("feedback") + "?sent=1")
        fb = Feedback.objects.get()
        self.assertEqual(fb.user, self.user)
        self.assertEqual(fb.body, "낙인이 무슨 뜻인지 모르겠어요")
        self.assertEqual(fb.contact, "010-0000-0000")
        self.assertTrue(fb.interview_ok)

    def test_연락처와_인터뷰는_선택이다(self):
        self.post()
        fb = Feedback.objects.get()
        self.assertEqual(fb.contact, "")
        self.assertFalse(fb.interview_ok)

    def test_본문이_비면_저장하지_않는다(self):
        r = self.post(body="   ")
        self.assertEqual(r.status_code, 200)          # 폼을 다시 그린다
        self.assertIn("의견을 한 줄이라도", r.content.decode())
        self.assertFalse(Feedback.objects.exists())
        self.send.assert_not_called()

    def test_본문은_상한에서_잘린다(self):
        self.post(body="가" * (Feedback.BODY_MAX + 500))
        self.assertEqual(len(Feedback.objects.get().body), Feedback.BODY_MAX)

    def test_제출하면_배너가_사라진다(self):
        """닫기를 누르지 않아도 목적을 달성했으면 안 띄운다."""
        self.assertIn(BANNER_MARK, self.weekly_html())
        self.post()
        self.assertNotIn(BANNER_MARK, self.weekly_html())

    def test_인터뷰_체크박스가_묻히지_않는다(self):
        """유일한 인터뷰 모집 경로라, 본문 바로 아래에 강조 상자로 둔다."""
        html = self.client.get(reverse("feedback")).content.decode()
        self.assertIn("fb-check", html)
        self.assertIn("30분 통화", html)
        # 안내 문구(page-sub)에서도 통화 모집을 먼저 알린다
        self.assertIn("통화로 더 이야기해 주실 분도 찾고 있습니다", html)
        # 순서: 본문 → 인터뷰 → 연락처 (연락처가 통화 일정 확인에도 쓰이므로)
        self.assertLess(html.index('name="body"'), html.index('name="interview_ok"'))
        self.assertLess(html.index('name="interview_ok"'), html.index('name="contact"'))

    def test_비로그인은_폼에_못_들어간다(self):
        self.client.logout()
        r = self.client.get(reverse("feedback"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

    def test_비로그인_제출도_막힌다(self):
        self.client.logout()
        r = self.post()
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])
        self.assertFalse(Feedback.objects.exists())


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c")
class 텔레그램알림(_Base):
    """⚠ 실발송 금지 — send_message는 항상 모킹."""

    def setUp(self):
        super().setUp()
        self.send = mock.patch("core.telegram.send_message", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.client.force_login(self.user)

    def submit(self, **kw):
        data = {"body": "휴대폰에서 표가 옆으로 잘려요"}
        data.update(kw)
        self.client.post(reverse("feedback"), data)
        return self.send.call_args[0][0] if self.send.called else None

    def test_기본값이_켜짐이라_그냥_나간다(self):
        """다른 알림 4종과 달리 .env 설정 없이도 발송된다 — 놓치면 안 되는 알림."""
        from django.conf import settings
        self.assertTrue(settings.TELEGRAM_FEEDBACK_ALERT_ENABLED)
        self.assertIsNotNone(self.submit())

    def test_본문과_인터뷰_의향이_들어간다(self):
        text = self.submit(interview_ok="1")
        self.assertIn("휴대폰에서 표가 옆으로 잘려요", text)
        self.assertIn("beta1", text)
        self.assertIn("인터뷰", text)
        self.assertIn("가능", text)

    def test_인터뷰_미체크도_구분된다(self):
        text = self.submit()
        self.assertIn("인터뷰", text)
        self.assertNotIn("인터뷰(30분 통화): 가능", text)

    def test_연락처_원문은_싣지_않는다(self):
        """공용 채널이라 다른 사람도 읽는다 — 남겼는지 여부만 알린다."""
        text = self.submit(contact="010-1234-5678")
        self.assertNotIn("010-1234-5678", text)
        self.assertIn("남김", text)

    @override_settings(TELEGRAM_FEEDBACK_ALERT_ENABLED=False)
    def test_끄면_발송하지_않지만_기록은_남는다(self):
        self.client.post(reverse("feedback"), {"body": "끈 상태 확인"})
        self.send.assert_not_called()
        self.assertEqual(Feedback.objects.count(), 1)

    def test_발송이_실패해도_저장은_유지된다(self):
        self.send.side_effect = RuntimeError("텔레그램 죽음")
        r = self.client.post(reverse("feedback"), {"body": "알림 실패해도 남아야 함"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Feedback.objects.count(), 1)

    @override_settings(SITE_URL="https://elsrader.site/")
    def test_관리자_링크가_붙는다(self):
        """연락처 원문을 안 싣는 대신, 바로 열어볼 주소를 준다."""
        text = self.submit()
        fb = Feedback.objects.get()
        self.assertIn(f"https://elsrader.site/admin/core/feedback/{fb.id}/change/", text)


class 제출후안내(_Base):
    """제출로 끝내지 않고 '다음에 뭐가 일어나는지'를 알린다.

    인터뷰 신청자에게 연락 방법·시점을 안 알려주면, 지금 유일한 인터뷰 모집
    경로에서 신청해 놓고 아무 일도 안 일어나는 것처럼 보인다.
    """

    def setUp(self):
        super().setUp()
        mock.patch("core.telegram.send_message", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.client.force_login(self.user)

    def done_html(self, **kw):
        data = {"body": "표가 잘려요"}
        data.update(kw)
        self.client.post(reverse("feedback"), data)
        return self.client.get(reverse("feedback") + "?sent=1").content.decode()

    def test_기프티콘_수령_방법과_시점을_알린다(self):
        html = self.done_html()
        self.assertIn("의견 잘 받았습니다", html)
        self.assertIn("커피 기프티콘", html)
        self.assertIn("일주일 안에", html)
        self.assertIn("가입하신 이메일로", html)      # 연락처 미입력

    def test_연락처를_남기면_그쪽으로_간다고_알린다(self):
        html = self.done_html(contact="010-0000-0000")
        self.assertIn("남겨주신 연락처로", html)

    def test_인터뷰_신청자에게는_통화_안내가_붙는다(self):
        html = self.done_html(interview_ok="1", contact="010-0000-0000")
        self.assertIn("30분 통화", html)
        self.assertIn("가능한 시간을 여쭙겠습니다", html)
        self.assertIn("거절하셔도 괜찮습니다", html)

    def test_미신청자에게는_다시_알릴_길을_준다(self):
        html = self.done_html()
        self.assertIn("생각이 바뀌시면", html)
        self.assertNotIn("가능한 시간을 여쭙겠습니다", html)

    def test_보낸_적_없으면_완료화면이_안_나온다(self):
        """주소에 ?sent=1만 붙여도 완료 화면이 뜨면 안 된다."""
        html = self.client.get(reverse("feedback") + "?sent=1").content.decode()
        self.assertNotIn("의견 잘 받았습니다", html)
        self.assertIn('name="body"', html)            # 그냥 폼이다

    def test_새로고침해도_두_번_저장되지_않는다(self):
        """리다이렉트로 갈라 뒀으므로 완료 화면 새로고침은 GET일 뿐이다."""
        self.done_html()
        self.client.get(reverse("feedback") + "?sent=1")
        self.assertEqual(Feedback.objects.count(), 1)


class 관리자화면(_Base):
    """운영자가 받은 의견을 목록으로 본다 — 연락처 원문을 읽는 유일한 경로."""

    def test_목록에_본문과_연락처가_보인다(self):
        boss = get_user_model().objects.create_superuser(
            username="boss", email="boss@example.com", password="pw-test-1234")
        Feedback.objects.create(user=self.user, body="휴대폰에서 표가 잘려요",
                                contact="010-1234-5678", interview_ok=True)
        self.client.force_login(boss)
        r = self.client.get("/admin/core/feedback/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("휴대폰에서 표가 잘려요", html)
        self.assertIn("010-1234-5678", html)
