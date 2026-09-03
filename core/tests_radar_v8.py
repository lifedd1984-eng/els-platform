"""레이더 v8 선정식 — 쿠폰 필터와 정원.

배경 (2026-08-11)
  v8은 네 가지를 함께 바꿨다. 낙인 컷의 출처·비교(→ tests_ki_cut.py), 그리고
  여기서 다루는 쿠폰 필터와 정원이다. 넷 중 하나라도 빠지면 10년 백테스트
  목표(5,544건·정상상환 99.57%·손실 24·주당 10.70)가 안 나온다.

  특히 정원이 빠지면 조용히 두 배가 터진다 — 정원 없이 10년을 돌리면 12,613건
  주당 24.35개다. 배지가 흔해지면 '타겟 신호'라는 말 자체가 값을 잃는다.

여기서 못 박는 것
  ① 쿠폰 필터는 그 주차 그룹 분포의 하위 40% 값 **이상**만 통과시킨다
     (2026-09-03: 0.30 → 0.40 상향. 저쿠폰 상품을 더 걸러 표시 쿠폰을 높인다.)
  ② 정원은 (낙인 5단위 버킷)마다 쿠폰 상위 5개다
  ③ 버킷이 다르면 정원을 따로 받는다 — 한 주에 5개로 묶이지 않는다
  ④ 5단위 버킷 경계는 반올림이다 (43→45, 47→45, 48→50)
  ⑤ 버킷은 선정에만 쓴다 — 화면에는 원래 낙인값이 나간다
"""

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase

from core.models import (
    _RADAR_POOL_CACHE, RADAR_V8_BUCKET_TOP, RADAR_V8_KI_BUCKET,
    RADAR_V8_YLD_PCT, Product, _compute_radar_pool, v8_bucket_quota,
    v8_ki_bucket,
)

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def make(**kw):
    """이번 주 마감 ELS 하나. 낙인·쿠폰 말고는 전부 게이트를 통과하는 값."""
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


