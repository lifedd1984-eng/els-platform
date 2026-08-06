"""KOFIA 기초자산 결손(assets_raw 빈 값) 대응 테스트.

배경
  KOFIA 청약중 목록 API가 일부 상품의 기초자산(val8)을 빈 문자열로 내려준다.
  우리 파싱 문제가 아니라 소스 결손이다 — 2026-08-06 실측에서 청약중 425건 중
  NH투자증권 25034·25065 두 건이 그랬고, KOFIA 웹 화면에서도 그 칸이 비어 있었다.

여기서 지키려는 것
  ① 값이 정상인 상품의 수집 결과는 **한 글자도 달라지지 않는다**
  ② 손으로 보정해 둔 기초자산이 다음 배치에서 빈 값으로 되돌아가지 않는다
  ③ 설명서 추출기가 '다른 자산'을 자신 있게 내놓지 않는다 (틀리느니 보류)
  ④ 보정 명령은 --apply 없이는 아무것도 쓰지 않는다
"""

import itertools
from datetime import date
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from core.management.commands.fix_missing_assets import (
    extract_assets_from_prospectus,
    normalize_asset_token,
)
from core.models import Product

# ── 실제 간이투자설명서에서 뽑은 텍스트 조각 (2026-08-06 수집) ────────────
# NH투자증권 제25065회 — KOFIA가 기초자산을 빈 값으로 내려준 바로 그 상품.
NH_25065 = (
    "1. 상품개요 항 목 내 용 NH투자증권 제25065회 파생결합증권 종목명 "
    "(주가연계증권) 2등급(높은위험),원금비보장형 기초자산 KOSDAQ150 지수 "
    "모 집 총 액 10,000,000,000원 1증권당 액면가액 100,000원"
)

# NH투자증권 제25039회 — 다중 기초자산이라 표 칸이 접히면서
# 앞 두 자산이 '기초자산' 라벨보다 **먼저** 나온다.
NH_25039 = (
    "상품개요 항 목 내 용 NH투자증권 제25039회 파생결합증권 종목명 "
    "(주가연계증권) 1등급(매우높은위험),원금비보장형 삼성전자 보통주/ "
    "SK하이닉스 보통주/ 기초자산 Micron Technology Inc. (블룸버그 티커: MU UW) "
    "모 집 총 액 8,000,000,000원 1증권당 액면가액 100,000원"
)

# 교보증권 — 같은 표 안에 설명 문장이 '기초자산'으로 먼저 등장한다.
KYOBO = (
    "상품개요 항목 내용 교보증권 제445회 주가연계파생결합사채 종목명 "
    "(낮은위험, 5등급) 본 증권은 자본시장법 시행령 제2조제7호에서 정하는 고난도 "
    "금융투자상품에 해당하지 아니하며 만기 또는 자동조기상환 시 원금 이상을 "
    "지급하는 구조입니다. 그러나 기초자산 가격 변동 이외의 요인으로 추가 손실이 "
    "발생할 수 있습니다. 기초자산 KOSPI200 지수 모 집 총 액 3,000,000,000원"
)

# 상품개요 표가 없고 투자유의사항 본문만 있는 설명서 — 반드시 '보류'여야 한다.
PROSE_ONLY = (
    "5. 기초자산의 가격에 연계하여 증권의 수익률이 결정되므로 투자결정시 "
    "기초자산의 성격 및 과거 가격 추이, 기초자산 관련 경영환경 변화 등을 "
    "충분히 숙지하고 투자하시기 바랍니다."
)


class ProspectusExtractTest(TestCase):
    """설명서 '상품개요' 표에서 기초자산을 읽는다."""

    def test_단일_기초자산을_KOFIA_표기로_읽는다(self):
        got, evidence = extract_assets_from_prospectus(NH_25065)
        self.assertEqual(got, "KOSDAQ150 Index")
        self.assertIn("KOSDAQ150", evidence)

    def test_라벨_앞으로_접힌_자산도_빠짐없이_읽는다(self):
        got, _ = extract_assets_from_prospectus(NH_25039)
        self.assertEqual(got, "삼성전자/SK하이닉스/Micron Technology Inc.")

    def test_같은_표의_설명문장은_건너뛰고_진짜_칸을_읽는다(self):
        got, _ = extract_assets_from_prospectus(KYOBO)
        self.assertEqual(got, "KOSPI200 Index")

    def test_상품개요가_없으면_보류한다(self):
        got, _ = extract_assets_from_prospectus(PROSE_ONLY)
        self.assertIsNone(got)

    def test_빈_텍스트는_보류한다(self):
        self.assertEqual(extract_assets_from_prospectus(""), (None, ""))


