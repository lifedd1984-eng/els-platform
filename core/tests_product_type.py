"""상품유형(ELS/DLS/ELB/DLB) 판정 테스트.

배경 (2026-08-06 운영 4,769건 실측)
  product_type이 양방향으로 틀려 있었다.
    · 이름이 명백히 ELB·DLB인데 'ELS'로 저장된 건이 79건
    · 반대로 'ELB'인데 ki=40이 박힌 진짜 ELS가 6건 (교보증권K(ELS) 19/23/27/31 등)
  원인은 셋이었다.
    ① 수집(scrape_kofia)이 **상품명을 아예 안 봤다** — 설명만 봤다
    ② 그 설명 검사가 '원금지급형'만 봐서 신한의 '원금**추가**지급형'을 놓쳤다
       (한글은 부분문자열이 연속하지 않는다). 같은 문자열을 parsers.extract_ki는
       제대로 읽고 있었다 — 두 코드가 같은 원문을 다르게 읽고 있었던 것이다.
    ③ reparse_products의 갱신 목록에 product_type이 없어 소급 교정이 불가능했다

여기서 지키려는 것
  ① 판정은 parsers 한 곳에만 있고 수집·엑셀수입·재파싱이 모두 그것을 부른다
  ② ki가 박힌 상품은 어떤 원금보장 문구가 있어도 ELS·DLS로 남는다
  ③ 재파싱이 product_type을 고치되 **다른 필드는 건드리지 않는다**
"""

from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core import parsers
from core.models import Product
from core.tests_kofia_assets import _row, _run_scrape


class ClassifyProductTypeTest(TestCase):
    """판정 경계를 운영 실측값으로 못 박는다."""

    def test_상품명이_ELB_DLB면_원금보장형이다(self):
        # 설명에는 아무 단서가 없다 — 이름이 유일한 근거인 실제 3건
        for name, want in (
            ("신한투자-ELB-4118", "ELB"),
            ("신한투자-ELB-4117", "ELB"),
            ("신한투자-DLB-3902", "DLB"),
        ):
            self.assertEqual(
                parsers.classify_product_type(
                    name, "스텝다운 (65-65-65-65-65-65) 월지급 하이파이브", None),
                want, name)

    def test_이름_표기_변형을_모두_잡는다(self):
        for name, want in (
            ("신한투자증권(ELB) 4101", "ELB"),
            ("대신[Balance] ELB 180", "ELB"),
            ("한화스마트ON  ELB 제160호 파생결합사채(주가연계파생결합사채)", "ELB"),
            ("트루 파생결합사채(주가연계파생결합사채) 제2885회", "ELB"),  # 토큰 없이 한글만
            ("삼성증권 제2944회 주가연계파생결합사채", "ELB"),
            ("KB able DLB 제347호 기타파생결합사채", "DLB"),
            ("BNK투자증권 제291호 기타파생결합사채(DLB)", "DLB"),
            ("하나증권 제2793회 파생결합사채(기타파생결합사채)", "DLB"),
            ("삼성증권 제5017회 기타파생결합증권", "DLS"),
            ("하나증권 제3833회 파생결합증권(기타파생결합증권)", "DLS"),
        ):
            self.assertEqual(parsers.classify_product_type(name, "", None), want, name)

    def test_낙인이_있으면_원금보장형이_아니다(self):
        """'(80%원금지급형)'은 부분보장일 뿐 ELB가 아니다 — 운영 6건이 여기 걸렸다."""
        desc = ("월지급식 스텝다운 낙인 / 3년만기, 6개월마다 조기상환 기회부여 / "
                "85-85-85-75-75-75 KI 40 월지급배리어 55/ 월 1.325% / (80%원금지급형)")
        for name in ("교보증권K(ELS) 23", "교보증권K(ELS) 19",
                     "교보증권K(ELS) 27", "교보증권K(ELS) 31", "11", "15"):
            self.assertEqual(parsers.classify_product_type(name, desc, 40), "ELS", name)
        # 이름에 ELB가 있어도 ki가 박혀 있으면 마찬가지다
        self.assertEqual(
            parsers.classify_product_type("무슨증권 ELB 제1호", "", 45), "ELS")

    def test_이름이_회차번호뿐이면_설명으로_판정한다(self):
        """엑셀 수입분은 name이 '3826'처럼 회차뿐이라 설명이 유일한 단서다."""
        self.assertEqual(
            parsers.classify_product_type("3826", "원금추가지급형, 만기 3개월", None), "ELB")
        self.assertEqual(
            parsers.classify_product_type(
                "8", "[온라인 청약 가능 상품] 6개월 만기상환형, 원금지급추구형", None), "ELB")
        self.assertEqual(
            parsers.classify_product_type(
                "1054", "3개월 만기, 국채1M(KRW 3m KTB) 연계 DLB", None), "DLB")
        self.assertEqual(
            parsers.classify_product_type(
                "5003", "3개월 만기, 국채1M(KRW 3m KTB) 연계 DLS", None), "DLS")

    def test_평범한_ELS는_그대로_ELS다(self):
        for name in (
            "미래에셋증권 제38024회 파생결합증권",   # 증권 ≠ 사채
            "삼성증권 제31356회 주가연계증권",
            "BNK투자증권 제164호 파생결합증권(ELS)",
            "NH투자증권(ELS) 25072",
            "N2 ELS 400",
            "",                                      # 이름이 비어도 안전해야 한다
        ):
            self.assertEqual(
                parsers.classify_product_type(name, "StepDown형[90-90/45KI]", 45),
                "ELS", name)

    def test_ELB가_다른_단어에_섞인_경우는_안_잡는다(self):
        for name in ("HELBO 제3호 파생결합증권", "ELBOW 인덱스 연계증권"):
            self.assertEqual(parsers.classify_product_type(name, "", None), "ELS", name)

    def test_None을_넣어도_안전하다(self):
        self.assertEqual(parsers.classify_product_type(None, None, None), "ELS")