class PoolMixin:
    def setUp(self):
        super().setUp()
        _RADAR_POOL_CACHE.clear()
        self.addCleanup(_RADAR_POOL_CACHE.clear)
        patcher = mock.patch("core.market.resolve_ticker", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pool(self):
        """고점 게이트만 통과로 고정하고 실제 _compute_radar_pool을 돌린다."""
        with mock.patch("core.models.v7_peak_gate", return_value=(True, 80)):
            return _compute_radar_pool(MONDAY, "지수형")


class KiBucketTest(TestCase):
    """v8_ki_bucket — 5단위 반올림 경계."""

    def test_상수는_5단위_상위5개다(self):
        self.assertEqual(RADAR_V8_KI_BUCKET, 5)
        self.assertEqual(RADAR_V8_BUCKET_TOP, 5)

    def test_반올림_경계(self):
        # 42까지는 내려가고 43부터 45로 올라간다. 47은 45, 48부터 50.
        expect = {40: 40, 41: 40, 42: 40, 43: 45, 44: 45, 45: 45, 46: 45,
                  47: 45, 48: 50, 49: 50, 50: 50, 51: 50, 52: 50, 53: 55}
        self.assertEqual({k: v8_ki_bucket(k) for k in expect}, expect)

    def test_5의_배수는_제자리에_있는다(self):
        for k in (20, 25, 30, 35, 40, 45, 50, 55, 60, 65):
            self.assertEqual(v8_ki_bucket(k), k)


class YieldGateTest(PoolMixin, TestCase):
    """③ 쿠폰 필터 — 그룹 분포 하위 40% 값 이상."""

    def test_상수는_40퍼센트다(self):
        self.assertEqual(RADAR_V8_YLD_PCT, 0.40)

    def _spread(self):
        """쿠폰 10·11·12·13·14 다섯 건. 낙인은 전부 같아 낙인 컷이 안 걸린다.

        쿠폰 컷 = 정렬 [10,11,12,13,14]의 int(5*0.40)=2번째 = 12.
        낙인이 전부 25라 한 버킷에 다섯 건 → 정원 5개와 딱 맞아 안 걸린다.
        """
        return {y: make(product_no=f"81{y}", ki=25, yield_rate=float(y))
                for y in (10, 11, 12, 13, 14)}

    def test_쿠폰이_하위_40퍼센트_아래면_탈락한다(self):
        p = self._spread()
        pool = self.pool()
        self.assertNotIn(p[10].id, pool)
        self.assertNotIn(p[11].id, pool)          # 컷 12 미달

    def test_쿠폰이_컷과_같으면_통과한다(self):
        p = self._spread()
        self.assertIn(p[12].id, self.pool())      # 컷 = 12, 이상 비교

    def test_상위_60퍼센트만_남는다(self):
        p = self._spread()
        self.assertEqual(set(self.pool()),
                         {p[12].id, p[13].id, p[14].id})

    def test_컷의_모수는_게이트_통과자가_아니라_그룹_전체다(self):
        """낙인에서 탈락할 상품도 쿠폰 분포에는 들어간다 (백테스트와 같다)."""
        p = self._spread()
        # 낙인 90짜리 고쿠폰 4건 — 낙인 게이트에서 죽지만 쿠폰 컷은 밀어올린다
        for i in range(4):
            make(product_no=f"82{i}", ki=90, yield_rate=30.0 + i)
        # 쿠폰 분포가 [10,11,12,13,14,30,31,32,33] → int(9*0.4)=3 → 컷 13
        pool = self.pool()
        self.assertNotIn(p[12].id, pool)
        self.assertEqual(set(pool), {p[13].id, p[14].id})


class BucketQuotaTest(PoolMixin, TestCase):
    """⑤ 정원 — 낙인 5단위 버킷마다 쿠폰 상위 5개."""

    def test_한_버킷에서_5개까지만_배지다(self):
        """낙인이 같은 8건이 있어도 5건만 남는다 (v7은 8건 전부였다)."""
        ps = [make(product_no=f"83{i:02d}", ki=30, yield_rate=10.0 + i)
              for i in range(8)]
        # 쿠폰 [10..17] → int(8*0.4)=3 → 컷 13 → 5건이 ④까지 통과 → 정원과 딱 맞는다
        pool = self.pool()
        self.assertEqual(len(pool), RADAR_V8_BUCKET_TOP)
        self.assertEqual(set(pool), {p.id for p in ps[3:]})   # 쿠폰 13~17

    def test_잘리는_것은_쿠폰이_낮은_쪽이다(self):
        ps = [make(product_no=f"84{i:02d}", ki=30, yield_rate=10.0 + i)
              for i in range(8)]
        pool = self.pool()
        self.assertNotIn(ps[2].id, pool)      # 쿠폰 12 — 하위 40% 컷(13) 미달
        self.assertIn(ps[7].id, pool)         # 쿠폰 17 — 최상위

    def test_버킷이_다르면_정원을_따로_받는다(self):
        """낙인 43(→45)과 48(→50)은 서로 다른 줄에 서므로 합쳐서 10건이 된다."""
        # 낙인 55짜리 8건을 깔아 낙인 컷을 48까지 올린다
        # (정렬 43*6, 48*6, 55*8 → int(20*0.4)=8 → 컷 48)
        for i in range(8):
            make(product_no=f"85F{i}", ki=55, yield_rate=5.0)
        a = [make(product_no=f"85A{i}", ki=43, yield_rate=20.0 + i)
             for i in range(6)]
        b = [make(product_no=f"85B{i}", ki=48, yield_rate=20.0 + i)
             for i in range(6)]
        pool = self.pool()
        self.assertEqual(len(pool), 2 * RADAR_V8_BUCKET_TOP)
        self.assertEqual(sum(1 for p in a if p.id in pool), RADAR_V8_BUCKET_TOP)
        self.assertEqual(sum(1 for p in b if p.id in pool), RADAR_V8_BUCKET_TOP)

    def test_같은_버킷이면_낙인이_달라도_한_줄에_선다(self):
        """43과 47은 둘 다 버킷 45다 — 8건이 같이 서서 5건만 남는다."""
        for i in range(8):
            make(product_no=f"86F{i}", ki=55, yield_rate=5.0)
        near = [make(product_no=f"86A{i}", ki=43 if i % 2 else 47,
                     yield_rate=20.0 + i) for i in range(8)]
        pool = self.pool()
        self.assertEqual(len(pool), RADAR_V8_BUCKET_TOP)
        self.assertEqual(set(pool), {p.id for p in near[3:]})   # 쿠폰 상위 5건

    def test_동점_쿠폰은_같은_상품이_뽑힌다(self):
        """쿠폰이 같아도 상품코드로 갈라 다시 돌려도 답이 안 바뀐다."""
        for i in range(8):
            make(product_no=f"87{i}", product_code=f"KR6X{i:06d}",
                 ki=30, yield_rate=15.0)
        first = set(self.pool())
        _RADAR_POOL_CACHE.clear()
        self.assertEqual(len(first), RADAR_V8_BUCKET_TOP)
        self.assertEqual(set(self.pool()), first)

    def test_정원은_고점_게이트_뒤에_적용된다(self):
        """고점에서 떨어진 상품은 정원 자리를 안 먹는다 — 다음 순위가 올라온다.

        쿠폰 20~31 열두 건 → 쿠폰 컷은 int(12*0.40)=4번째 = 24 → 여덟 건이 남는다.
        고점 게이트가 최상위 30·31을 떨구면 여섯 건이 남고, 정원 5개라 쿠폰 24가 밀린다.
        고점 게이트가 정원 뒤였다면 24·25·26이 잘려서 세 건만 배지였을 것이다.
        """
        ps = [make(product_no=f"88{i:02d}", ki=30, yield_rate=20.0 + i)
              for i in range(12)]
        top = {ps[11].id, ps[10].id}

        def gate(p, refs=None):
            return (p.id not in top), 80        # 쿠폰 최상위 2건을 고점에서 떨군다

        with mock.patch("core.models.v7_peak_gate", side_effect=gate):
            pool = _compute_radar_pool(MONDAY, "지수형")
        self.assertEqual(len(pool), RADAR_V8_BUCKET_TOP)
        self.assertTrue(top.isdisjoint(pool))
        self.assertEqual(set(pool), {p.id for p in ps[5:10]})   # 쿠폰 25~29
        self.assertIn(ps[5].id, pool)          # 쿠폰 24가 밀리고 여기까지 내려온다

    def test_빈_생존자에는_아무_일도_없다(self):
        self.assertEqual(v8_bucket_quota([]), [])


class BucketDisplayTest(PoolMixin, TestCase):
    """⑤ 버킷은 선정에만 쓴다 — 화면에는 원래 낙인값이 나간다."""

    def test_화면_낙인은_반올림하지_않은_값이다(self):
        p = make(product_no="8901", ki=43, yield_rate=15.0)
        r = self.pool()[p.id]
        defense = [a for a in r["axes"] if a["name"] == "방어력"][0]
        self.assertIn("낙인 43%", defense["val"])
        self.assertNotIn("45", defense["val"])

    def test_상품의_낙인_필드가_안_바뀐다(self):
        p = make(product_no="8902", ki=47, yield_rate=15.0)
        self.pool()
        p.refresh_from_db()
        self.assertEqual(p.ki, 47)