class NormalizeTokenTest(TestCase):
    """설명서 표기 → KOFIA 표기.

    KOFIA는 지수를 '<이름> Index'로 주고 market.resolve_ticker가 그 접미사를
    벗겨 티커를 찾는다. 설명서 표기를 그대로 두면 티커가 안 잡혀 손실확률이 안 나온다.
    """

    def test_지수는_KOFIA의_Index_표기로_맞춘다(self):
        self.assertEqual(normalize_asset_token("KOSDAQ150 지수"), "KOSDAQ150 Index")
        self.assertEqual(normalize_asset_token("KOSPI200 지수"), "KOSPI200 Index")
        self.assertEqual(normalize_asset_token("NKY225"), "Nikkei225 Index")
        self.assertEqual(normalize_asset_token("EUROSTOXX50"), "Euro Stoxx 50 Index")

    def test_보통주_접미사와_티커_괄호를_떼어낸다(self):
        self.assertEqual(normalize_asset_token("SK하이닉스 보통주"), "SK하이닉스")
        self.assertEqual(
            normalize_asset_token("Micron Technology Inc. (블룸버그 티커: MU UW)"),
            "Micron Technology Inc.")

    def test_표_머리글과_쪽번호는_버린다(self):
        for noise in ("종목명", "항 목", "내 용", "- 3 -", "Page 12"):
            self.assertEqual(normalize_asset_token(noise), "", noise)

    def test_모르는_이름은_그대로_둔다(self):
        self.assertEqual(normalize_asset_token("국고채권 3개월 금리"), "국고채권 3개월 금리")

    def test_지수_티커가_실제로_잡힌다(self):
        """표기를 맞춘 값이 시세 매핑까지 통과하는지 — 여기서 끊기면 손실확률이 안 나온다."""
        from core import market
        self.assertEqual(market.resolve_ticker("KOSDAQ150 Index"), "229200.KS")


def _row(**kw):
    """KOFIA 응답 한 줄(kofia_scraper.fetch_subscribing 반환 형식)."""
    base = dict(
        issuer="NH투자증권", product_no="25065", product_code="KR6NH0006VC5",
        name="NH투자증권(ELS) 25065", assets_raw="KOSDAQ150 Index",
        description="원금비보장, 80-80-80-80-75-70/30 KI",
        broker_url="", prospectus_url="", yield_rate=20.2, max_loss=-100.0,
        issue_date=date(2026, 8, 12), expiry_date=date(2029, 8, 13),
        sub_start=date(2026, 8, 4), sub_end=date(2026, 8, 12),
    )
    base.update(kw)
    return base


_run_seq = itertools.count()


def _run_scrape(rows):
    """텔레그램·알림을 막고 scrape_kofia를 돌린다.

    ImportLog.filename은 초 단위 타임스탬프라 한 테스트에서 연속 실행하면
    유니크 제약에 걸린다 — 실행마다 다른 값을 주입한다.
    """
    with mock.patch("core.kofia_scraper.fetch_subscribing", return_value=rows), \
            mock.patch("core.telegram.send_message") as tg, \
            mock.patch("core.notify.notify_preset_matches"), \
            mock.patch("core.management.commands.scrape_kofia.timezone_today",
                       side_effect=lambda: f"t{next(_run_seq)}"):
        call_command("scrape_kofia", stdout=mock.MagicMock())
    return tg


