"""주간 청약 GET 파라미터 방어 테스트.

배경 (2026-08-07)
  ?preset=abc가 int 변환에서 ValueError를 내 /weekly/를 통째로 500으로 만들던 것을
  e1a0fcb에서 고쳤는데, 같은 자리가 셋 더 남아 있었다. 실측 응답 코드:

    ?w=abc / ?w=1.5 / ?w= / ?w=1e3 / ?w=0x10   500  int() ValueError
    ?w=999999                                  500  timedelta(weeks=…) OverflowError
    ?yield_min=abc                             500  float() ValueError
    ?ki_max=abc / ?ki_max=45.5                 500  int() ValueError

  w는 주소창을 직접 손대지 않아도 걸린다 — 주 이동 링크가 offset을 URL로 나르므로
  링크 하나만 깨져도 메인 화면이 통째로 죽는다.

여기서 지키려는 것
  ① 무효한 값에 500이 나지 않는다 — 조용히 기본값으로 되돌린다
     (w는 이번 주, 숫자 칸은 그 칸을 안 건 것으로)
  ② 무효한 값은 filters로도 공란이 된다 — 입력칸에 'abc'가 남아 있으면
     걸리지도 않은 조건이 걸린 것처럼 보인다
  ③ 정상값은 그대로 걸린다 — 방어를 넣다가 필터가 죽으면 안 된다
"""

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.models import _RADAR_POOL_CACHE, Product
from core.views import MAX_WEEK_OFFSET

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


class OfflineMixin:
    """시세 조회를 끊고 배지 캐시를 비운다 (tests_preset_personalization과 같은 이유)."""

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
        issuer="키움증권", product_no=str(Product.objects.count() + 8000),
        name="키움 ELS", product_type="ELS",
        yield_rate=12.0, ki=25, is_no_ki=False, barrier_first=85, barrier_last=65,
        assets_raw="KOSPI200 Index", asset_type="지수형",
        sub_end=TODAY, currency="KRW",
        issue_date=MONDAY + timedelta(days=3), period_months=6,
    )
    base.update(kw)
    return Product.objects.create(**base)


class WeekOffsetParamTest(OfflineMixin, TestCase):
    """① ?w — 무효하면 조용히 이번 주."""

    def setUp(self):
        super().setUp()
        make(product_no="8101")
        make(product_no="8102")

    def _get(self, **params):
        return self.client.get(reverse("weekly"), params)

    def test_무효한_값에_500이_나지_않고_이번_주로_돌아온다(self):
        # 2026-08-07 실측: 아래 값 전부가 /weekly/를 500으로 만들었다
        for bad in ("abc", "1.5", "", "1e3", "0x10", "None", "1,2", " ", "--1"):
            with self.subTest(w=bad):
                r = self._get(w=bad)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["offset"], 0)
                self.assertEqual(r.context["monday"], MONDAY)
                self.assertEqual(r.context["total"], 2)

    def test_범위를_벗어난_주차도_이번_주로_돌아온다(self):
        # int()는 통과하지만 timedelta(weeks=999999)가 date 범위를 넘겨 OverflowError
        for bad in ("999999", "-999999", str(MAX_WEEK_OFFSET + 1), str(-MAX_WEEK_OFFSET - 1)):
            with self.subTest(w=bad):
                r = self._get(w=bad)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["offset"], 0)

    def test_파라미터가_없으면_이번_주다(self):
        r = self._get()
        self.assertEqual(r.context["offset"], 0)
        self.assertEqual(r.context["monday"], MONDAY)

    def test_정상적인_주_이동은_그대로_동작한다(self):
        for raw, expect in (("1", 1), ("-1", -1), ("0", 0), (" 2 ", 2)):
            with self.subTest(w=raw):
                r = self._get(w=raw)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["offset"], expect)
                self.assertEqual(r.context["monday"], MONDAY + timedelta(weeks=expect))

    def test_경계값은_살려_둔다(self):
        # 한계 자체는 유효 — 자르는 자리가 한 칸 밀리지 않았는지 확인
        for raw in (MAX_WEEK_OFFSET, -MAX_WEEK_OFFSET):
            with self.subTest(w=raw):
                r = self._get(w=str(raw))
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["offset"], raw)

    def test_다음_주로_옮기면_이번_주_상품이_빠진다(self):
        # 방어를 넣다가 주 이동 자체가 죽지 않았는지
        self.assertEqual(self._get(w="1").context["total"], 0)


class NumericFilterParamTest(OfflineMixin, TestCase):
    """① ?ki_max·?yield_min — 무효하면 그 칸을 안 건 것으로."""

    def setUp(self):
        super().setUp()
        make(product_no="8201", ki=20, yield_rate=30.0)
        make(product_no="8202", ki=40, yield_rate=8.0)

    def _get(self, **params):
        return self.client.get(reverse("weekly"), params)

    def test_ki_max가_무효해도_500이_아니고_전체가_보인다(self):
        for bad in ("abc", "45.5", "1e2", "0x10", "None", " "):
            with self.subTest(ki_max=bad):
                r = self._get(ki_max=bad)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["total"], 2)

    def test_yield_min이_무효해도_500이_아니고_전체가_보인다(self):
        # nan·inf는 float()을 통과하지만 비교가 성립하지 않아 조용히 0건이 됐다
        for bad in ("abc", "nan", "inf", "-inf", "1,000", " "):
            with self.subTest(yield_min=bad):
                r = self._get(yield_min=bad)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["total"], 2)

    def test_무효한_값은_입력칸에_남지_않는다(self):
        # 'abc'가 칸에 남으면 걸리지도 않은 조건이 걸린 것처럼 보인다
        r = self._get(ki_max="abc", yield_min="nan")
        self.assertEqual(r.context["filters"]["ki_max"], "")
        self.assertEqual(r.context["filters"]["yield_min"], "")

    def test_정상값은_그대로_걸린다(self):
        self.assertEqual(self._get(ki_max="25").context["total"], 1)
        self.assertEqual(self._get(yield_min="15").context["total"], 1)
        self.assertEqual(self._get(ki_max="25", yield_min="15").context["total"], 1)

    def test_정상값은_입력칸에_남는다(self):
        r = self._get(ki_max="25", yield_min="15")
        self.assertEqual(r.context["filters"]["ki_max"], "25")
        self.assertEqual(r.context["filters"]["yield_min"], "15")

    def test_0도_유효한_조건이다(self):
        # '0'은 문자열로는 참이라 예전에도 걸렸다 — 방어를 넣으며 죽이지 않았는지
        self.assertEqual(self._get(ki_max="0").context["total"], 0)
        self.assertEqual(self._get(yield_min="0").context["total"], 2)

    def test_빈_값은_필터가_아니다(self):
        r = self._get(ki_max="", yield_min="")
        self.assertEqual(r.context["total"], 2)
        self.assertEqual(r.context["filters"]["ki_max"], "")

    def test_무효한_값을_한꺼번에_넣어도_화면이_뜬다(self):
        r = self._get(w="abc", ki_max="abc", yield_min="abc", preset="abc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["offset"], 0)
        self.assertEqual(r.context["total"], 2)
