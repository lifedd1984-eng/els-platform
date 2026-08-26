from datetime import date
from unittest.mock import patch

from django.test import TestCase

from core.management.commands.fix_missing_assets import auto_repair_product
from core.models import Product


class MissingAssetsAutoRepairTests(TestCase):
    def make_product(self):
        return Product.objects.create(
            issuer="NH투자증권", product_no="25104", name="NH투자증권(ELS) 25104",
            product_code="TEST25104", yield_rate=10, ki=30, is_no_ki=False,
            barriers_raw=[80, 80, 80, 80, 75, 70], period_months=6,
            assets_raw="", asset_type="", issue_date=date(2026, 9, 4),
            expiry_date=date(2029, 9, 4), sub_start=date(2026, 8, 25),
            sub_end=date(2026, 9, 3), description="원금비보장",
            prospectus_url="https://example.test/prospectus.pdf",
        )

    @patch("core.management.commands.fix_missing_assets.fetch_prospectus_text")
    @patch("core.market.resolve_ticker", return_value="229200.KS")
    def test_repairs_only_fully_validated_candidate(self, resolve_ticker, fetch_text):
        fetch_text.return_value = (
            "상품개요 항목 내용 원금비보장형 기초자산 KOSDAQ150 지수 "
            "모집총액 10,000,000,000원"
        )
        product = self.make_product()

        result = auto_repair_product(product)

        self.assertTrue(result["ok"])
        product.refresh_from_db()
        self.assertEqual(product.assets_raw, "KOSDAQ150 Index")
        self.assertEqual(product.asset_type, "지수형")
        resolve_ticker.assert_called_once_with("KOSDAQ150 Index")

    @patch("core.management.commands.fix_missing_assets.fetch_prospectus_text")
    @patch("core.market.resolve_ticker", return_value=None)
    def test_keeps_database_unchanged_when_ticker_is_unknown(self, resolve_ticker, fetch_text):
        fetch_text.return_value = (
            "상품개요 항목 내용 원금비보장형 기초자산 KOSDAQ150 지수 "
            "모집총액 10,000,000,000원"
        )
        product = self.make_product()

        result = auto_repair_product(product)

        self.assertFalse(result["ok"])
        self.assertIn("시세 매핑 없음", result["reason"])
        product.refresh_from_db()
        self.assertEqual(product.assets_raw, "")
        self.assertEqual(product.asset_type, "")