class ScrapeKofiaAssetGuardTest(TestCase):
    """수집 배치가 기초자산 결손을 다루는 방식."""

    def test_정상_기초자산은_그대로_저장된다(self):
        """① 값이 정상인 상품의 수집 결과 불변 — 결손 대응 코드가 끼어들지 않는다."""
        _run_scrape([_row()])
        p = Product.objects.get(product_code="KR6NH0006VC5")
        self.assertEqual(p.assets_raw, "KOSDAQ150 Index")
        self.assertEqual(p.asset_type, "지수형")
        self.assertEqual(p.ki, 30)
        self.assertEqual(p.barriers_raw, [80, 80, 80, 80, 75, 70])

    def test_결손이면_빈_값으로_새로_만들고_경보한다(self):
        tg = _run_scrape([_row(assets_raw="")])
        p = Product.objects.get(product_code="KR6NH0006VC5")
        self.assertEqual(p.assets_raw, "")
        self.assertEqual(p.asset_type, "")
        sent = "\n".join(str(c) for c in tg.call_args_list)
        self.assertIn("기초자산 누락", sent)

    def test_경보는_설명서_링크와_보정명령을_알려준다(self):
        """예전 문구('KOFIA 웹에서 확인')는 헛걸음이었다 — 목록 자체가 빈 값이다."""
        tg = _run_scrape([_row(assets_raw="", prospectus_url="https://x/E25065_short.pdf")])
        sent = "\n".join(str(c) for c in tg.call_args_list)
        self.assertIn("https://x/E25065_short.pdf", sent)
        self.assertIn("fix_missing_assets", sent)
        self.assertIn("간이투자설명서", sent)

    def test_보정해_둔_값을_빈_응답이_덮어쓰지_않는다(self):
        """② product_code로 이어지는 행 — NH 25004에서 겪은 되돌림 방지."""
        _run_scrape([_row()])                       # 먼저 정상 수집
        _run_scrape([_row(assets_raw="")])          # 다음 배치는 빈 값
        p = Product.objects.get(product_code="KR6NH0006VC5")
        self.assertEqual(p.assets_raw, "KOSDAQ150 Index")
        self.assertEqual(p.asset_type, "지수형")

    def test_코드없는_레거시행의_보정값도_지켜진다(self):
        """② exe 소스(product_code 빈값) 행 — 예전 가드는 이 경로를 놓쳤다."""
        Product.objects.create(
            issuer="NH투자증권", product_no="25065", product_code="",
            name="NH투자증권(ELS) 25065", product_type="ELS",
            assets_raw="KOSDAQ150 Index", asset_type="지수형",
            sub_end=date(2026, 8, 12),
        )
        _run_scrape([_row(product_code="", assets_raw="")])
        self.assertEqual(Product.objects.count(), 1)
        p = Product.objects.get(product_no="25065")
        self.assertEqual(p.assets_raw, "KOSDAQ150 Index")

    def test_여러건이어도_정상건은_영향을_받지_않는다(self):
        rows = [
            _row(),
            _row(product_no="25066", product_code="KR6NH0006VC6",
                 name="NH투자증권(ELS) 25066", assets_raw=""),
            _row(product_no="25067", product_code="KR6NH0006VC7",
                 name="NH투자증권(ELS) 25067", assets_raw="삼성전자/SK하이닉스"),
        ]
        _run_scrape(rows)
        self.assertEqual(
            Product.objects.get(product_code="KR6NH0006VC5").asset_type, "지수형")
        self.assertEqual(
            Product.objects.get(product_code="KR6NH0006VC6").asset_type, "")
        self.assertEqual(
            Product.objects.get(product_code="KR6NH0006VC7").asset_type, "종목형")


class FixMissingAssetsCommandTest(TestCase):
    """보정 명령 — 기본은 dry-run, --apply에서만 쓴다."""

    def setUp(self):
        self.p = Product.objects.create(
            issuer="NH투자증권", product_no="25065", product_code="KR6NH0006VC5",
            name="NH투자증권(ELS) 25065", product_type="ELS",
            assets_raw="", asset_type="", ki=30, yield_rate=20.2,
            barriers_raw=[80, 80, 80, 80, 75, 70], period_months=6,
            sub_start=date(2026, 8, 4), sub_end=date(2026, 8, 12),
            description="원금비보장, 80-80-80-80-75-70/30 KI",
            sim_result={"available": False, "reason": "기초자산 정보 없음"},
        )

    def _call(self, *args, **kw):
        out = mock.MagicMock()
        call_command("fix_missing_assets", *args, no_fetch=True, stdout=out, **kw)
        return "\n".join(str(c) for c in out.write.call_args_list)

    def test_dry_run은_아무것도_쓰지_않는다(self):
        self._call("--id", str(self.p.id), "--assets", "KOSDAQ150 Index")
        self.p.refresh_from_db()
        self.assertEqual(self.p.assets_raw, "")
        self.assertEqual(self.p.asset_type, "")

    def test_apply는_기초자산과_유형을_채운다(self):
        self._call("--id", str(self.p.id), "--assets", "KOSDAQ150 Index", "--apply")
        self.p.refresh_from_db()
        self.assertEqual(self.p.assets_raw, "KOSDAQ150 Index")
        self.assertEqual(self.p.asset_type, "지수형")

    def test_apply는_낡은_시뮬결과를_비운다(self):
        """'기초자산 정보 없음'으로 남은 캐시가 그대로면 화면이 계속 '-'로 보인다."""
        self._call("--id", str(self.p.id), "--assets", "KOSDAQ150 Index", "--apply")
        self.p.refresh_from_db()
        self.assertIsNone(self.p.sim_result)
        self.assertIsNone(self.p.loss_prob)

    def test_유형을_못_정하는_값은_저장하지_않는다(self):
        self._call("--id", str(self.p.id), "--assets", "   ", "--apply")
        self.p.refresh_from_db()
        self.assertEqual(self.p.assets_raw, "")

    def test_대상이_없으면_조용히_끝난다(self):
        self.p.assets_raw = "KOSPI200 Index"
        self.p.save(update_fields=["assets_raw"])
        text = self._call()
        self.assertIn("빈 상품이 없습니다", text)

    def test_전체_점검은_빈_상품만_고른다(self):
        Product.objects.create(
            issuer="삼성증권", product_no="1", product_type="ELS",
            assets_raw="KOSPI200 Index", asset_type="지수형")
        text = self._call()
        self.assertIn("25065", text)
        self.assertNotIn("삼성증권 1 ", text)

    def test_공백뿐인_값도_결손으로_본다(self):
        self.p.assets_raw = "   "
        self.p.save(update_fields=["assets_raw"])
        text = self._call()
        self.assertIn("25065", text)
