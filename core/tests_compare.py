"""유사상품 비교 — 모수 필터·토글·게이지 계산·대상 건수 규칙.

여기서 못 박는 것
  ① 비교 모수 = 같은 주 청약 + 같은 자산유형 + **낙인 동일값**
     (±범위가 아니다. 조기상환 주기는 조건에서 빼고 표에만 적는다)
  ② '같은 기초자산만' 토글 — 기본 꺼짐, 켜면 기초자산이 하나라도 겹치는 것만
  ③ **막대 위치는 실제 값의 위치**다 — (값 − 최저) / (최고 − 최저).
     라벨("낮은 쪽 상위 N%")과 완전히 분리해서 계산해야 한다.
     목업 v1에서 라벨에 끌려 막대를 반대쪽에 그린 사고가 있었다.
  ④ 3건 미만이면 비교 버튼 자체가 안 나온다 (혼자 1등은 뜻이 없다)
  ⑤ 10건 미만이면 패널은 열되 참고용 문구를 띄운다
  ⑥ 세 축(수익률·1차 배리어·마지막 배리어)은 반드시 함께 나온다 —
     한 축만 보이면 수익률↔배리어 맞바꿈이 가려져 왜곡된다
"""

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase

from core.compare import (
    LOW_SAMPLE, MIN_PEERS, compare_context, peer_key, week_peer_counts,
)
from core.models import _RADAR_POOL_CACHE, Product

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())
NEXT_MONDAY = MONDAY + timedelta(days=7)

_SEQ = [0]


def make(**kw):
    """같은 주 마감 ELS 하나. 지정 안 한 값은 전부 같아서 필터만 시험하게 된다."""
    _SEQ[0] += 1
    base = dict(
        issuer="키움증권", product_no=str(1000 + _SEQ[0]),
        name="키움 ELS", product_type="ELS", yield_rate=30.0,
        ki=25, is_no_ki=False, barrier_first=85, barrier_last=65,
        barriers_raw=[85, 85, 80, 65], period_months=4,
        assets_raw="삼성전자, SK하이닉스", asset_type="종목형",
        sub_end=MONDAY + timedelta(days=2), currency="KRW",
    )
    base.update(kw)
    return Product.objects.create(**base)


class NoTickerMixin:
    """티커 해석은 학습 저장소를 읽는다 — 시험에서는 이름 그대로 쓰게 고정."""

    def setUp(self):
        super().setUp()
        _RADAR_POOL_CACHE.clear()
        self.addCleanup(_RADAR_POOL_CACHE.clear)
        # compare는 직접 import했고 배지 계산은 market 쪽을 본다 — 둘 다 막아야
        # 시험이 바깥 시세 조회로 새지 않는다.
        for path in ("core.compare.resolve_ticker", "core.market.resolve_ticker"):
            p = mock.patch(path, return_value=None)
            p.start()
            self.addCleanup(p.stop)


