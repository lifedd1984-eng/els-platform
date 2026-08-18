"""구글 시트 동기화 피드(/portfolio/sync.json) 테스트.

지켜야 할 것이 넷이다.
  ① 토큰 없이는 아무것도 안 나온다 (로그인 세션이 없는 자리라 자물쇠가 이것뿐)
  ② 지정 계정 것만 나온다 (다른 회원 투자가 새면 개인정보 사고)
  ③ 값이 엑셀 다운로드(portfolio_export)와 한 칸도 다르지 않다
  ④ 사람이 손으로 채우는 열(회사·비고)은 피드에 아예 실리지 않는다
"""

import json
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core import portfolio_export as pfx
from core import portfolio_sync as pfs
from core.models import Investment, Product

URL = "/portfolio/sync.json"
TOKEN = "test-token-for-unit-tests-only"


def _investment(user, **kw):
    amount = kw.pop("amount", 10_000_000)
    status = kw.pop("status", "보유중")
    kw.setdefault("product_type", "ELS")
    kw.setdefault("issuer", "테스트증권")
    kw.setdefault("product_no", str(30000 + Product.objects.count()))
    kw.setdefault("asset_type", "종목형")
    kw.setdefault("assets_raw", "Palantir   , Micron")
    kw.setdefault("yield_rate", 24.2)
    kw.setdefault("ki", 20)
    kw.setdefault("barrier_first", 80)
    kw.setdefault("barrier_last", 65)
    kw.setdefault("period_months", 6)
    kw.setdefault("barriers_raw", [80, 80, 75, 75, 70, 65])
    kw.setdefault("issue_date", date(2026, 4, 3))
    kw.setdefault("expiry_date", date(2029, 4, 5))
    kw.setdefault("description", "[스텝다운] 3년/6개월/80-80-75-75-70-65/KI20")
    product = Product.objects.create(**kw)
    return Investment.objects.create(
        user=user, product=product, amount=amount, status=status,
        invested_at=product.issue_date)


@override_settings(SHEET_SYNC_TOKEN=TOKEN, SHEET_SYNC_USERNAME="admin")
class TokenGateTest(TestCase):
    """자물쇠가 실제로 잠기는가."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="admin", password="x")
        _investment(self.owner)

    def test_no_token_is_404(self):
        self.assertEqual(self.client.get(URL).status_code, 404)

    def test_wrong_token_is_404(self):
        self.assertEqual(self.client.get(URL, {"token": "nope"}).status_code, 404)
        self.assertEqual(
            self.client.get(URL, headers={"x-sync-token": "nope"}).status_code, 404)

    def test_empty_token_value_is_404(self):
        self.assertEqual(self.client.get(URL, {"token": ""}).status_code, 404)

    def test_token_prefix_is_not_enough(self):
        """앞 글자만 맞는 토큰이 통과하면 한 글자씩 알아낼 수 있다."""
        self.assertEqual(self.client.get(URL, {"token": TOKEN[:-1]}).status_code, 404)

    def test_query_token_works(self):
        self.assertEqual(self.client.get(URL, {"token": TOKEN}).status_code, 200)

    def test_header_token_works(self):
        resp = self.client.get(URL, headers={"x-sync-token": TOKEN})
        self.assertEqual(resp.status_code, 200)

    @override_settings(SHEET_SYNC_TOKEN="")
    def test_endpoint_is_404_when_token_is_not_configured(self):
        """.env에 토큰을 안 넣은 서버에서는 주소 자체가 없어야 한다."""
        self.assertEqual(self.client.get(URL).status_code, 404)
        self.assertEqual(self.client.get(URL, {"token": TOKEN}).status_code, 404)

    def test_login_alone_does_not_open_it(self):
        """로그인했다고 열리지 않는다 — 토큰 전용 통로다."""
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(URL).status_code, 404)

    def test_response_is_not_cached_or_indexed(self):
        resp = self.client.get(URL, headers={"x-sync-token": TOKEN})
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertIn("noindex", resp["X-Robots-Tag"])


@override_settings(SHEET_SYNC_TOKEN=TOKEN, SHEET_SYNC_USERNAME="admin")
class ScopeTest(TestCase):
    """지정 계정 것만 나가는가."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="admin", password="x")
        self.other = User.objects.create_user(username="남", password="x")
        _investment(self.owner, issuer="키움증권", product_no="1806")
        _investment(self.other, issuer="남의증권", product_no="99999")

    def _body(self):
        resp = self.client.get(URL, headers={"x-sync-token": TOKEN})
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def _rows(self):
        return json.loads(self._body())["rows"]

    def test_only_the_configured_account_is_exported(self):
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["증권사"], "키움증권")
        body = self._body()
        self.assertNotIn("남의증권", body)
        self.assertNotIn("99999", body)

    @override_settings(SHEET_SYNC_USERNAME="남")
    def test_username_setting_switches_the_account(self):
        rows = self._rows()
        self.assertEqual([r["증권사"] for r in rows], ["남의증권"])

    @override_settings(SHEET_SYNC_USERNAME="없는계정")
    def test_unknown_account_yields_nothing_rather_than_everything(self):
        self.assertEqual(self._rows(), [])


