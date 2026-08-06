"""주간 청약 프리셋 개인화 테스트.

배경 (2026-08-06 · 태훈님 "프리셋은 공란으로 하고 각 사용자가 개별적으로 심어서
개인화하는 방향으로 하자")

조사 결과 기본값 공란과 계정별 소유는 이미 되어 있었고(_scope · 7/20 분리),
정작 그 원칙을 코드가 안 지키는 자리가 남아 있었다.

여기서 지키려는 것
  ① 프리셋은 본인 것만 걸린다 — URL로 남의 id를 넣어도 조건이 적용되지 않는다
  ② 무효한 선택(남의 것·없는 것·숫자 아닌 것)은 공란으로 되돌아간다.
     500이 나서도 안 되고, 아무 칩도 안 켜진 어정쩡한 상태가 돼서도 안 된다
  ③ 비로그인에게는 프리셋 UI가 없다 — 걸 수 있는 것이 없으므로
  ④ 로그인했는데 프리셋이 없으면 만들러 가는 길이 화면에 있다
  ⑤ 텔레그램(가족 공용 채널)은 일반 회원이 수정으로도 켤 수 없다
  ⑥ match_queryset의 .listed()는 살아 있다 — 화면·알림 공용 길목이라
     여기가 뚫리면 목록에서 뺀 ELB·DLB가 다시 샌다
"""

import re
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import _RADAR_POOL_CACHE, Preset, Product

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


class OfflineMixin:
    """시세 조회를 끊고 배지 캐시를 비운다 (tests_exclude_principal과 같은 이유)."""

    def setUp(self):
        super().setUp()
        _RADAR_POOL_CACHE.clear()
        patcher = mock.patch("core.market.resolve_ticker", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_RADAR_POOL_CACHE.clear)


def make(**kw):
    """이번 주 마감 상품 하나."""
    base = dict(
        issuer="키움증권", product_no=str(Product.objects.count() + 1000),
        name="키움 ELS", product_type="ELS",
        yield_rate=12.0, ki=25, is_no_ki=False, barrier_first=85, barrier_last=65,
        assets_raw="KOSPI200 Index", asset_type="지수형",
        sub_end=TODAY, currency="KRW",
        issue_date=MONDAY + timedelta(days=3), period_months=6,
    )
    base.update(kw)
    return Product.objects.create(**base)


def user(name, **kw):
    return get_user_model().objects.create_user(name, password="x", **kw)


class PresetIsolationTest(OfflineMixin, TestCase):
    """① 프리셋은 본인 것만 걸린다."""

    def setUp(self):
        super().setUp()
        # 종목형 1건 + 지수형 2건 — 프리셋이 걸리면 1건, 안 걸리면 3건
        make(product_no="7001", asset_type="종목형", yield_rate=30.0)
        make(product_no="7002", yield_rate=10.0)
        make(product_no="7003", yield_rate=18.0)
        self.owner = user("owner")
        self.preset = Preset.objects.create(
            user=self.owner, name="내 조건", asset_type="종목형",
            yield_min=25.0, include_no_ki=False)

    def _total(self, **params):
        return self.client.get(reverse("weekly"), params).context["total"]

    def test_주인은_자기_프리셋으로_걸러진다(self):
        self.client.force_login(self.owner)
        self.assertEqual(self._total(preset=self.preset.id), 1)

    def test_비로그인은_남의_프리셋_id로_걸러지지_않는다(self):
        # 2026-08-06 실측: ?preset=1 하나로 남의 조건이 그대로 먹었다
        self.assertEqual(self._total(preset=self.preset.id), 3)

    def test_다른_회원도_남의_프리셋_id로_걸러지지_않는다(self):
        self.client.force_login(user("other"))
        self.assertEqual(self._total(preset=self.preset.id), 3)

    def test_프리셋_없이는_전체가_보인다(self):
        self.client.force_login(self.owner)
        self.assertEqual(self._total(), 3)