# ── ① 모수 필터 ────────────────────────────────────
class PeerFilterTest(NoTickerMixin, TestCase):
    def test_같은_주_같은_유형_낙인_동일값만_모수에_들어온다(self):
        target = make(yield_rate=30.0)
        same = make(yield_rate=31.0)                       # 조건 동일 → 포함
        same2 = make(yield_rate=32.0)                      # 조건 동일 → 포함
        make(ki=30)                                        # 낙인 다름 → 제외
        make(ki=24)                                        # 낙인 ±1도 제외 (범위가 아니다)
        make(asset_type="지수형", assets_raw="KOSPI200")   # 자산유형 다름 → 제외
        make(sub_end=NEXT_MONDAY + timedelta(days=2))      # 다음 주 → 제외
        make(sub_end=MONDAY - timedelta(days=1))           # 지난 주 → 제외
        make(is_no_ki=True, ki=None)                       # 노낙인 → 다른 무리
        make(product_type="ELB", yield_rate=8.0)           # 원금지급형은 목록에서 제외

        ctx = compare_context(target)
        ids = {r["p"].id for r in ctx["nearest"]}
        self.assertEqual(ctx["count"], 3)                  # 기준 상품 + same 둘
        self.assertEqual(ids, {target.id, same.id, same2.id})

    def test_기준_상품_자신도_모수에_들어간다(self):
        target = make()
        make(), make()
        ctx = compare_context(target)
        self.assertEqual(ctx["count"], 3)
        self.assertTrue(any(r["is_target"] for r in ctx["nearest"]))

    def test_조기상환_주기가_달라도_모수에서_빼지_않는다(self):
        """주기는 조건이 아니다 — 대신 표에 실제 주기를 적는다."""
        target = make(period_months=4)
        make(period_months=6)
        make(period_months=3)
        ctx = compare_context(target)
        self.assertEqual(ctx["count"], 3)
        periods = {r["period_text"] for r in ctx["nearest"]}
        self.assertEqual(periods, {"4개월", "6개월", "3개월"})

    def test_노낙인끼리는_한_무리로_묶인다(self):
        target = make(is_no_ki=True, ki=None)
        make(is_no_ki=True, ki=None)
        make(is_no_ki=True, ki=None)
        make(ki=25)                                        # 낙인 있는 상품은 남
        ctx = compare_context(target)
        self.assertEqual(ctx["count"], 3)
        self.assertEqual(ctx["ki_label"], "노낙인")

    def test_낙인값도_노낙인_표시도_없으면_비교가_성립하지_않는다(self):
        target = make(ki=None, is_no_ki=False)
        make(ki=None, is_no_ki=False)
        make(ki=None, is_no_ki=False)
        self.assertIsNone(peer_key(target))
        self.assertIsNone(compare_context(target))

    def test_모수_조회는_쿼리_한_번이면_된다(self):
        """같은 주 상품이 200건대다 — 상품마다 왕복하면 화면이 안 뜬다."""
        target = make()
        for _ in range(30):
            make()
        with self.assertNumQueries(1):
            ctx = compare_context(target)
            # 게이지·표 계산은 전부 메모리에서 끝나야 한다 (배지는 별도 캐시)
            [g["pos"] for g in ctx["gauges"]]
        self.assertEqual(ctx["count"], 31)


