"""낙인 퍼센타일 컷 테스트 — RADAR_V7_KI_PCT.

배경 (2026-08-11)
  v7은 낙인 컷을 '직전 연도 같은 유형 발행 분포의 하위 30%'로 잡았다.
  v8에서 이 퍼센타일 하나만 40%로 완화했다. 1차 배리어·고점 게이트는 그대로다.

여기서 못 박는 것
  ① 퍼센타일 상수는 0.40이다
  ② 컷은 '직전 연도 전체 분포'에서 나온다 — 그 주차 그룹 분포가 아니다
  ③ 비교는 '미만'이다 — 낙인이 컷과 같으면 탈락
  ④ 직전 연도 표본이 RADAR_V7_KI_MIN_SAMPLE 미만이면 v6 고정 컷으로 폴백한다
  ⑤ 배지에 정원이 없다 — 게이트 통과자는 전원 배지다

⑤를 못 박는 이유: v8 튜닝에 쓰인 백테스트 스윕은 '낙인 5단위 버킷마다 쿠폰
상위 5개'라는 정원을 두고 목표 수치를 냈는데, 서비스 코드에는 그런 정원이
없다. 두 구조를 같은 것으로 착각하면 배지 공급량이 두 배로 어긋난다.
(서비스의 TOP5는 radar_tracks의 화면 표시 상한이고 배지 자격과 무관하다.)
"""

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase

from core.models import (
    _RADAR_POOL_CACHE, _V7_KI_CUT_CACHE, RADAR_KI_EXCL, RADAR_V7_KI_MIN_SAMPLE,
    RADAR_V7_KI_PCT, HistoricalIssue, Product, _compute_radar_pool, v7_ki_cut,
)

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())
PREV_YEAR = TODAY.year - 1


def seed_dist(kis, asset_type="지수형", year=None):
    """직전 연도 발행 분포를 만든다 (v7_ki_cut이 읽는 조건 그대로)."""
    year = year or PREV_YEAR
    sort = "지수" if asset_type == "지수형" else "주식"
    for i, ki in enumerate(kis):
        HistoricalIssue.objects.create(
            isin=f"KR{asset_type[:1]}{year}{i:06d}", issuer="한국투자증권",
            product_type="ELS", basset_sort=sort, ki=ki, detail_fetched=True,
            issue_date=date(year, 6, 1))


def make(**kw):
    """이번 주 마감 ELS 하나. 낙인 말고는 전부 게이트를 통과하는 값."""
    base = dict(
        issuer="키움증권", product_no=str(Product.objects.count() + 1000),
        name="키움 ELS", product_type="ELS", yield_rate=12.0,
        ki=25, is_no_ki=False, barrier_first=85, barrier_last=65,
        assets_raw="KOSPI200 Index", asset_type="지수형",
        sub_end=TODAY, currency="KRW",
        issue_date=MONDAY + timedelta(days=3), period_months=6,
    )
    base.update(kw)
    return Product.objects.create(**base)