class InvalidPresetParamTest(OfflineMixin, TestCase):
    """② 무효한 선택은 500이 아니라 공란으로 되돌아간다."""

    def setUp(self):
        super().setUp()
        make(product_no="7101")
        make(product_no="7102")

    def test_숫자가_아닌_값에_500이_나지_않는다(self):
        # 예전엔 Preset.objects.get(id="abc")가 ValueError를 내 /weekly/가 통째로 500
        r = self.client.get(reverse("weekly"), {"preset": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["total"], 2)

    def test_없는_id에도_전체가_보인다(self):
        r = self.client.get(reverse("weekly"), {"preset": "99999"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["total"], 2)

    def test_무효한_선택은_필터값이_공란으로_정규화된다(self):
        # 정규화가 없으면 '전체' 칩도 프리셋 칩도 안 켜진 상태로 렌더된다
        u = user("normalize")
        Preset.objects.create(user=u, name="내 조건")
        self.client.force_login(u)
        for bad in ("abc", "99999", "-1", ""):
            r = self.client.get(reverse("weekly"), {"preset": bad})
            self.assertEqual(r.context["filters"]["preset"], "", f"preset={bad!r}")
            self.assertIn('class="preset-chip active">전체</a>', r.content.decode())


class PresetFilterBarTest(OfflineMixin, TestCase):
    """③④ 화면 — 비로그인 / 로그인·프리셋 없음 / 로그인·프리셋 있음."""

    def setUp(self):
        super().setUp()
        make(product_no="7201")

    def _body(self):
        return self.client.get(reverse("weekly")).content.decode()

    def _filterbar(self):
        """필터바 안쪽 프리셋 영역만 잘라 본다.

        문서 전체에서 '프리셋'·'preset-chip'을 찾으면 base.html의 CSS 규칙과
        그 주석에 걸린다. 스타일시트가 아니라 화면에 그려진 것을 봐야 한다.
        """
        body = self._body()
        m = re.search(r'<div class="filterbar">(.*?)<form method="get"', body, re.S)
        self.assertIsNotNone(m, "필터바를 찾지 못했다 — 템플릿 구조가 바뀌었다")
        return m.group(1)

    def test_비로그인에는_프리셋_UI가_없다(self):
        bar = self._filterbar()
        self.assertNotIn("preset-chip", bar)
        self.assertNotIn("프리셋", bar)

    def test_로그인_프리셋_없으면_만들러_가는_길이_있다(self):
        self.client.force_login(user("nopreset"))
        bar = self._filterbar()
        self.assertIn("새 프리셋 추가", bar)
        self.assertIn(reverse("presets"), bar)
        # 고를 것이 없으므로 '전체' 칩은 두지 않는다
        self.assertNotIn(">전체</a>", bar)

    def test_로그인_프리셋_있으면_칩으로_고른다(self):
        u = user("haspreset")
        Preset.objects.create(user=u, name="내 조건", asset_type="지수형")
        self.client.force_login(u)
        bar = self._filterbar()
        self.assertIn("내 조건", bar)
        self.assertIn('class="preset-chip active">전체</a>', bar)  # 기본은 공란
        self.assertNotIn("새 프리셋 추가", bar)

    def test_남의_프리셋명은_칩에_뜨지_않는다(self):
        Preset.objects.create(user=user("owner"), name="남의조건")
        self.client.force_login(user("visitor"))
        self.assertNotIn("남의조건", self._body())

    def test_개발주석이_화면으로_새지_않는다(self):
        # {# #}를 두 줄로 쓰면 주석으로 인식되지 않고 그대로 렌더된다.
        # 필터바 설명은 {% comment %}로 썼으므로 그 자리를 못 박아 둔다.
        self.client.force_login(user("commentcheck"))
        for body in (self._body(), self.client.get(reverse("presets")).content.decode()):
            self.assertNotIn("고장난 컨트롤처럼", body)
            self.assertNotIn("텔레그램은 가족 공용 운영 채널이라", body)
            self.assertNotIn("{% comment %}", body)


class PresetNotifyGuardTest(OfflineMixin, TestCase):
    """⑤ 텔레그램은 가족 공용 채널 — 일반 회원은 켤 수 없다."""

    def _post(self, u, **extra):
        self.client.force_login(u)
        data = {"action": "save", "name": "내 조건", "asset_type": "전체", "notify": "on"}
        data.update(extra)
        return self.client.post(reverse("presets"), data)

    def test_일반회원은_추가할_때_켤_수_없다(self):
        u = user("member")
        self._post(u)
        self.assertFalse(Preset.objects.get(user=u).notify)

    def test_일반회원은_수정으로도_켤_수_없다(self):
        # 가드가 추가에만 있어서, 만든 뒤 수정 POST로 다시 켜졌다
        u = user("member2")
        p = Preset.objects.create(user=u, name="내 조건", notify=False)
        self._post(u, id=p.id)
        p.refresh_from_db()
        self.assertFalse(p.notify)

    def test_staff는_켤_수_있다(self):
        u = user("staffer", is_staff=True)
        self._post(u)
        self.assertTrue(Preset.objects.get(user=u).notify)

    def test_체크박스는_staff에게만_보인다(self):
        u = user("member3")
        Preset.objects.create(user=u, name="내 조건")
        self.client.force_login(u)
        self.assertNotIn("텔레그램 알림", self.client.get(reverse("presets")).content.decode())

        s = user("staffer2", is_staff=True)
        Preset.objects.create(user=s, name="운영 조건")
        self.client.force_login(s)
        self.assertIn("텔레그램 알림", self.client.get(reverse("presets")).content.decode())

    def test_안내문구가_지키지_못할_약속을_하지_않는다(self):
        # 예전 서브타이틀은 모두에게 "텔레그램으로 알려드립니다"라고 말했다
        self.client.force_login(user("member4"))
        body = self.client.get(reverse("presets")).content.decode()
        self.assertNotIn("텔레그램으로 알려드립니다", body)


class MatchQuerysetKeepsListedTest(OfflineMixin, TestCase):
    """⑥ match_queryset의 .listed()는 살아 있다 (원금지급형 제외 · 8/6 배포)."""

    def test_ELB는_프리셋에_매칭되지_않는다(self):
        els = make(product_no="7301", yield_rate=30.0)
        make(product_no="7302", product_type="ELB", ki=None, is_no_ki=True, yield_rate=30.0)
        p = Preset.objects.create(user=user("owner"), name="고수익",
                                  yield_min=25.0, include_no_ki=False)
        self.assertEqual(set(p.match_queryset().values_list("id", flat=True)), {els.id})

    def test_화면_프리셋_필터에서도_빠진다(self):
        make(product_no="7401", yield_rate=30.0)
        make(product_no="7402", product_type="ELB", ki=None, is_no_ki=True, yield_rate=30.0)
        u = user("owner2")
        p = Preset.objects.create(user=u, name="고수익", yield_min=25.0, include_no_ki=False)
        self.client.force_login(u)
        body = self.client.get(reverse("weekly"), {"preset": p.id}).content.decode()
        self.assertIn("7401", body)
        self.assertNotIn("7402", body)
