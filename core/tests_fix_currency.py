"""발행통화 보정 명령(fix_currency) 테스트.

배경 (2026-08-07)
  중복행 병합에서 남은 충돌 1건 — 신한투자증권 27659(id 2796)의 description은
  '… / 발행통화 : USD'인데 currency는 KRW였다. 지워진 짝 행이 USD였다.

  currency가 상품설명 문자열에서 파생되는 값이라(scrape_kofia·import_els),
  병합으로 설명이 뒤늦게 채워져도 파생값은 갱신되지 않는 것이 원인이다.

여기서 지키려는 것
  ① 근거는 SEIBro 등록통화(예탁결제원 원부)를 함께 본다 — 설명 문자열은
     currency를 만들어 낸 바로 그 값이라 혼자서는 근거가 되지 못한다
  ② 기본이 dry-run이다 — --apply 없이는 아무것도 쓰지 않는다
  ③ 지정한 값이 SEIBro와 어긋나면 저장하지 않는다
  ④ 인자 없이 돌리면 근거와 어긋난 상품만 잡힌다
"""

import itertools
from datetime import date
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from core import parsers
from core.management.commands.fix_currency import (
    extract_currency, extract_issue_currency, seibro_conflict,
)
from core.models import HistoricalIssue, Product

# 실제 충돌 건의 원문 (2026-08-07 운영 DB)
REAL_DESC = ("스텝다운 (90-90-90-90-85-85-85-85-80-80-80-50) NoKI, "
             "리자드1234차(75/70/60/45), 연20.1%, 만기 3년,"
             "조기상환 평가주기 3개월/ 발행통화 : USD")


def make_product(**kw):
    base = dict(
        issuer="신한투자증권", product_no="27659", name="27659", product_type="ELS",
        product_code="KR6SH0009A78", currency="KRW", description=REAL_DESC,
        assets_raw="KOSPI200 Index", asset_type="지수형",
        yield_rate=20.1, is_no_ki=True, sub_end=date(2026, 6, 23),
    )
    base.update(kw)
    return Product.objects.create(**base)


def make_issue(**kw):
    base = dict(
        isin="KR6SH0009A78", name="신한투자증권27659(공모/ELS)", issuer="신한투자증권",
        product_type="ELS", currency_name="외화", issue_date=date(2026, 6, 23),
        issue_amount=2359000,
    )
    base.update(kw)
    return HistoricalIssue.objects.create(**base)


def run(*args, **kw):
    out = StringIO()
    call_command("fix_currency", *args, stdout=out, stderr=out, **kw)
    return out.getvalue()


class ExtractCurrencyTest(TestCase):
    """① 근거 원문에서 통화를 읽어낸다."""

    def test_실제_충돌건의_설명에서_USD를_읽는다(self):
        code, evidence = extract_currency(REAL_DESC)
        self.assertEqual(code, "USD")
        self.assertIn("발행통화", evidence)

    def test_설명서_표기의_한글도_읽는다(self):
        self.assertEqual(extract_currency("발행통화 : 미국 달러")[0], "USD")
        self.assertEqual(extract_currency("발행 통화 원화")[0], "KRW")
        self.assertEqual(extract_currency("표시통화: JPY")[0], "JPY")

    def test_통화_표기가_없으면_읽지_않는다(self):
        for text in ("", "NoKI", "지수형", "스텝다운 (90-90-85) 40KI"):
            with self.subTest(text=text):
                self.assertEqual(extract_currency(text)[0], None)

    def test_통화와_무관한_문장을_통화로_읽지_않는다(self):
        self.assertEqual(extract_currency("환율 변동에 따라 손실이 발생할 수 있습니다")[0], None)


class SeibroConflictTest(TestCase):
    """① SEIBro 등록통화와의 어긋남 판정."""

    def test_외화인데_KRW면_충돌이다(self):
        self.assertTrue(seibro_conflict("KRW", "외화"))

    def test_원화인데_외화코드면_충돌이다(self):
        self.assertTrue(seibro_conflict("USD", "원화"))

    def test_맞으면_충돌이_아니다(self):
        self.assertFalse(seibro_conflict("USD", "외화"))
        self.assertFalse(seibro_conflict("JPY", "외화"))
        self.assertFalse(seibro_conflict("KRW", "원화"))

    def test_SEIBro_값이_없으면_판단하지_않는다(self):
        # 근거가 없는 것과 근거가 어긋나는 것은 다르다
        self.assertFalse(seibro_conflict("KRW", ""))
        self.assertFalse(seibro_conflict("USD", "알수없음"))


