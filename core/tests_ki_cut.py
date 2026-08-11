"""낙인 컷 테스트 — 컷을 어디서 뽑고 어떻게 비교하는가.

배경 (2026-08-11)
  v7은 낙인 컷을 '직전 연도 같은 유형 발행 분포의 하위 30%'로 잡고 미만으로
  비교했다. v8은 컷의 출처를 '그 주차×유형 그룹 내부'로 옮기고 이하로 비교한다.
  퍼센타일 자체도 30%→40%로 완화했다.

여기서 못 박는 것
  ① 퍼센타일 상수는 0.40이다
  ② 배지 컷은 **그 주차 그룹 분포**에서 나온다 — 직전 연도 분포가 아니다
  ③ 비교는 '이하'다 — 낙인이 컷과 같으면 통과한다
  ④ group_cut은 색인식이다 (분위수 보간을 하지 않는다)
  ⑤ v7_ki_cut(직전 연도 컷)은 남아 있되 배지가 아니라 시장 국면 계기판 몫이다

②를 못 박는 이유: 절대 컷으로 되돌리면 낙인이 통상 50~65였던 2016~2022년
빈티지와 30~35인 2025년을 같은 잣대로 재게 된다. ⑤를 갈라 두는 이유: 국면
계기판까지 그룹 상대 컷으로 바꾸면 통과율이 정의상 늘 40% 근처에 붙어
'이번 주 조건이 좋은가'를 못 읽는다.

쿠폰 필터·정원은 tests_radar_v8.py가 맡는다.
"""

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase

from core.models import (
    _RADAR_POOL_CACHE, _V7_KI_CUT_CACHE, RADAR_KI_EXCL, RADAR_V7_KI_MIN_SAMPLE,
    RADAR_V7_KI_PCT, HistoricalIssue, Product, _compute_radar_pool, group_cut,
    v7_ki_cut,
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


class GroupCutTest(TestCase):
    """group_cut — 백테스트 스윕이 쓴 색인식 그대로인가."""

    def test_하위_40퍼센트_색인의_값을_고른다(self):
        # int(5 * 0.40) = 2 → 정렬 후 세 번째 값
        self.assertEqual(group_cut([50, 20, 40, 30, 10], 0.40), 30)

    def test_보간하지_않는다(self):
        """분위수 보간이면 42.5가 나오는 자리 — 표본에 있는 값이어야 한다."""
        self.assertEqual(group_cut([40, 45], 0.50), 45)

    def test_색인이_넘치면_마지막_값에서_멈춘다(self):
        self.assertEqual(group_cut([10, 20], 1.0), 20)

    def test_None은_분포에서_빠진다(self):
        self.assertEqual(group_cut([10, None, 20, None, 30], 0.40), 20)

    def test_값이_없으면_None이다(self):
        self.assertIsNone(group_cut([], 0.40))
        self.assertIsNone(group_cut([None, None], 0.40))


class KiCutValueTest(TestCase):
    """v7_ki_cut — 직전 연도 절대 컷. 배지가 아니라 시장 국면 계기판이 쓴다."""

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


class KiGateTest(TestCase):
    """배지 낙인 게이트 — 그 주차 그룹 분포에서 컷을 잡고 이하로 비교한다."""

    def setUp(self):
        _RADAR_POOL_CACHE.clear()
        _V7_KI_CUT_CACHE.clear()
        self.addCleanup(_RADAR_POOL_CACHE.clear)
        self.addCleanup(_V7_KI_CUT_CACHE.clear)
        patcher = mock.patch("core.market.resolve_ticker", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pool(self):
        """고점 게이트만 통과로 고정하고 실제 _compute_radar_pool을 돌린다."""
        with mock.patch("core.models.v7_peak_gate", return_value=(True, 80)):
            return _compute_radar_pool(MONDAY, "지수형")

    def _ladder(self):
        """낙인 20·25·30·35·40 다섯 건. 쿠폰은 같아서 쿠폰 필터가 안 걸린다.

        낙인 컷 = 정렬 [20,25,30,35,40]의 int(5*0.40)=2번째 = 30.
        낙인이 다 달라 5단위 버킷도 전부 1건씩 → 정원도 안 걸린다.
        """
        return {ki: make(product_no=f"75{ki}", ki=ki)
                for ki in (20, 25, 30, 35, 40)}

    def test_컷은_그_주차_그룹에서_나온다(self):
        """직전 연도 분포를 아무리 넣어도 배지 결과가 안 바뀐다."""
        p = self._ladder()
        before = set(self._pool())
        seed_dist([90] * 500)          # 연간 컷이었다면 90 → 다섯 건 전부 통과
        _RADAR_POOL_CACHE.clear()
        _V7_KI_CUT_CACHE.clear()
        self.assertEqual(set(self._pool()), before)
        self.assertEqual(before, {p[20].id, p[25].id, p[30].id})

    def test_낙인이_컷과_같으면_통과한다(self):
        """v7은 미만이라 탈락시켰다 — v8에서 뒤집힌 자리다."""
        p = self._ladder()
        self.assertIn(p[30].id, self._pool())      # 컷 = 30

    def test_컷보다_높으면_탈락한다(self):
        p = self._ladder()
        pool = self._pool()
        self.assertNotIn(p[35].id, pool)
        self.assertNotIn(p[40].id, pool)

    def test_노낙인은_낙인_분포에도_배지에도_못_들어간다(self):
        p = self._ladder()
        for i in range(4):
            make(product_no=f"76{i}", ki=None, is_no_ki=True)
        # 노낙인이 분포에 끼면 컷이 흔들린다 — 컷도 통과자도 그대로여야 한다
        self.assertEqual(set(self._pool()), {p[20].id, p[25].id, p[30].id})
