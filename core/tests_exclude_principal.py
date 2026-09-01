"""원금지급형(ELB·DLB) 화면·배지 풀 제외 테스트.

배경 (2026-08-06 운영 4,769건 실측 · 태훈님 확정)
  상품유형 소급 교정으로 ELB 438 / DLB 32건이 드러났다. 이들은 낙인 배리어가
  없어(운영 470건 전부 ki=NULL) 목록에서 낙인 칸이 비고, ELS 백테스트가 늘
  0.0%를 내서 손실확률 칸이 초록 0.0%로 찍혔다. 지표가 성립하지 않는다는 뜻인데
  화면에서는 '가장 안전한 상품'으로 뒤집혀 읽힌다. 그래서 통째로 뺀다.

여기서 지키려는 것
  ① 제외 규칙의 근거는 ProductQuerySet.listed() **한 곳**이다
  ② DLS는 빠지지 않는다 — 원금비보장 파생결합증권이라 같은 지표가 성립한다
  ③ 목록에서 뺀 것이 알림으로 새지 않는다 (프리셋 → 텔레그램)
  ④ 상세는 404로 막지 않는다 — 링크·북마크·관심목록에서 들어올 수 있다
  ⑤ 배지 통과자는 제외 전후가 같다 (ELB는 낙인 게이트에서 이미 전량 탈락)
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    _RADAR_POOL_CACHE, PRINCIPAL_PROTECTED_TYPES, Preset, Product, WatchItem,
    _compute_radar_pool,
)

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


class OfflineMixin:
    """시세 조회를 끊고 배지 캐시를 비운다.

    resolve_ticker가 None을 주면 고점 게이트·고점대비가 모두 값 없이 빠져나가
    네트워크를 타지 않는다. _RADAR_POOL_CACHE는 모듈 전역이라 비우지 않으면
    앞 테스트가 만든 (주차, 유형) 결과가 다음 테스트로 새어 들어온다.
    """

    def setUp(self):
        super().setUp()
        _RADAR_POOL_CACHE.clear()
        patcher = mock.patch("core.market.resolve_ticker", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_RADAR_POOL_CACHE.clear)


def make(product_type="ELS", **kw):
    """이번 주 마감 상품 하나. 낙인 게이트를 통과하는 값이 기본이다."""
    base = dict(
        issuer="키움증권", product_no=str(Product.objects.count() + 1000),
        name=f"키움 {product_type}", product_type=product_type,
        yield_rate=12.0, ki=25, is_no_ki=False, barrier_first=85, barrier_last=65,
        assets_raw="KOSPI200 Index", asset_type="지수형",
        sub_end=TODAY, currency="KRW",
        issue_date=MONDAY + timedelta(days=3), period_months=6,
    )
    base.update(kw)
    return Product.objects.create(**base)


def elb(**kw):
    """운영 실측 형태의 ELB — 낙인이 없고(ki=NULL·노낙인) 쿠폰이 낮다."""
    base = dict(product_type="ELB", ki=None, is_no_ki=True, yield_rate=6.0)
    base.update(kw)
    return make(**base)


class ListedQuerySetTest(TestCase):
    """제외 규칙 자체 — 근거가 한 곳(listed)에 있는지."""

    def test_ELB와_DLB만_빠지고_ELS_DLS는_남는다(self):
        els, dls = make("ELS"), make("DLS")
        e, d = elb(), elb(product_type="DLB")
        got = set(Product.objects.listed().values_list("id", flat=True))
        self.assertEqual(got, {els.id, dls.id})
        self.assertNotIn(e.id, got)
        self.assertNotIn(d.id, got)

    def test_기본_매니저는_전량_그대로다(self):
        # 수집·중복정리·백필 배치는 ELB·DLB도 계속 다뤄야 한다
        make("ELS")
        elb()
        self.assertEqual(Product.objects.count(), 2)
        self.assertEqual(Product.objects.listed().count(), 1)

    def test_제외_대상은_상수_한_곳에서_온다(self):
        self.assertEqual(PRINCIPAL_PROTECTED_TYPES, ("ELB", "DLB"))
        self.assertNotIn("DLS", PRINCIPAL_PROTECTED_TYPES)

    def test_상품_단건_판정이_같은_상수를_쓴다(self):
        self.assertTrue(elb().is_principal_protected)
        self.assertTrue(elb(product_type="DLB").is_principal_protected)
        self.assertFalse(make("DLS").is_principal_protected)
        self.assertFalse(make("ELS").is_principal_protected)


class ScreenExclusionTest(OfflineMixin, TestCase):
    """화면별 목록 — ELB·DLB가 안 보이고 DLS는 보인다."""

    def setUp(self):
        super().setUp()
        self.els = make("ELS", product_no="7001")
        self.dls = make("DLS", product_no="7002")
        self.elb = elb(product_no="7003")
        self.dlb = elb(product_no="7004", product_type="DLB")

    def _body(self, url):
        return self.client.get(url).content.decode()

    def test_주간청약_목록(self):
        body = self._body(reverse("weekly"))
        self.assertIn("7001", body)
        self.assertIn("7002", body)          # DLS는 남는다
        self.assertNotIn("7003", body)
        self.assertNotIn("7004", body)

    def test_주간청약_총건수가_제외분만큼_준다(self):
        resp = self.client.get(reverse("weekly"))
        self.assertEqual(resp.context["total"], 2)

    def test_주간청약_발행사_후보에도_안_남는다(self):
        # ELB만 낸 발행사를 고르면 0건이 나오는 필터가 생긴다
        elb(issuer="원금지급전문증권", product_no="7005")
        issuers = self.client.get(reverse("weekly")).context["issuers"]
        self.assertNotIn("원금지급전문증권", issuers)

    def test_검색_결과(self):
        body = self._body(reverse("search") + "?q=키움")
        self.assertIn("7001", body)
        self.assertIn("7002", body)
        self.assertNotIn("7003", body)
        self.assertNotIn("7004", body)

    def test_트렌드_월별_집계(self):
        rows = self.client.get(reverse("trend")).context["rows"]
        month = date(TODAY.year, TODAY.month, 1)
        monthly = [r for r in rows if r["month"] == month]
        self.assertEqual(len(monthly), 1)
        self.assertEqual(monthly[0]["count"], 2)      # ELS + DLS

    def test_트렌드_평균수익률이_ELB에_눌리지_않는다(self):
        rows = self.client.get(reverse("trend")).context["rows"]
        month = date(TODAY.year, TODAY.month, 1)
        monthly = [r for r in rows if r["month"] == month][0]
        self.assertEqual(monthly["avg_yield"], 12.0)  # ELB 6.0%가 섞이면 9.0이 된다

    def test_시장국면_통과율_모수에서_빠진다(self):
        # ELB는 낙인이 없어 통과에는 못 들어가면서 모수만 키운다
        regime = self.client.get(reverse("trend")).context["regime"]
        self.assertEqual(regime["n_all"], 2)
        self.assertEqual(regime["n_pass"], 2)


class PresetAndNotifyTest(OfflineMixin, TestCase):
    """프리셋 — 화면 필터와 텔레그램 알림이 같은 함수를 탄다."""

    def setUp(self):
        super().setUp()
        self.els = make("ELS", product_no="8001", yield_rate=30.0)
        self.elb = elb(product_no="8002", yield_rate=30.0)

    def _preset(self, **kw):
        # 운영 프리셋 '나의플랜'과 같은 모양 — 낙인 조건이 아예 안 걸리는 상태.
        # 낙인 조건에 기대면 ELB가 그대로 새어 나간다.
        base = dict(name="나의플랜", asset_type="전체", ki_min=None, ki_max=None,
                    include_no_ki=False, yield_min=25.0, notify=True)
        base.update(kw)
        return Preset.objects.create(**base)

    def test_낙인조건이_없어도_ELB는_매칭되지_않는다(self):
        got = set(self._preset().match_queryset().values_list("id", flat=True))
        self.assertEqual(got, {self.els.id})

    @override_settings(TELEGRAM_PRESET_MATCH_ALERT_ENABLED=True)
    def test_텔레그램_발송_목록에서도_빠진다(self):
        # 발송 자체는 2026-08-11부터 기본 꺼짐이다. 여기서 고정하려는 것은
        # '켜져 있을 때 ELB가 목록에 실리지 않는다'이므로 토글만 켜고 본다.
        from core import notify
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            notify.notify_preset_matches()
        sent = "\n".join(str(c) for c in tg.call_args_list)
        self.assertIn("8001", sent)
        self.assertNotIn("8002", sent)

    def test_프리셋_화면_매칭건수(self):
        user = get_user_model().objects.create_user("조팀장", password="x", is_staff=True)
        self._preset(user=user)
        self.client.force_login(user)
        rows = self.client.get(reverse("presets")).context["preset_list"]
        self.assertEqual(rows[0]["match_count"], 1)


class DetailStaysReachableTest(OfflineMixin, TestCase):
    """상세는 막지 않는다 — 링크·북마크·관심목록에서 들어올 수 있다."""

    def test_ELB_상세가_404가_아니다(self):
        p = elb()
        resp = self.client.get(reverse("product_detail", args=[p.id]))
        self.assertEqual(resp.status_code, 200)

    def test_상세에_목록에서_빠진_이유가_적혀_있다(self):
        p = elb()
        body = self.client.get(reverse("product_detail", args=[p.id])).content.decode()
        self.assertIn("원금지급형(ELB)", body)
        self.assertIn("목록에는 표시하지 않습니다", body)

    def test_ELS_상세에는_안내가_안_붙는다(self):
        p = make("ELS")
        body = self.client.get(reverse("product_detail", args=[p.id])).content.decode()
        self.assertNotIn("목록에는 표시하지 않습니다", body)

    def test_개발주석이_화면으로_새지_않는다(self):
        # 여러 줄 주석을 {# #}로 쓰면 주석으로 인식되지 않고 그대로 렌더된다.
        # 최근 그 사고를 고쳤으므로 {% comment %}를 쓴 자리를 못 박아 둔다.
        for p in (elb(), make("ELS")):
            body = self.client.get(reverse("product_detail", args=[p.id])).content.decode()
            self.assertNotIn("404로 막지 않고", body)
            self.assertNotIn("{% comment %}", body)

    def test_관심목록에_담긴_ELB는_그대로_보인다(self):
        # 본인이 담은 데이터라 숨기지 않는다 (2026-08-06 운영 관심 9건은 전부 ELS)
        user = get_user_model().objects.create_user("조팀장", password="x")
        p = elb(product_no="9001")
        WatchItem.objects.create(user=user, product=p)
        self.client.force_login(user)
        self.assertIn("9001", self.client.get(reverse("watchlist")).content.decode())


class RadarPoolTest(OfflineMixin, TestCase):
    """배지 풀 — 통과자는 그대로, 수익성 백분위의 분모만 바뀐다."""

    def _pool(self):
        """고점 게이트만 통과로 고정하고 실제 _compute_radar_pool을 돌린다."""
        with mock.patch("core.models.v7_peak_gate", return_value=(True, 80)), \
                mock.patch("core.models.v7_ki_cut", return_value=30):
            return _compute_radar_pool(MONDAY, "지수형")

    def test_ELB가_있으나_없으나_배지_통과_ID가_같다(self):
        a = make("ELS", product_no="6001", yield_rate=12.0)
        b = make("ELS", product_no="6002", yield_rate=20.0)
        without = set(self._pool())
        # ELB를 잔뜩 넣어도 통과자는 그대로여야 한다 (ki=NULL → 낙인 게이트 탈락)
        for i in range(5):
            elb(product_no=f"600{i}B", yield_rate=5.0 + i)
        with_elb = set(self._pool())
        self.assertEqual(without, with_elb)
        self.assertEqual(without, {a.id, b.id})

    def test_낙인이_없는_상품은_애초에_배지를_못_받는다(self):
        e = elb()
        self.assertNotIn(e.id, self._pool())

    def test_수익성_백분위는_ELS끼리만_비교한다(self):
        # 쿠폰이 낮은 ELB가 분모에 깔려 있으면 ELS의 상대 위치가 부풀려진다.
        low = make("ELS", product_no="6101", yield_rate=10.0)
        make("ELS", product_no="6102", yield_rate=20.0)
        base = self._pool()[low.id]["axes"][0]["score"]
        for i in range(8):
            elb(product_no=f"61{i}B", yield_rate=3.0)
        self.assertEqual(self._pool()[low.id]["axes"][0]["score"], base)

    def test_DLS는_배지_풀에_남는다(self):
        d = make("DLS", product_no="6201", yield_rate=15.0)
        self.assertIn(d.id, self._pool())