class DryRunTest(TestCase):
    """② 기본은 dry-run — 아무것도 쓰지 않는다."""

    def setUp(self):
        self.p = make_product()
        make_issue()

    def test_값을_지정해도_apply가_없으면_저장하지_않는다(self):
        out = run("--id", str(self.p.id), "--currency", "USD")
        self.p.refresh_from_db()
        self.assertEqual(self.p.currency, "KRW")
        self.assertIn("dry-run", out)

    def test_근거를_모두_보여준다(self):
        out = run("--id", str(self.p.id))
        self.assertIn("KR6SH0009A78", out)
        self.assertIn("외화", out)          # SEIBro 등록통화
        self.assertIn("2359000", out)       # 발행금액
        self.assertIn("발행통화 : USD", out)  # 설명 원문
        self.assertIn("USD", out)

    def test_SEIBro가_외화까지만_말한다는_것을_밝힌다(self):
        # 근거의 한계를 감추면 사람이 잘못 확정한다
        self.assertIn("어느 통화인지는 알려주지 않는다", run("--id", str(self.p.id)))


class ApplyTest(TestCase):
    """--apply — 근거가 맞을 때만 저장한다."""

    def setUp(self):
        self.p = make_product()
        make_issue()

    def test_저장한다(self):
        run("--id", str(self.p.id), "--currency", "USD", "--apply")
        self.p.refresh_from_db()
        self.assertEqual(self.p.currency, "USD")

    def test_다른_필드는_건드리지_않는다(self):
        before = Product.objects.values().get(id=self.p.id)
        run("--id", str(self.p.id), "--currency", "USD", "--apply")
        after = Product.objects.values().get(id=self.p.id)
        changed = {k for k in before if before[k] != after[k]}
        self.assertEqual(changed, {"currency"})

    def test_이미_같은_값이면_저장하지_않는다(self):
        Product.objects.filter(id=self.p.id).update(currency="USD")
        out = run("--id", str(self.p.id), "--currency", "USD", "--apply")
        self.assertIn("이미 같은 값", out)


class GuardTest(TestCase):
    """③ 근거와 어긋난 값은 막는다."""

    def test_외화_등록인데_KRW로_되돌리려_하면_막는다(self):
        p = make_product(currency="USD")
        make_issue()
        out = run("--id", str(p.id), "--currency", "KRW", "--apply")
        p.refresh_from_db()
        self.assertEqual(p.currency, "USD")
        self.assertIn("어긋납니다", out)

    def test_원화_등록인데_USD로_바꾸려_하면_막는다(self):
        p = make_product(description="지수형")
        make_issue(currency_name="원화")
        out = run("--id", str(p.id), "--currency", "USD", "--apply")
        p.refresh_from_db()
        self.assertEqual(p.currency, "KRW")
        self.assertIn("어긋납니다", out)

    def test_SEIBro_근거가_없으면_막지_않되_알린다(self):
        p = make_product(product_code="")
        out = run("--id", str(p.id), "--currency", "USD", "--apply")
        p.refresh_from_db()
        self.assertEqual(p.currency, "USD")
        self.assertIn("발행내역에서 찾지 못했습니다", out)

    def test_여러_건에_값을_지정하면_거절한다(self):
        make_product()
        make_product(product_no="27660", product_code="KR6SH0009A79")
        make_issue()
        make_issue(isin="KR6SH0009A79")
        with self.assertRaises(Exception):
            run("--currency", "USD", "--apply")


