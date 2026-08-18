"""청약이 한 건도 없는 주 — 빈 화면이 '서비스가 죽었다'로 읽히지 않게.

배경 (2026-08-18 실측)
  증권사 공시 기간 등이 겹치면 신규 ELS 청약이 한 주 내내 나오지 않는다.
  2026-08-17 주부터 3주 연속 0건이었다(직전 3주는 227·219·202건).
  그때 /weekly/ 화면은 이랬다.

    주간 청약 / 이번 주 청약 마감 상품 0건        [5일 전 데이터]
    [검색] [발행사 전체] [자산유형] [통화] [적용]
    이번 주 청약 마감 상품이 없습니다
    (끝)

  검색으로 처음 들어온 사람에게는 "서비스가 죽었나 / ELS가 없어졌나"로 읽힌다.

  ⚠ 배지가 "5일 전"에서 멈춘 이유는 수집 실패가 아니었다. KOFIA가 청약 0건을
    정상 응답(HTTP 200 · dbio_total_count_=0)으로 주면 scrape_kofia가
    ImportLog를 남기지 않고 돌아갔기 때문이다 — 배치는 매일 정상이었다.

여기서 지키려는 것
  ① 상품 0건인 주에는 안내 카드가 나온다 — 사유를 단정하지 않고, 갈 곳을 준다
  ② 시장 국면의 지수 위치는 상품 0건이어도 남는다 (상품과 무관하게 계산된다)
  ③ 통과율은 상품 0건이면 아예 안 나온다 — 0%로 찍지 않는다
  ④ 상품이 있는 주는 예전 그대로다
  ⑤ 배지가 '수집이 멈춘 것'과 '수집할 청약이 없는 것'을 구분한다
"""

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core import kofia_scraper
from core.models import _RADAR_POOL_CACHE, ImportLog, Product

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def make(**kw):
    """상품 하나. 기본은 이번 주 마감."""
    base = dict(
        issuer="키움증권", product_no=str(Product.objects.count() + 9000),
        name="키움 ELS", product_type="ELS",
        yield_rate=12.0, ki=25, is_no_ki=False, barrier_first=85, barrier_last=65,
        assets_raw="KOSPI200 Index", asset_type="지수형",
        sub_end=TODAY, currency="KRW", period_months=6,
    )
    base.update(kw)
    return Product.objects.create(**base)