@override_settings(SHEET_SYNC_TOKEN=TOKEN, SHEET_SYNC_USERNAME="admin")
class PayloadTest(TestCase):
    """피드 내용 — 엑셀과 같은 값인가, 사람 칸을 흘리지 않는가."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="admin", password="x")
        # 보유중·조기상환·만기상환·낙인후상환을 모두 깐다
        _investment(self.owner, issuer="삼성증권", product_no="30868", status="조기상환",
                    asset_type="지수형", assets_raw="KOSPI200 , S&P500 , Nikkei225",
                    yield_rate=20.0, ki=45, barrier_first=90, barrier_last=75,
                    period_months=3,
                    barriers_raw=[90, 90, 90, 90, 90, 90, 85, 85, 85, 80, 80, 75],
                    issue_date=date(2026, 4, 3), expiry_date=date(2029, 4, 2),
                    description="[스텝다운] 3년/3개월,45KI(90)%,세전 연 20%",
                    eval_dates=["2026-07-01", "2026-10-02", "2026-12-30", "2027-04-02",
                                "2027-07-02", "2027-10-01", "2027-12-30", "2028-03-31",
                                "2028-06-30", "2028-09-29", "2028-12-28", "2029-04-05"],
                    amount=30_000_000)
        _investment(self.owner, issuer="키움증권", product_no="1806", status="보유중",
                    amount=30_000_000)
        _investment(self.owner, issuer="키움증권", product_no="1827", status="만기상환",
                    description="[스텝다운리자드] 2년/4개월/80-80-75(L50)-75-70-60/KI35",
                    amount=10_000_000)
        _investment(self.owner, issuer="NH투자증권", product_no="306", status="낙인후상환",
                    amount=9_900_000)

    def _payload(self):
        resp = self.client.get(URL, headers={"x-sync-token": TOKEN})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        return json.loads(resp.content)

    def test_shape(self):
        data = self._payload()
        self.assertEqual(data["count"], 4)
        self.assertEqual(len(data["rows"]), data["count"])
        self.assertEqual(data["username"], "admin")
        self.assertEqual(data["columns"], pfs.COLUMN_NAMES)
        self.assertEqual(len(data["columns"]), 16)

    def test_key_columns_are_present_on_every_row(self):
        """(증권사, 상품번호)가 시트 행을 찾는 유일한 열쇠다."""
        for row in self._payload()["rows"]:
            for key in pfs.KEY_COLUMNS:
                self.assertTrue(str(row[key]).strip(), f"{key}가 비었다: {row}")

    def test_keys_are_unique(self):
        keys = [(r["증권사"], r["상품번호"]) for r in self._payload()["rows"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_manual_columns_never_leave_the_server(self):
        """회사·비고는 피드에 실리지 않는다. 실을 값이 없어야 못 덮어쓴다."""
        data = self._payload()
        self.assertNotIn("회사", data["columns"])
        self.assertNotIn("비고", data["columns"])
        for row in data["rows"]:
            self.assertNotIn("회사", row)
            self.assertNotIn("비고", row)
        # 비고에 들어갈 뻔한 값(L50)이 응답 어디에도 없어야 한다
        self.assertNotIn("L50", self.client.get(
            URL, headers={"x-sync-token": TOKEN}).content.decode())

    def test_values_match_the_excel_download_cell_by_cell(self):
        """엑셀(/portfolio/export/)과 같은 값인가 — 여기서 갈리면 시트가 어긋난다."""
        rows = self._payload()["rows"]
        excel = pfx.build_rows(Investment.objects.select_related("product"))
        self.assertEqual(len(rows), len(excel))
        for got, want in zip(rows, excel):
            for name, idx in pfs.SHEET_COLUMNS:
                self.assertEqual(got[name], want[idx],
                                 f"{got['증권사']} {got['상품번호']} 의 {name} 열이 어긋난다")

    def test_row_order_follows_the_excel_download(self):
        rows = self._payload()["rows"]
        excel = pfx.build_rows(Investment.objects.select_related("product"))
        self.assertEqual([(r["증권사"], r["상품번호"]) for r in rows],
                         [(e[0], e[1]) for e in excel])

    def test_both_holding_and_redeemed_are_included(self):
        """시트에 '완료' 행이 남아 있어서, 보유중만 보내면 상태가 영영 안 바뀐다."""
        statuses = {r["상환"] for r in self._payload()["rows"]}
        self.assertIn("", statuses)            # 보유중
        self.assertIn("완료", statuses)         # 조기상환
        self.assertIn("만기상환", statuses)
        self.assertIn("낙인후상환", statuses)

    def test_numbers_stay_numbers(self):
        """시트 계산식이 살아 있으려면 숫자가 문자열로 굳으면 안 된다."""
        row = next(r for r in self._payload()["rows"] if r["상품번호"] == "30868")
        self.assertEqual(row["발행일"], 20260403)
        self.assertEqual(row["투자월"], 202604)
        self.assertEqual(row["투자금액"], 3000)
        self.assertIsInstance(row["금리"], float)
        self.assertIsInstance(row["예상수익"], int)
        self.assertEqual(row["만기일"], "29.04.02")   # 만기일만 문자열 양식이다

    def test_korean_is_not_escaped(self):
        """\\uXXXX로 나가면 Apps Script는 읽지만 사람이 로그를 못 읽는다."""
        body = self.client.get(URL, headers={"x-sync-token": TOKEN}).content.decode()
        self.assertIn("키움증권", body)