class ScanTest(TestCase):
    """④ 인자 없이 돌리면 근거와 어긋난 상품만 잡힌다."""

    def test_어긋난_상품만_대상이_된다(self):
        bad = make_product()                                     # 외화 등록 + KRW
        ok = make_product(product_no="27700", product_code="KR6SH0009B00", currency="USD")
        krw = make_product(product_no="27701", product_code="KR6SH0009B01", description="지수형")
        make_issue()
        make_issue(isin="KR6SH0009B00")
        make_issue(isin="KR6SH0009B01", currency_name="원화")

        out = run()
        self.assertIn("대상 1건", out)
        self.assertIn(f"[{bad.id}]", out)
        self.assertNotIn(f"[{ok.id}]", out)
        self.assertNotIn(f"[{krw.id}]", out)

    def test_어긋난_상품이_없으면_그렇게_말한다(self):
        make_product(currency="USD")
        make_issue()
        self.assertIn("어긋난 상품이 없습니다", run())

    def test_발행내역에_없는_상품은_대상이_아니다(self):
        # 근거가 없는 것을 충돌로 세면 수집 범위 밖 상품이 전부 딸려 나온다
        make_product()
        self.assertIn("어긋난 상품이 없습니다", run())


# ══════════════════════════════════════════════════════════════════
# 2026-08-11 — 오분류가 다시 생기는 자리를 막는다
#
#  55건은 전부 보정됐는데(운영 스냅샷 대조) 그 뒤 2건이 새로 생겼다.
#  수집 로직이 그대로였기 때문이다. 여기부터는 그 원인을 잡는 테스트다.
# ══════════════════════════════════════════════════════════════════

# 실측 설명서 원문 조각 (2026-08-11, 공백 정규화 후)
PDF_FOREIGN = ("기초자산 모 집 총 액 미화 오백만 달러(USD 5,000,000) "
               "1증권당 액면가액 미화 일천 달러(USD 1,000) "
               "1증권당 발행가액 미화 일천 달러(USD 1,000) 발행 수량 5,000증권")
PDF_KRW = ("1증권당 액면가액 10,000원 1증권당 발행가액 10,000원 "
           "발행 수량 1,000,000 증권 최소청약금액 1,000,000원")
# 원화 상품 설명서에 그대로 실린 위험등급 안내문 — 통화 낱말이 나열돼 있다
PDF_KRW_WITH_BOILERPLATE = (
    "4) 해외표준통화인 미국, 스위스, 영국, 일본, 캐나다의 법정통화 및 유로화, "
    "G2인 중국의 법정통화를 제외한 통화로 표시되는 경우 "
    "8. 미국 통화(USD)로 투자하는 증권은 원화를 달러화로 환전하여 투자 할 경우 "
    + PDF_KRW)


class ExtractIssueCurrencyTest(TestCase):
    """④ 설명서 액면가액 — 통화코드를 직접 준다."""

    def test_외화_설명서에서_USD를_읽는다(self):
        code, evidence = extract_issue_currency(PDF_FOREIGN)
        self.assertEqual(code, "USD")
        self.assertIn("액면가액", evidence)

    def test_원화_설명서에서_KRW를_읽는다(self):
        self.assertEqual(extract_issue_currency(PDF_KRW)[0], "KRW")

    def test_공백이_들어간_원_표기도_읽는다(self):
        # 삼성증권 표기 — '10,000 원'
        self.assertEqual(extract_issue_currency("1증권당 액면가액 10,000 원")[0], "KRW")

    def test_위험등급_안내문에_속지_않는다(self):
        # 예전 extract_currency는 이 문장들 때문에 원화 상품을 EUR·USD로 읽었다
        # (확정 원화 22건 중 2건 오탐). 액면가액만 보면 원화로 바로잡힌다.
        self.assertEqual(extract_issue_currency(PDF_KRW_WITH_BOILERPLATE)[0], "KRW")

    def test_상환조건_수식의_액면가액은_근거가_아니다(self):
        # 신영증권 표기 — '액면가액 x (100% + 7.07%) 지급'
        self.assertEqual(
            extract_issue_currency("액면가액 x (100% + 7.07%) 지급 (연 21.21%)")[0], None)

    def test_액면가액이_없으면_읽지_않는다(self):
        for text in ("", "원금비보장, 75-75-75/35 KI", "환율 변동에 따라 손실이 발생할 수 있습니다"):
            with self.subTest(text=text):
                self.assertEqual(extract_issue_currency(text)[0], None)

    def test_발행사별_표기_변형을_모두_읽는다(self):
        """설명서 1,004건 전수에서 확인된 표기 변형 (2026-08-11).

        코드만 쓰는 형태를 못 읽어 신한투자·KB 12건이 통째로 미검출이었다.
        """
        cases = [
            # 신한투자증권·KB증권 — 한글 금액 표기 없이 코드만
            ("1증권당 액면가액 USD 1,000 1증권당 발행가액 USD 1,000", "USD"),
            ("모 집 총 액 USD 10,000,000 1증권당 액면가액 USD 1,000", "USD"),
            # 신영증권 — 라벨이 '액면금액'
            ("1증권당 액면금액 미화 일십 달러(USD 10)", "USD"),
            ("1증권당 액면금액 10,000원", "KRW"),
            # 하나·NH·한국투자 — 한글 금액 + 괄호 코드
            ("1증권당 액면가액 미화 일천 달러(USD 1,000)", "USD"),
            # 원화 표기 변형
            ("1증권당 액면가액 10,000 원", "KRW"),
            ("1증권당 발행가액 100,000원", "KRW"),
        ]
        for text, expected in cases:
            with self.subTest(text=text[:40]):
                self.assertEqual(extract_issue_currency(text)[0], expected)