# ── ② 토글 ────────────────────────────────────────
class SameAssetsToggleTest(NoTickerMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.target = make(assets_raw="삼성전자, SK하이닉스")
        self.overlap = make(assets_raw="SK하이닉스, Micron")     # 하나 겹침
        self.overlap2 = make(assets_raw="Micron, 삼성전자")      # 하나 겹침
        self.other1 = make(assets_raw="Tesla, NVIDIA")
        self.other2 = make(assets_raw="Apple, Alphabet")

    def test_기본은_꺼짐이라_전체가_모수다(self):
        ctx = compare_context(self.target)
        self.assertFalse(ctx["same_assets"])
        self.assertEqual(ctx["count"], 5)

    def test_켜면_기초자산이_하나라도_겹치는_것만_남는다(self):
        ctx = compare_context(self.target, same_assets=True)
        self.assertTrue(ctx["same_assets"])
        self.assertFalse(ctx["too_few_same_assets"])
        self.assertEqual(ctx["count"], 3)                  # 기준 + 겹치는 둘
        ids = {r["p"].id for r in ctx["nearest"]}
        self.assertIn(self.overlap.id, ids)
        self.assertIn(self.overlap2.id, ids)
        self.assertNotIn(self.other1.id, ids)

    def test_토글을_켜서_3건_미만이_되면_줄_세우지_않는다(self):
        Product.objects.filter(id=self.overlap2.id).delete()
        ctx = compare_context(self.target, same_assets=True)
        self.assertTrue(ctx["too_few_same_assets"])
        self.assertEqual(ctx["gauges"], [])
        self.assertEqual(ctx["count"], 2)                  # 건수는 그대로 알려 준다
        self.assertEqual(ctx["total"], 4)                  # 토글을 끄면 4건
        self.assertFalse(ctx["low_sample"])                # 참고용 문구와 겹치지 않게

    def test_회사명_안의_쉼표는_구분자가_아니다(self):
        """market.split_assets 재사용 — 'Advanced Micro Devices, Inc.'는 한 종목."""
        t = make(assets_raw="Advanced Micro Devices, Inc.")
        make(assets_raw="Advanced Micro Devices, Inc.")
        make(assets_raw="Advanced Micro Devices, Inc., Tesla")
        make(assets_raw="Inc., NVIDIA")                    # 'Inc.'만으로는 안 겹쳐야
        ctx = compare_context(t, same_assets=True)
        self.assertEqual(ctx["count"], 3)


# ── ③ 게이지 — 백분위와 막대 위치 ────────────────
class GaugeTest(NoTickerMixin, TestCase):
    def _gauge(self, ctx, label):
        return next(g for g in ctx["gauges"] if g["label"] == label)

    def test_막대_위치는_값의_실제_위치다(self):
        """(값 − 최저) / (최고 − 최저). 값 70·최저 50·최고 90 → 50%."""
        target = make(barrier_last=70)
        make(barrier_last=50)
        make(barrier_last=90)
        g = self._gauge(compare_context(target), "마지막 배리어")
        self.assertEqual(g["pos"], 50.0)
        self.assertEqual(g["lo_text"], "50")
        self.assertEqual(g["hi_text"], "90")

    def test_값이_중앙값보다_크면_라벨이_무엇이든_막대는_오른쪽이다(self):
        """목업 v1이 틀렸던 자리 — '낮은 쪽 상위 N%' 라벨에 끌려 막대를 왼쪽에
        그렸는데, 값 70은 중앙값 65보다 커서 오른쪽이어야 했다."""
        target = make(barrier_last=70)
        for v in (60, 65, 65, 66, 90):
            make(barrier_last=v)
        g = self._gauge(compare_context(target), "마지막 배리어")
        # 값 70 · 최저 60 · 최고 90 → (70-60)/(90-60) = 33.3%
        self.assertEqual(g["pos"], 33.3)
        # 중앙값(65.5)의 위치는 18.3% — 막대는 그보다 확실히 오른쪽에 있어야 한다
        self.assertGreater(g["pos"], (65.5 - 60) / (90 - 60) * 100)
        # 라벨은 '낮은 쪽'(70보다 큰 값이 1건, 작은 값이 4건이므로 위쪽이 가깝다)이
        # 아니라 '높은 쪽'이 나와야 하고, 어느 쪽이든 막대 위치와 무관해야 한다
        self.assertTrue(g["rank_label"].startswith("높은 쪽 상위"))

    def test_수익률은_높을수록_유리해서_상위로_읽는다(self):
        target = make(yield_rate=40.0)
        for v in (20.0, 25.0, 30.0):
            make(yield_rate=v)
        g = self._gauge(compare_context(target), "제시 수익률")
        # 4건 중 자기보다 높은 것 0건 → 1위 → 상위 25%
        self.assertEqual(g["rank_label"], "상위 25%")
        self.assertEqual(g["tone"], "good")
        self.assertEqual(g["pos"], 100.0)
        self.assertEqual(g["note"], "4건 중 3건보다 높습니다.")

    def test_수익률이_낮은_쪽이면_하위로_읽고_경고색이다(self):
        target = make(yield_rate=20.0)
        for v in (30.0, 35.0, 40.0):
            make(yield_rate=v)
        g = self._gauge(compare_context(target), "제시 수익률")
        self.assertEqual(g["rank_label"], "하위 25%")
        self.assertEqual(g["tone"], "warn")
        self.assertEqual(g["pos"], 0.0)

    def test_배리어는_낮을수록_유리해서_방향이_뒤집힌다(self):
        target = make(barrier_first=90)
        for v in (70, 75, 80):
            make(barrier_first=v)
        g = self._gauge(compare_context(target), "1차 조기상환 배리어")
        self.assertEqual(g["rank_label"], "높은 쪽 상위 25%")
        self.assertEqual(g["tone"], "warn")          # 가장 높다 = 가장 불리하다
        self.assertIn("높을수록 첫 상환 문턱이 높습니다", g["note"])

    def test_배리어가_가장_낮으면_유리한_쪽이다(self):
        target = make(barrier_first=70)
        for v in (75, 80, 90):
            make(barrier_first=v)
        g = self._gauge(compare_context(target), "1차 조기상환 배리어")
        self.assertEqual(g["rank_label"], "낮은 쪽 상위 25%")
        self.assertEqual(g["tone"], "good")

    def test_백분위는_자기보다_유리한_건수_더하기_1을_모수로_나눈_값이다(self):
        target = make(yield_rate=37.02)
        # 자기보다 높은 것 4건 · 모수 35건 → 5/35 = 14%
        for v in (38.0, 39.0, 40.0, 41.0):
            make(yield_rate=v)
        for i in range(30):
            make(yield_rate=20.0 + i * 0.1)
        g = self._gauge(compare_context(target), "제시 수익률")
        self.assertEqual(g["n"], 35)
        self.assertEqual(g["pct"], 14)
        self.assertEqual(g["rank_label"], "상위 14%")

    def test_세_축이_항상_함께_나온다(self):
        """수익률만 떼어 보여주면 배리어와의 맞바꿈이 가려진다."""
        target = make()
        make(yield_rate=20.0, barrier_first=70, barrier_last=50)
        make(yield_rate=40.0, barrier_first=90, barrier_last=75)
        labels = [g["label"] for g in compare_context(target)["gauges"]]
        self.assertEqual(labels, ["제시 수익률", "1차 조기상환 배리어", "마지막 배리어"])

    def test_수익률도_1차_배리어도_높은_쪽이면_맞바꿈을_짚어_준다(self):
        target = make(yield_rate=40.0, barrier_first=90)
        make(yield_rate=20.0, barrier_first=70)
        make(yield_rate=25.0, barrier_first=75)
        g = self._gauge(compare_context(target), "1차 조기상환 배리어")
        self.assertIn("수익률이 높은 대신 이쪽이 불리합니다", g["note"])

    def test_전원이_같은_값이면_막대는_한가운데다(self):
        target = make(barrier_last=65)
        make(), make()
        g = self._gauge(compare_context(target), "마지막 배리어")
        self.assertEqual(g["pos"], 50.0)
        self.assertEqual(g["tone"], "flat")

    def test_값이_없는_지표는_게이지를_그리지_않는다(self):
        target = make(barrier_last=None)
        make(), make()
        labels = [g["label"] for g in compare_context(target)["gauges"]]
        self.assertNotIn("마지막 배리어", labels)
        self.assertIn("제시 수익률", labels)


# ── ④⑤ 대상 건수 규칙 ────────────────────────────
class SampleSizeTest(NoTickerMixin, TestCase):
    def test_3건_미만이면_비교_자체가_성립하지_않는다(self):
        target = make()
        make()                                             # 자기 포함 2건
        self.assertEqual(MIN_PEERS, 3)
        self.assertIsNone(compare_context(target))

    def test_딱_3건이면_열린다(self):
        target = make()
        make(), make()
        self.assertIsNotNone(compare_context(target))

    def test_10건_미만이면_참고용_문구가_붙는다(self):
        target = make()
        for _ in range(LOW_SAMPLE - 2):
            make()
        ctx = compare_context(target)
        self.assertEqual(ctx["count"], LOW_SAMPLE - 1)
        self.assertTrue(ctx["low_sample"])

    def test_10건_이상이면_참고용_문구가_없다(self):
        target = make()
        for _ in range(LOW_SAMPLE - 1):
            make()
        ctx = compare_context(target)
        self.assertEqual(ctx["count"], LOW_SAMPLE)
        self.assertFalse(ctx["low_sample"])


# ── 목록의 '비교 N' 버튼 ──────────────────────────
class ButtonTest(NoTickerMixin, TestCase):
    def test_한_쿼리로_주간_전체_건수를_센다(self):
        for _ in range(20):
            make()
        for _ in range(5):
            make(ki=30)
        with self.assertNumQueries(1):
            counts = week_peer_counts(MONDAY, MONDAY + timedelta(days=6))
        self.assertEqual(counts[("종목형", 25)], 20)
        self.assertEqual(counts[("종목형", 30)], 5)

    def test_3건_미만이면_목록에_버튼이_안_나온다(self):
        make(ki=25), make(ki=25), make(ki=25)              # 3건 → 버튼 있음
        make(ki=40), make(ki=40)                           # 2건 → 버튼 없음
        r = self.client.get("/weekly/")
        self.assertContains(r, "비교 3")
        self.assertNotContains(r, "비교 2")

    def test_필터로_좁혀도_비교_모수는_그_주_전체다(self):
        """화면 필터마다 백분위가 달라지면 순위에 뜻이 없어진다."""
        for _ in range(5):
            make(yield_rate=40.0)
        for _ in range(5):
            make(yield_rate=10.0)
        r = self.client.get("/weekly/", {"yield_min": "30"})
        self.assertContains(r, "비교 10")                  # 화면에는 5건만 남아도 10


# ── 화면 ──────────────────────────────────────────
class PanelViewTest(NoTickerMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.target = make(assets_raw="삼성전자, SK하이닉스", product_no="T1")
        self.overlap = make(assets_raw="SK하이닉스, Micron", product_no="OV1")
        self.overlap2 = make(assets_raw="Micron, 삼성전자", product_no="OV2")
        self.other = make(assets_raw="Tesla, NVIDIA", product_no="OTHER")

    def test_조각을_그대로_돌려준다(self):
        r = self.client.get(f"/product/{self.target.id}/compare/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "비슷한 조건 4건과 비교")
        self.assertContains(r, "조건이 가장 가까운")
        self.assertContains(r, "(지금 보는 상품)")

    def test_토글은_서버에서_다시_계산한다(self):
        r = self.client.get(f"/product/{self.target.id}/compare/", {"same": "1"})
        self.assertContains(r, "비슷한 조건 3건과 비교")
        self.assertContains(r, "OV1")
        self.assertNotContains(r, "OTHER")

    def test_토글로_3건_미만이_되면_되돌아갈_길을_알려_준다(self):
        Product.objects.filter(id=self.overlap2.id).delete()
        r = self.client.get(f"/product/{self.target.id}/compare/", {"same": "1"})
        self.assertContains(r, "3건 미만이라 줄 세울 수 없습니다")
        self.assertContains(r, "토글을 끄면 3건과 비교합니다")

    def test_상세_화면에_패널이_들어간다(self):
        r = self.client.get(f"/product/{self.target.id}/")
        self.assertContains(r, "유사상품 비교")
        self.assertContains(r, "비슷한 조건 4건과 비교")

    def test_비교가_안_되는_상품은_상세에도_카드가_없다(self):
        lonely = make(ki=45)
        r = self.client.get(f"/product/{lonely.id}/")
        self.assertNotContains(r, "유사상품 비교")

    def test_주소로_직접_들어와도_화면이_깨지지_않는다(self):
        lonely = make(ki=45)
        r = self.client.get(f"/product/{lonely.id}/compare/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "비교할 수 없습니다")


# ── 규제 문구 ─────────────────────────────────────
class DisclosureTest(NoTickerMixin, TestCase):
    def test_순위가_판단이_아니라는_고지가_항상_붙는다(self):
        target = make()
        make(), make()
        r = self.client.get(f"/product/{target.id}/compare/")
        self.assertContains(r, "순위는 <b>제시 조건을 줄 세운 것일 뿐</b>이며 "
                               "어느 상품이 더 낫다는 판단이 아닙니다.", html=False)
        self.assertContains(r, "실제 결과는 기초자산 가격으로 결정됩니다")

    def test_추천_최고_베스트_같은_표현을_쓰지_않는다(self):
        target = make()
        make(), make()
        body = self.client.get(f"/product/{target.id}/compare/").content.decode()
        for word in ("추천", "베스트", "최고의", "가장 좋은"):
            self.assertNotIn(word, body)