class KiCutValueTest(TestCase):
    """v7_ki_cut이 직전 연도 분포에서 뽑는 값."""

    def setUp(self):
        _V7_KI_CUT_CACHE.clear()
        self.addCleanup(_V7_KI_CUT_CACHE.clear)

    def test_퍼센타일_상수는_40퍼센트다(self):
        self.assertEqual(RADAR_V7_KI_PCT, 0.40)

    def test_컷은_직전연도_분포의_하위_40퍼센트_값이다(self):
        # 정렬하면 인덱스 0~34가 30, 35~99가 45.
        # 하위 30% → 인덱스 30 → 30 / 하위 40% → 인덱스 40 → 45
        seed_dist([30] * 35 + [45] * 65)
        self.assertEqual(v7_ki_cut("지수형"), 45)

    def test_30퍼센트였다면_같은_분포에서_컷이_더_낮았다(self):
        """완화 전후를 같은 표본에서 대조한다 — 상수 하나가 원인임을 못 박는다."""
        seed_dist([30] * 35 + [45] * 65)
        with mock.patch("core.models.RADAR_V7_KI_PCT", 0.30):
            _V7_KI_CUT_CACHE.clear()
            self.assertEqual(v7_ki_cut("지수형"), 30)
        _V7_KI_CUT_CACHE.clear()
        self.assertEqual(v7_ki_cut("지수형"), 45)

    def test_유형별로_따로_센다(self):
        seed_dist([30] * 35 + [45] * 65, "지수형")
        seed_dist([20] * 35 + [35] * 65, "종목형")
        self.assertEqual(v7_ki_cut("지수형"), 45)
        self.assertEqual(v7_ki_cut("종목형"), 35)

    def test_표본이_모자라면_v6_고정컷으로_폴백한다(self):
        seed_dist([30] * (RADAR_V7_KI_MIN_SAMPLE - 1))
        self.assertEqual(v7_ki_cut("지수형"), RADAR_KI_EXCL["지수형"])

    def test_직전연도만_본다(self):
        """올해·재작년 발행분은 컷 산출에 끼어들지 않는다."""
        seed_dist([30] * 35 + [45] * 65)
        seed_dist([90] * 500, year=PREV_YEAR - 1)
        seed_dist([90] * 500, year=TODAY.year)
        self.assertEqual(v7_ki_cut("지수형"), 45)


class KiCutGateTest(TestCase):
    """컷이 배지 판정에서 쓰이는 방식 — 미만 비교, 정원 없음."""

    def setUp(self):
        _RADAR_POOL_CACHE.clear()
        _V7_KI_CUT_CACHE.clear()
        self.addCleanup(_RADAR_POOL_CACHE.clear)
        self.addCleanup(_V7_KI_CUT_CACHE.clear)
        patcher = mock.patch("core.market.resolve_ticker", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pool(self, cut=35):
        """고점 게이트만 통과로 고정하고 실제 _compute_radar_pool을 돌린다."""
        with mock.patch("core.models.v7_peak_gate", return_value=(True, 80)), \
                mock.patch("core.models.v7_ki_cut", return_value=cut):
            return _compute_radar_pool(MONDAY, "지수형")

    def test_낙인이_컷과_같으면_탈락이다(self):
        same = make(product_no="7001", ki=35)
        under = make(product_no="7002", ki=34)
        pool = self._pool(cut=35)
        self.assertNotIn(same.id, pool)
        self.assertIn(under.id, pool)

    def test_컷이_40퍼센트로_올라가면_통과가_늘어난다(self):
        """같은 상품 집합에서 컷만 30→35로 바꾼 효과."""
        low = make(product_no="7101", ki=25)
        mid = make(product_no="7102", ki=32)
        self.assertEqual(set(self._pool(cut=30)), {low.id})
        self.assertEqual(set(self._pool(cut=35)), {low.id, mid.id})

    def test_배지에_정원이_없다(self):
        """낙인이 같은(=같은 5단위 버킷) 상품이 8개면 8개 전부 배지다."""
        ids = {make(product_no=f"72{i:02d}", ki=30, yield_rate=10.0 + i).id
               for i in range(8)}
        pool = self._pool(cut=35)
        self.assertEqual(set(pool), ids)
        self.assertEqual(len(pool), 8)

    def test_통과자_전원이_같은_배지를_받는다(self):
        """순위는 매기되 등급을 가르지 않는다 — v6의 상위 5/15 정원은 없다."""
        for i in range(8):
            make(product_no=f"73{i:02d}", ki=30, yield_rate=10.0 + i)
        pool = self._pool(cut=35)
        self.assertEqual({r["tier"] for r in pool.values()}, {"타겟 신호"})
        self.assertEqual(sorted(r["srank"] for r in pool.values()), list(range(1, 9)))