class CurrencyFromDescriptionTest(TestCase):
    """수집 단계 파생 — 설명이 밝힌 경우에만 정한다."""

    def test_발행통화_표기를_읽는다(self):
        self.assertEqual(parsers.currency_from_description(REAL_DESC), "USD")

    def test_달러상품_표기를_읽는다(self):
        # 유안타 372 실데이터
        self.assertEqual(parsers.currency_from_description(
            "만기 6개월, Digital형, 원금지급형, 달러상품"), "USD")

    def test_통화를_말하지_않으면_None이다(self):
        # ⚠ 예전 한 줄은 여기서 KRW로 단정했다. 외화 상품 55건이 그렇게 굳었다.
        for desc in (
            "EUROSTOXX50+NIKKEI225+S&P500, 3y/6m 80-80-80-80-75-65, Cpn=9.60%",   # 하나 17813
            "원금비보장, 75-75-75-75-75-75-70-70-60/35 KI, Guard 베리어=60(3차)",   # NH 25041
            "",
        ):
            with self.subTest(desc=desc[:30]):
                self.assertIsNone(parsers.currency_from_description(desc))

    def test_기초자산으로_쓰인_통화쌍은_근거가_아니다(self):
        # 환율연계 상품. 예전 한 줄은 'USD'만 보고 USD로 저장했다 (실데이터 8건).
        for desc in (
            "USD/KRW 매매기준율, 6m, 만기평가시 기초자산이 1000원 이상일 시 연 3.37% 지급",
            "[지점 청약 가능 상품] 3개월 만기상환형, 기초자산: USD/KRW 매매기준율",
            "기초자산: USD환율, 만기 6개월",
        ):
            with self.subTest(desc=desc[:30]):
                self.assertIsNone(parsers.currency_from_description(desc))


def _kofia_row(**kw):
    """KOFIA 응답 한 줄 (kofia_scraper.fetch_subscribing 반환 형식)."""
    base = dict(
        issuer="하나증권", product_no="17813", product_code="KR6HN0008MF3",
        name="하나증권 제17813회 파생결합증권(주가연계증권)",
        assets_raw="Euro Stoxx 50 Index/Nikkei225 Index/S&P500 Index",
        description="EUROSTOXX50+NIKKEI225+S&P500, 3y/6m 80-80-80-80-75-65, Cpn=9.60%",
        broker_url="", prospectus_url="", yield_rate=9.6, max_loss=-100.0,
        issue_date=date(2026, 8, 4), expiry_date=date(2029, 8, 8),
        sub_start=date(2026, 7, 27), sub_end=date(2026, 8, 4),
    )
    base.update(kw)
    return base


_seq = itertools.count()