def _series(first=100.0, peak=120.0, last=108.0, days=300):
    """오늘까지 이어지는 일별 종가 — 고점대비 90.0% / 직전 1년 +8.0%."""
    end = TODAY
    rows = [(end - timedelta(days=days - 1), first)]
    rows.append((end - timedelta(days=days // 2), peak))
    for i in range(days // 2 - 1, -1, -1):
        rows.append((end - timedelta(days=i), last))
    return rows


class GaugeMixin:
    """지수 시세를 붙인다 — 시장 국면의 '주요 지수 위치'가 나오게.

    다른 테스트(OfflineMixin)는 resolve_ticker를 None으로 막아 지수 표를 통째로
    비우지만, 여기서는 그 표가 남는지를 보는 게 목적이라 반대로 채워 준다.
    """

    def setUp(self):
        super().setUp()
        _RADAR_POOL_CACHE.clear()
        self.addCleanup(_RADAR_POOL_CACHE.clear)
        for target, value in (
            ("core.market.resolve_ticker", "^KS200"),
            ("core.market.fetch_history", _series()),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _get(self, **params):
        return self.client.get(reverse("weekly"), params)

    def _body(self, **params):
        return self._get(**params).content.decode()


class EmptyWeekGuideTest(GaugeMixin, TestCase):
    """① 상품 0건인 주 안내."""

    def setUp(self):
        super().setUp()
        # 2주 전에만 상품이 있다 — 이번 주는 0건
        self.old = [make(product_no=f"91{i}", sub_end=MONDAY - timedelta(days=14 - i))
                    for i in range(3)]

    def test_빈_주에는_안내_카드가_나온다(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.context["empty_week"])
        body = r.content.decode()
        self.assertIn("이 주에 청약이 마감되는 상품이 없습니다", body)
        # 예전 한 줄짜리 문구는 사라졌다
        self.assertNotIn("이번 주 청약 마감 상품이 없습니다", body)

    def test_사유를_단정하지_않는다(self):
        """왜 없는지는 우리가 모른다 — 예시로만 들고 모른다고 밝힌다."""
        body = self._body()
        self.assertIn("사유까지 확인한 것은 아닙니다", body)
        # "공시 기간이라서 없습니다" 식의 단정이 없어야 한다
        self.assertNotIn("공시 기간이라 ", body)
        self.assertNotIn("공시 기간이어서", body)

    def test_직전에_상품이_있던_주로_가는_링크가_붙는다(self):
        near = self._get().context["empty_week"]["near"]
        self.assertEqual(near["weeks"], 2)
        self.assertTrue(near["past"])
        self.assertEqual(near["offset"], -2)
        self.assertEqual(near["count"], 3)
        body = self._body()
        self.assertIn("2주 전 청약 보기", body)
        self.assertIn("w=-2", body)

    def test_대신_볼_곳을_준다(self):
        body = self._body()
        self.assertIn(reverse("report_els_10year"), body)   # 10년 성적표
        self.assertIn(reverse("trend"), body)               # 비로그인 대안

    def test_로그인하면_상환캘린더와_관심목록을_준다(self):
        from django.contrib.auth.models import User
        User.objects.create_user("empty_week_user", password="pw12345!")
        self.client.login(username="empty_week_user", password="pw12345!")
        body = self._body()
        self.assertIn(reverse("calendar"), body)
        self.assertIn(reverse("watchlist"), body)

    def test_필터_때문에_0건인_것과_구분한다(self):
        """그 주에 상품은 있는데 필터가 다 걸러낸 경우는 빈 주가 아니다."""
        make(product_no="9200", asset_type="지수형")
        r = self._get(asset="종목형")
        self.assertEqual(r.context["total"], 0)
        self.assertIsNone(r.context["empty_week"])
        body = r.content.decode()
        self.assertIn("고른 조건에 맞는 상품이 없습니다", body)
        self.assertNotIn("이 주에 청약이 마감되는 상품이 없습니다", body)

    def test_상품이_있는_주에는_안_뜬다(self):
        make(product_no="9300")
        r = self._get()
        self.assertIsNone(r.context["empty_week"])
        self.assertNotIn("이 주에 청약이 마감되는 상품이 없습니다", r.content.decode())

    def test_데이터보다_앞선_주면_뒤쪽_주를_가리킨다(self):
        """?w=-500 같은 링크로 들어와 과거 쪽이 비면 미래 쪽에서 찾는다."""
        near = self._get(w=-4).context["empty_week"]["near"]
        self.assertFalse(near["past"])
        self.assertEqual(near["weeks"], 2)
        self.assertEqual(near["offset"], -2)

    def test_상품이_하나도_없어도_500이_나지_않는다(self):
        Product.objects.all().delete()
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["empty_week"]["near"])
        self.assertIn("이 주에 청약이 마감되는 상품이 없습니다", r.content.decode())


class MarketRegimeEmptyWeekTest(GaugeMixin, TestCase):
    """②③ 시장 국면 — 지수 위치는 남고 통과율만 빠진다."""

    def test_상품이_0건이어도_지수_위치는_나온다(self):
        r = self._get()
        regime = r.context["regime"]
        self.assertIsNotNone(regime)
        self.assertTrue(regime["indexes"])
        self.assertEqual(regime["indexes"][0]["peak"], 90.0)
        self.assertEqual(regime["indexes"][0]["ret1y"], 8.0)
        body = r.content.decode()
        self.assertIn("시장 국면", body)
        self.assertIn("주요 지수 위치", body)
        self.assertIn("52주 고점 대비", body)

    def test_상품이_0건이면_통과율은_안_나온다(self):
        r = self._get()
        regime = r.context["regime"]
        self.assertIsNone(regime["rate"])       # 0%가 아니라 '없음'이다
        self.assertEqual(regime["n_all"], 0)
        body = r.content.decode()
        self.assertNotIn("이번 주 조건 통과율", body)   # 요약 줄
        self.assertNotIn("통과 조건", body)             # 통과율 칸의 조건 목록
        self.assertNotIn("상관 +0.52", body)            # 통과율 해설
        self.assertIn("조건 통과율을 낼 수 없습니다", body)

    def test_상품이_0건이면_종목형_기초자산_표는_빠진다(self):
        """그 주 상품에서 뽑는 표라 상품이 없으면 비는 게 맞다."""
        regime = self._get().context["regime"]
        self.assertEqual(regime["stocks"], [])
        self.assertNotIn("종목형 기초자산", self._body())

    def test_빈_주에는_카드가_펼쳐진_채로_나온다(self):
        self.assertIn('data-fold="regimeFold"', self._body())
        self.assertRegex(self._body(), r'data-fold="regimeFold"[^>]*open')

    def test_지수_시세도_없으면_카드를_접는다(self):
        """보여줄 게 하나도 없으면 예전처럼 카드를 통째로 뺀다."""
        with mock.patch("core.market.resolve_ticker", return_value=None):
            self.assertIsNone(self._get().context["regime"])


class MarketRegimeNormalWeekTest(GaugeMixin, TestCase):
    """④ 상품이 있는 주는 예전 그대로."""

    def setUp(self):
        super().setUp()
        make(product_no="9401", ki=25, barrier_first=85)
        make(product_no="9402", ki=95, barrier_first=99)   # 통과 못 함

    def test_통과율이_그대로_계산된다(self):
        regime = self._get().context["regime"]
        self.assertEqual(regime["n_all"], 2)
        self.assertEqual(regime["n_pass"], 1)
        self.assertEqual(regime["rate"], 50.0)

    def test_통과율_칸과_해설이_그대로_나온다(self):
        body = self._body()
        self.assertIn("이번 주 조건 통과율 50.0% (1/2건)", body)
        self.assertIn("통과 조건", body)
        self.assertIn("상관 +0.52", body)
        self.assertNotIn("조건 통과율을 낼 수 없습니다", body)

    def test_지수_위치도_함께_나온다(self):
        self.assertIn("주요 지수 위치", self._body())


class FreshnessBadgeTest(GaugeMixin, TestCase):
    """⑤ 배지 — '수집이 멈춤'과 '수집할 청약이 없음'을 구분한다."""

    def _log(self, days_ago, row_count):
        log = ImportLog.objects.create(
            filename=f"kofia_auto_test_{days_ago}_{row_count}",
            row_count=row_count, new_count=0)
        ImportLog.objects.filter(pk=log.pk).update(
            imported_at=log.imported_at - timedelta(days=days_ago))
        return log

    def test_수집한_게_있으면_평소_문구(self):
        self._log(0, 220)
        self.assertEqual(self._get().context["freshness"]["state"], "fresh")
        self.assertIn("오늘 업데이트", self._body())

    def test_청약이_0건이면_낡았다고_하지_않는다(self):
        """배치는 돌았고 수집할 청약이 없었을 뿐 — '데이터'가 아니라 '확인'이다."""
        self._log(0, 0)
        r = self._get()
        self.assertEqual(r.context["freshness"]["state"], "quiet")
        body = r.content.decode()
        self.assertIn("오늘 확인 · 청약중 0건", body)
        self.assertNotIn("일 전 데이터", body)
        self.assertNotIn("freshness warn", body)

    def test_며칠_지난_0건_실행도_경고색이_아니다(self):
        self._log(3, 0)
        body = self._body()
        self.assertIn("3일 전 확인 · 청약중 0건", body)
        self.assertNotIn("freshness warn", body)

    def test_수집_기록이_일주일_넘게_없으면_경고한다(self):
        """0건 실행이 이어져도 일주일을 넘기면 수집이 멈춘 것과 구분이 안 된다."""
        self._log(9, 0)
        r = self._get()
        self.assertEqual(r.context["freshness"]["state"], "stale")
        self.assertIn("freshness warn", r.content.decode())

    def test_수집_기록이_없으면_배지_자체가_없다(self):
        self.assertIsNone(self._get().context["freshness"])
        self.assertNotIn('class="freshness', self._body())

    def test_빈_주_안내가_수집_상태를_함께_말한다(self):
        self._log(0, 0)
        self.assertIn("자동 수집은 매일 실행됩니다", self._body())

    def test_수집이_멈췄으면_정상이라고_말하지_않는다(self):
        self._log(9, 0)
        self.assertNotIn("자동 수집은 매일 실행됩니다", self._body())


class KofiaEmptyResponseTest(TestCase):
    """수집 쪽 — 진짜 0건과 매핑 깨짐을 가르고, 0건도 실행 기록을 남긴다."""

    def _xml(self, total, items=""):
        return (
            '<?xml version="1.0" encoding="UTF-8"?><root><message>'
            f'<DISDlsListDTO><dbio_total_count_>{total}</dbio_total_count_>'
            f'</DISDlsListDTO>{items}</message></root>'
        )

    def _fetch(self, xml):
        resp = mock.Mock(status_code=200, text=xml)
        resp.raise_for_status = mock.Mock()
        with mock.patch("requests.post", return_value=resp):
            return kofia_scraper.fetch_subscribing()

    def test_청약_0건은_정상_응답으로_본다(self):
        # 2026-08-18 실측 응답 형태 — DISDlsDTO 0개 + total_count 0
        self.assertEqual(self._fetch(self._xml(0)), [])

    def test_건수는_있는데_한_건도_못_읽으면_실패로_올린다(self):
        """val 매핑이 바뀐 경우 — 여기서 조용히 넘기면 고장이 '0건'으로 위장된다."""
        with self.assertRaises(kofia_scraper.KofiaFetchError):
            self._fetch(self._xml(276))

    def test_건수_태그가_아예_없어도_실패로_본다(self):
        with self.assertRaises(kofia_scraper.KofiaFetchError):
            self._fetch('<?xml version="1.0"?><root><message></message></root>')

    def test_0건_실행도_수집_기록을_남긴다(self):
        """이 기록이 없으면 화면 배지가 마지막으로 상품이 있던 날에 멈춘다."""
        from django.core.management import call_command

        with mock.patch("core.kofia_scraper.fetch_subscribing", return_value=[]), \
                mock.patch("core.telegram.send_message") as send:
            call_command("scrape_kofia", verbosity=0)

        log = ImportLog.objects.first()
        self.assertIsNotNone(log)
        self.assertEqual(log.row_count, 0)
        self.assertEqual(log.new_count, 0)
        self.assertIn("청약 중인 상품 0건", send.call_args[0][0])

    def test_수집_실패에는_기록을_남기지_않는다(self):
        """실패까지 '정상 실행'으로 적으면 배지가 고장을 가린다."""
        from django.core.management import call_command

        err = kofia_scraper.KofiaFetchError("KOFIA 요청 실패")
        with mock.patch("core.kofia_scraper.fetch_subscribing", side_effect=err), \
                mock.patch("core.telegram.send_message"):
            call_command("scrape_kofia", verbosity=0)
        self.assertFalse(ImportLog.objects.exists())
