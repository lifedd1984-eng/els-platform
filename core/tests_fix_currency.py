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

from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.management.commands.fix_currency import (
    extract_currency, seibro_conflict,
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