def _run_scrape(rows):
    with mock.patch("core.kofia_scraper.fetch_subscribing", return_value=rows), \
            mock.patch("core.telegram.send_message"), \
            mock.patch("core.notify.notify_preset_matches"), \
            mock.patch("core.management.commands.scrape_kofia.timezone_today",
                       side_effect=lambda: f"c{next(_seq)}"):
        call_command("scrape_kofia", stdout=StringIO())


class ScrapeKofiaCurrencyTest(TestCase):
    """수집 배치가 확정된 발행통화를 되돌리지 않는다."""

    def test_설명이_통화를_말하지_않으면_신규는_KRW로_만든다(self):
        # 화면 배지·통화 필터가 빈 값을 다루지 않으므로 기본값은 그대로 KRW다
        _run_scrape([_kofia_row()])
        self.assertEqual(Product.objects.get(product_code="KR6HN0008MF3").currency, "KRW")

    def test_보정된_통화를_다음_배치가_되돌리지_않는다(self):
        """핵심 — 예전엔 매 배치 upsert가 currency를 KRW로 덮어썼다."""
        _run_scrape([_kofia_row()])
        Product.objects.filter(product_code="KR6HN0008MF3").update(currency="USD")

        _run_scrape([_kofia_row()])                      # 청약중이라 다시 수집된다
        self.assertEqual(Product.objects.get(product_code="KR6HN0008MF3").currency, "USD")

    def test_설명이_통화를_밝히면_그_값을_쓴다(self):
        _run_scrape([_kofia_row(description=REAL_DESC)])
        self.assertEqual(Product.objects.get(product_code="KR6HN0008MF3").currency, "USD")

    def test_다른_필드는_평소대로_갱신된다(self):
        # 통화만 예외로 두는 것이지 upsert 자체를 바꾼 것이 아니다
        _run_scrape([_kofia_row()])
        _run_scrape([_kofia_row(yield_rate=11.1)])
        p = Product.objects.get(product_code="KR6HN0008MF3")
        self.assertEqual(p.yield_rate, 11.1)
        self.assertEqual(p.barriers_raw, [80, 80, 80, 80, 75, 65])


class ProspectusCurrencyTest(TestCase):
    """설명서 파싱 배치가 발행통화를 확정한다 (추가 다운로드 없음)."""

    def _run(self, text):
        with mock.patch(
            "core.management.commands.parse_prospectus_dates.Command._fetch_text",
            return_value=(text, []),
        ):
            out = StringIO()
            call_command("parse_prospectus_dates", stdout=out, stderr=out)
            return out.getvalue()

    def test_외화_설명서를_읽어_통화를_바로잡는다(self):
        p = make_product(currency="KRW", description="EUROSTOXX50+NIKKEI225, 3y/6m",
                         prospectus_url="https://example.test/a.pdf")
        make_issue()                                     # SEIBro '외화'
        self._run(PDF_FOREIGN)
        p.refresh_from_db()
        self.assertEqual(p.currency, "USD")

    def test_원화_설명서면_그대로_둔다(self):
        p = make_product(currency="KRW", description="지수형",
                         prospectus_url="https://example.test/a.pdf")
        make_issue(currency_name="원화")
        self._run(PDF_KRW)
        p.refresh_from_db()
        self.assertEqual(p.currency, "KRW")

    def test_SEIBro와_어긋나면_저장하지_않는다(self):
        # 다른 차수의 설명서를 읽었을 때 통화가 통째로 뒤집히는 것을 막는다
        p = make_product(currency="USD", description="지수형",
                         prospectus_url="https://example.test/a.pdf")
        make_issue(currency_name="외화")
        out = self._run(PDF_KRW)
        p.refresh_from_db()
        self.assertEqual(p.currency, "USD")
        self.assertIn("상충", out)

    def test_액면가액이_없으면_아무것도_하지_않는다(self):
        p = make_product(currency="KRW", description="지수형",
                         prospectus_url="https://example.test/a.pdf")
        make_issue()
        self._run("최초기준가격평가일 : 2026년 06월 23일")
        p.refresh_from_db()
        self.assertEqual(p.currency, "KRW")