class SharedRuleConsistencyTest(TestCase):
    """같은 설명 원문을 낙인 판정과 유형 판정이 같은 근거로 읽는지.

    예전엔 scrape_kofia가 '원금지급형'만, extract_ki가 '원금추가지급형'까지 보고
    있어 신한 ELB·DLB에서 둘의 답이 갈렸다. 목록을 한 곳으로 모은 뒤라
    '원금보장 문구가 있다 → 낙인 없다 → 원금보장형'이 항상 함께 성립해야 한다.
    """

    def test_원금보장_문구는_낙인부재와_원금보장형을_함께_의미한다(self):
        for word in parsers.PRINCIPAL_PROTECTED_WORDS:
            desc = f"{word}, 만기 3개월"
            self.assertEqual(parsers.extract_ki(desc), "NoKI", word)
            self.assertEqual(parsers.classify_product_type("3826", desc, None),
                             "ELB", word)

    def test_원금추가지급형이_원금지급형에_가려지지_않는다(self):
        """한글 부분문자열 함정 — 이 한 줄이 신한 ELB·DLB 전체를 좌우했다."""
        self.assertNotIn("원금지급형", "원금추가지급형")
        self.assertEqual(
            parsers.classify_product_type("4111", "원금추가지급형<br/>만기 3개월", None),
            "ELB")


class ScrapeKofiaProductTypeTest(TestCase):
    """수집 배치가 상품명을 보고 유형을 저장하는지."""

    def test_이름이_ELB면_ELB로_저장한다(self):
        _run_scrape([_row(
            issuer="신한투자증권", product_no="4118", product_code="KR6SH0009J50",
            name="신한투자-ELB-4118", max_loss=0.0,
            description="스텝다운 (65-65-65-65-65-65) 월지급 하이파이브")])
        self.assertEqual(
            Product.objects.get(product_code="KR6SH0009J50").product_type, "ELB")

    def test_이름이_DLB면_DLB로_저장한다(self):
        _run_scrape([_row(
            issuer="신한투자증권", product_no="3902", product_code="KR6SH0009J30",
            name="신한투자-DLB-3902", max_loss=0.0,
            description="원금추가지급형<br/>만기 3개월")])
        self.assertEqual(
            Product.objects.get(product_code="KR6SH0009J30").product_type, "DLB")

    def test_낙인이_있으면_ELS로_저장한다(self):
        _run_scrape([_row(
            issuer="교보증권", product_no="31", product_code="KR6KY0000310",
            name="교보증권K(ELS) 31", max_loss=-20.0,
            description="월지급식 스텝다운 낙인 / 85-85-85-75-75-75 KI 40 / (80%원금지급형)")])
        p = Product.objects.get(product_code="KR6KY0000310")
        self.assertEqual(p.product_type, "ELS")
        self.assertEqual(p.ki, 40)

    def test_평범한_ELS_수집_결과는_한_글자도_안_바뀐다(self):
        """유형 판정을 고쳐도 다른 필드에 손대지 않는다."""
        _run_scrape([_row()])
        p = Product.objects.get(product_code="KR6NH0006VC5")
        self.assertEqual(p.product_type, "ELS")
        self.assertEqual(p.assets_raw, "KOSDAQ150 Index")
        self.assertEqual(p.asset_type, "지수형")
        self.assertEqual(p.ki, 30)
        self.assertEqual(p.barriers_raw, [80, 80, 80, 80, 75, 70])
        self.assertEqual(p.currency, "KRW")


