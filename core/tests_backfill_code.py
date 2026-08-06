"""backfill_product_code의 ELB·DLB 헛경보 예외 테스트.

'기존 product_code 점검'이 ISIN 형식이 아닌 코드를 '오염 의심'으로 찍는데,
운영에서 걸린 3건이 전부 ELB·DLB(원금보장형 파생결합사채)였다. 이들은 ISIN
체계가 달라서 그렇지 오염이 아니다. 예외를 만들되 **진짜 오염은 계속 잡혀야**
하므로, 그 경계를 여기서 못 박는다.

고정값은 2026-08-06 운영 DB(읽기 전용) 실측이다.
"""

import io
from datetime import date

from django.core.management import call_command
from django.test import TestCase

from core.management.commands.backfill_product_code import is_principal_protected
from core.models import HistoricalIssue, Product


class PrincipalProtectedDetectionTest(TestCase):
    """어디까지를 ELB·DLB로 볼 것인가 — 경계를 값으로 고정한다."""

    def test_운영에서_걸린_3건은_원금보장형으로_본다(self):
        # product_type이 'ELS'로 잘못 박혀 있다. 이름이 유일한 단서다.
        for name in ("신한투자-ELB-4118", "신한투자-ELB-4117", "신한투자-DLB-3902"):
            self.assertTrue(
                is_principal_protected("ELS", name, None), name)

    def test_이름_표기_변형을_모두_잡는다(self):
        for name in (
            "신한투자증권(ELB) 4101",
            "대신[Balance] ELB 180",
            "한화스마트ON  ELB 제160호 파생결합사채(주가연계파생결합사채)",
            "KB able DLB 제347호 기타파생결합사채",
            "BNK투자증권 제291호 기타파생결합사채(DLB)",
            "트루 파생결합사채(주가연계파생결합사채) 제2885회",  # 토큰 없이 한글만
            "삼성증권 제2944회 주가연계파생결합사채",           # 토큰 없이 한글만
        ):
            self.assertTrue(is_principal_protected("ELS", name, None), name)

    def test_이름이_회차번호뿐이면_product_type으로_판정한다(self):
        # 엑셀 수입분은 name이 '344'처럼 회차뿐이라 이름만으로는 못 가린다.
        self.assertTrue(is_principal_protected("ELB", "344", None))
        self.assertTrue(is_principal_protected("DLB", "147", None))

    # ── 여기부터가 핵심: 예외가 너무 넓어지면 안 된다 ──

    def test_낙인이_있으면_원금보장형이_아니다(self):
        """product_type='ELB'라도 ki가 있으면 진짜 ELS다 (운영 6건 실재)."""
        self.assertFalse(is_principal_protected("ELB", "교보증권K(ELS) 23", 40))
        self.assertFalse(is_principal_protected("ELB", "교보증권K(ELS) 31", 40))
        # 이름에 ELB가 있어도 ki가 박혀 있으면 예외로 빼지 않는다
        self.assertFalse(is_principal_protected("ELS", "무슨증권 ELB 제1호", 45))

    def test_평범한_ELS는_원금보장형이_아니다(self):
        for name in (
            "미래에셋증권 제38024회 파생결합증권",   # 증권 ≠ 사채
            "삼성증권 제31356회 주가연계증권",
            "BNK투자증권 제164호 파생결합증권(ELS)",
            "NH투자증권(ELS) 25072",
            "N2 ELS 400",
            "",                                      # 이름이 비어도 안전해야 한다
        ):
            self.assertFalse(is_principal_protected("ELS", name, None), name)

    def test_ELB가_다른_단어에_섞인_경우는_안_잡는다(self):
        # 영문자에 붙은 ELB/DLB는 다른 단어다
        self.assertFalse(is_principal_protected("ELS", "HELBO 제3호 파생결합증권", None))
        self.assertFalse(is_principal_protected("ELS", "ELBOW 인덱스 연계증권", None))


class AuditReportTest(TestCase):
    """커맨드 출력에서 ELB·DLB가 '오염 의심'과 분리돼 찍히는지."""

    def _run(self):
        out = io.StringIO()
        call_command("backfill_product_code", stdout=out, samples=0)
        return out.getvalue()

    def setUp(self):
        # SEIBro에 없는 코드만 만든다 → 전부 orphan으로 잡힌다
        Product.objects.create(
            issuer="신한투자증권", product_no="4118", product_code="BSH0009J5",
            name="신한투자-ELB-4118", product_type="ELS", ki=None,
            issue_date=date(2026, 8, 7), sub_end=date(2026, 8, 7))
        Product.objects.create(
            issuer="신한투자증권", product_no="3902", product_code="BSH0009J3",
            name="신한투자-DLB-3902", product_type="ELS", ki=None,
            issue_date=date(2026, 8, 7), sub_end=date(2026, 8, 8))
        # 진짜 오염 — ELS인데 코드가 ISIN이 아니다
        Product.objects.create(
            issuer="미래에셋증권", product_no="38024", product_code="GARBAGE1",
            name="미래에셋증권 제38024회 파생결합증권", product_type="ELS", ki=25,
            issue_date=date(2026, 8, 7), sub_end=date(2026, 8, 9))

    def test_ELB는_정상으로_진짜_오염은_오염으로_센다(self):
        text = self._run()
        self.assertIn("· ISIN 형식이 아님 1건", text)
        self.assertIn("· ELB·DLB 2건 — ISIN 체계가 다름, 정상", text)

    def test_진짜_오염만_형식이상_줄에_찍힌다(self):
        text = self._run()
        self.assertIn("형식이상 P", text)
        self.assertIn("GARBAGE1", text)
        # ELB 코드가 '형식이상' 줄에 섞이면 안 된다
        for line in text.splitlines():
            if "형식이상 P" in line:
                self.assertNotIn("BSH0009J", line)

    def test_예외로_뺀_것도_눈에_보이게_남는다(self):
        text = self._run()
        self.assertIn("ELB·DLB P", text)
        self.assertIn("BSH0009J5", text)
        self.assertIn("BSH0009J3", text)

    def test_product_code를_지우지_않는다(self):
        self._run()
        self.assertEqual(
            Product.objects.get(product_no="4118").product_code, "BSH0009J5")
        self.assertEqual(
            Product.objects.get(product_no="38024").product_code, "GARBAGE1")

    def test_버킷_합이_orphan_총계와_맞는다(self):
        # SEIBro에 실재하는 코드를 하나 넣어 orphan에서 빠지는지도 함께 본다
        HistoricalIssue.objects.create(
            isin="KR6SH0009J20", name="신한투자증권4119(공모/ELB)",
            issuer="신한투자증권", issue_date=date(2026, 7, 31),
            expiry_date=date(2029, 7, 31))
        Product.objects.create(
            issuer="신한투자증권", product_no="4119", product_code="KR6SH0009J20",
            name="신한투자증권(ELB) 4119", product_type="ELB",
            issue_date=date(2026, 7, 31), sub_end=date(2026, 7, 30))
        text = self._run()
        self.assertIn("채워진 상품 4건 중 SEIBro에 없는 코드 3건", text)