class ReparseProductTypeTest(TestCase):
    """소급 교정 — 이미 저장된 상품의 유형을 재파싱이 고치는지."""

    def _make(self, **kw):
        base = dict(
            issuer="신한투자증권", product_no="4118", name="신한투자-ELB-4118",
            product_type="ELS", ki=None, is_no_ki=True, yield_rate=5.1,
            assets_raw="KOSPI200 Index", asset_type="지수형",
            issue_date=date(2026, 8, 7), expiry_date=date(2029, 8, 7),
            sub_end=date(2026, 8, 6),
            description="스텝다운 (65-65-65-65-65-65) 월지급 하이파이브",
        )
        base.update(kw)
        return Product.objects.create(**base)

    def test_잘못_저장된_ELS를_ELB로_고친다(self):
        p = self._make()
        call_command("reparse_products", stdout=StringIO())
        p.refresh_from_db()
        self.assertEqual(p.product_type, "ELB")

    def test_낙인이_박힌_ELB를_ELS로_되돌린다(self):
        p = self._make(
            issuer="교보증권", product_no="31", name="교보증권K(ELS) 31",
            product_type="ELB", ki=40, is_no_ki=False,
            description=("월지급식 스텝다운 낙인 / 3년만기, 6개월마다 조기상환 기회부여 / "
                         "85-85-85-75-75-75 KI 40 월지급배리어 55/ 월 1.25% / "
                         "(80%원금지급형)"))
        call_command("reparse_products", stdout=StringIO())
        p.refresh_from_db()
        self.assertEqual(p.product_type, "ELS")
        self.assertEqual(p.ki, 40)

    def test_dry_run은_아무것도_저장하지_않는다(self):
        p = self._make()
        out = StringIO()
        call_command("reparse_products", "--dry-run", stdout=out)
        p.refresh_from_db()
        self.assertEqual(p.product_type, "ELS")
        self.assertIn("변경 예정", out.getvalue())

    def test_유형만_바뀌고_다른_필드는_그대로다(self):
        p = self._make(
            product_type="ELS", ki=None, is_no_ki=True,
            barrier_first=65, barrier_last=65,
            barriers_raw=[65, 65, 65, 65, 65, 65], period_months=6,
            description=("스텝다운 (65-65-65-65-65-65) 월지급 하이파이브 / "
                         "3년만기 6개월단위 조기상환"))
        before = {f: getattr(p, f) for f in (
            "ki", "is_no_ki", "barrier_first", "barrier_last", "barriers_raw",
            "period_months", "asset_type", "assets_raw", "yield_rate",
            "issue_date", "expiry_date", "sub_end", "description", "name")}
        call_command("reparse_products", stdout=StringIO())
        p.refresh_from_db()
        self.assertEqual(p.product_type, "ELB")
        for f, v in before.items():
            self.assertEqual(getattr(p, f), v, f)

    def test_이미_맞으면_변경으로_세지_않는다(self):
        self._make(product_type="ELB", barrier_first=65, barrier_last=65,
                   barriers_raw=[65, 65, 65, 65, 65, 65], period_months=6,
                   description=("스텝다운 (65-65-65-65-65-65) 월지급 하이파이브 / "
                                "3년만기 6개월단위 조기상환"))
        out = StringIO()
        call_command("reparse_products", "--dry-run", stdout=out)
        self.assertIn("전체 1건 중 0건", out.getvalue())
