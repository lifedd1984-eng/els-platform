"""간이투자설명서에서 조기상환 평가일·만기평가일을 읽는 파서 테스트.

왜 이 테스트가 있나
  평가일이 하루라도 어긋나면 조기상환 판정·알림이 통째로 어긋난다. SEIBro
  경로에서 이미 같은 종류의 사고가 났다(키움 1863: 계산 8/4 vs 실제 7/30).
  그래서 설명서 파서는 '확신 없으면 저장하지 않는다'가 원칙이고, 그 경계를
  실제 설명서 원문으로 여기서 못 박는다.

고정값의 출처
  본문 조각은 전부 실제 간이투자설명서 PDF에서 뽑은 원문이고(공백 정규화만 함),
  기대 평가일은 2026-08-10 운영 DB(읽기 전용)의 SEIBro 확정값이다.
  단 **마지막 회차만 다르다** — SEIBro는 조기상환 회차만 주고 만기평가일을
  안 줘서 backfill_eval_dates가 그 자리에 만기일(상환 지급일)을 넣어 왔다.
  실제 만기평가일은 설명서에 따로 적혀 있고 만기일보다 2~6영업일 빠르다.
"""

import io
from datetime import date
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from core.management.commands.parse_prospectus_dates import (
    extract_early_dates,
    extract_maturity_date,
    extract_schedule,
    needs_parse,
)
from core.models import Product


def _d(s):
    return date.fromisoformat(s)


def _dates(*iso):
    return [_d(s) for s in iso]


# ── 실제 설명서 원문 조각 (공백 정규화 후) ───────────────────────────────
# 신한투자증권 27859호 — 12배리어 차수표. 회차가 가장 많은 표본.
SHINHAN_27859 = (
    "최초기준가격 : 최초기준가격평가일 각 기초자산 종가 ｏ 최초기준가격평가일 : 2026년 08월 07일 "
    "ｏ 자동조기상환평가가격 : 해당 자동조기상환평가일 각 기초자산 종가 "
    "ｏ 자동조기상환평가일 및 상환금액: - 42 - 차수 자동조기상환평가일 상환금액 "
    "1차 2026년 11월 09일 액면금액 × 105.15% 2차 2027년 02월 05일 액면금액 × 110.30% "
    "3차 2027년 05월 07일 액면금액 × 115.45% 4차 2027년 08월 06일 액면금액 × 120.60% "
    "5차 2027년 11월 05일 액면금액 × 125.75% 6차 2028년 02월 07일 액면금액 × 130.90% "
    "7차 2028년 04월 28일 액면금액 × 136.05% 8차 2028년 08월 07일 액면금액 × 141.20% "
    "9차 2028년 11월 07일 액면금액 × 146.35% 10차 2029년 02월 07일 액면금액 × 151.50% "
    "11차 2029년 05월 02일 액면금액 × 156.65% "
    "ｏ 자동조기상환일 : 해당 자동조기상환평가일(불포함) 후 3 영업일 "
    "ｏ 만기평가가격 : 만기평가일 각 기초자산 종가 ｏ 만기평가일 : 2029년 08월 07일 "
    "ｏ 만기일 : 만기평가일(불포함) 후 3 영업일"
)

# 신한투자증권 27854호 — 조기상환표 바로 뒤에 월수익지급 36회차 표가 붙는다.
# 창을 안 끊으면 36개가 섞여 들어온다. 그걸 막는지 보는 표본.
SHINHAN_27854 = (
    "최초기준가격평가일 : 2026년 08월 07일 "
    "ｏ 자동조기상환평가가격 : 해당 자동조기상환평가일 각 기초자산 종가 "
    "ｏ 자동조기상환평가일 및 상환금액: - 178 - 차수 자동조기상환평가일 상환금액 "
    "1차 2027년 02월 05일 액면금액 × 100.00% 2차 2027년 08월 06일 액면금액 × 100.00% "
    "3차 2028년 02월 07일 액면금액 × 100.00% 4차 2028년 08월 07일 액면금액 × 100.00% "
    "5차 2029년 02월 07일 액면금액 × 100.00% "
    "ｏ 자동조기상환일 : 해당 자동조기상환평가일(불포함) 후 3 영업일 "
    "ｏ 월수익지급평가일(1차~36차) 회차 평가일자 회차 평가일자 회차 평가일자 "
    "1회 2026년 09월 04일 2회 2026년 10월 07일 3회 2026년 11월 09일 4회 2026년 12월 07일 "
    "5회 2027년 01월 07일 6회 2027년 02월 05일 35회 2029년 07월 06일 36회 2029년 08월 07일 "
    "ｏ월수익 지급일 : 해당 차수 월수익지급평가일 후 3 영업일 "
    "ｏ 만기평가가격 : 만기평가일 각 기초자산 종가 ｏ 만기평가일 : 2029년 08월 07일 "
    "ｏ 만기일 : 만기평가일(불포함) 후 3 영업일"
)

# KB증권 4496호 — 리자드. '1차 조기상환조건 (1) 충족시'처럼 날짜 없는 차수 줄이
# 표 안에 섞인다. 날짜가 바로 뒤에 붙은 줄만 회차로 세는지 보는 표본.
KB_4496 = (
    "최초기준가격평가일 : 2026년 08월 07일 "
    "ｏ 자동조기상환평가가격 : 해당 자동조기상환평가일 각 기초자산 종가 "
    "ｏ 자동조기상환평가일 및 상환금액: - 5 - 차수 자동조기상환평가일 조건 상환금액 "
    "1차 조기상환조건 (1) 충족시 액면금액 × 108.35% "
    "1차 2027년 02월 05일 1차 조기상환조건 (2) 충족시 액면금액 × 108.35% "
    "2차 조기상환조건 (1) 충족시 액면금액 × 116.70% "
    "2차 2027년 08월 06일 2차 조기상환조건 (2) 충족시 액면금액 × 116.70% "
    "3차 2028년 02월 07일 3차 조기상환조건 충족시 액면금액 × 125.05% "
    "4차 2028년 08월 07일 4차 조기상환조건 충족시 액면금액 × 133.40% "
    "5차 2029년 02월 07일 5차 조기상환조건 충족시 액면금액 × 141.75% "
    "ｏ 자동조기상환일 : 해당 자동조기상환평가일(불포함) 후 3 영업일 "
    "ｏ 만기평가가격 : 만기평가일 각 기초자산 종가 ｏ 만기평가일 : 2029년 08월 07일 "
    "ｏ 만기일 : 만기평가일(불포함) 후 3 영업일"
)

# KB증권 4486호 — 1년물 3회차. 표 중간에 쪽번호가 끼어 있다.
KB_4486 = (
    "최초기준가격평가일 : 2026년 08월 07일 "
    "ｏ 자동조기상환평가가격 : 해당 자동조기상환평가일 기초자산 종가 "
    "ｏ 자동조기상환평가일 및 상환금액: 차수 자동조기상환평가일 상환금액 "
    "1차 2026년 11월 06일 액면금액 × 104.675% 2차 2027년 02월 05일 액면금액 × 109.35% "
    "3차 2027년 05월 07일 액면금액 × 114.025% - 5 - "
    "ｏ 자동조기상환일 : 해당 자동조기상환평가일(불포함) 후 2 영업일 "
    "ｏ 만기평가가격 : 만기평가일 기초자산 종가 ｏ 만기평가일 : 2027년 08월 06일 "
    "ｏ 만기일 : 만기평가일(불포함) 후 2 영업일"
)

# NH투자증권 369호 — 괄호 번호 나열형.
NH_369 = (
    "최초기준가격평가일 : 2026년 07월 24일 "
    "ｏ 자동조기상환평가가격 : 자동조기상환평가일 각 기초자산 종가 "
    "ｏ 자동조기상환평가일 : (1) 2026년 10월 22일, (2) 2027년 01월 20일, "
    "(3) 2027년 04월 21일, (4) 2027년 07월 21일, (5) 2027년 10월 20일, "
    "(6) 2028년 01월 20일, (7) 2028년 04월 20일 "
    "ｏ 자동조기상환일 : 해당 차수 자동조기상환평가일(불포함) 후 2영업일 "
    "ｏ 만기평가가격 : 만기평가일 각 기초자산 종가 ｏ 만기평가일 : 2028년 07월 20일 "
    "ｏ 만기일 : 2028년 07월 24일"
)

# 삼성증권 31243호 — '중간기준가격' 나열 + '최종기준가격'이 만기평가일.
SAMSUNG_31243 = (
    "최초기준가격 : 2026년 07월 23일 종가 ○ 중간기준가격 : 2026년 10월 23일, "
    "2027년 01월 22일, 2027년 04월 23일, 2027년 07월 23일, 2027년 10월 22일, "
    "2028년 01월 21일, 2028년 04월 21일, 2028년 07월 21일, 2028년 10월 23일, "
    "2029년 01월 23일, 2029년 04월 23일 각 종가 ○ 최종기준가격 : 2029년 07월 23일 종가"
)

# 교보증권 21호 — 만기평가일에 콜론이 없는 표 형태.
KYOBO_21 = (
    "○ 자동조기상환평가일 및 상환금액 차수 자동조기상환평가일 조건 상환금액(세전) "
    "1차 2026년 11월 30일 1차 자동조기상환조건 충족시 액면금액 × 115.20% "
    "2차 2027년 03월 29일 2차 자동조기상환조건 충족시 액면금액 × 130.40% "
    "3차 2027년 07월 29일 3차 자동조기상환조건 충족시 액면금액 × 145.60% "
    "4차 2027년 11월 29일 4차 자동조기상환조건 충족시 액면금액 × 160.80% "
    "5차 2028년 03월 29일 5차 자동조기상환조건 충족시 액면금액 × 176.00% "
    "6차 2028년 07월 31일 6차 자동조기상환조건 충족시 액면금액 × 191.20% "
    "7차 2028년 11월 29일 7차 자동조기상환조건 충족시 액면금액 × 206.40% "
    "8차 2029년 03월 29일 8차 자동조기상환조건 충족시 액면금액 × 221.60% "
    "○ 자동조기상환일 : 해당 차수 자동조기상환평가일(불포함) 후 [2]영업일 다. 만기상환 "
    "① 모든 기초자산의 만기평가가격이 각 최초기준가격의 70% 이상인 경우 "
    "② 위 ①을 충족하지 못하고, 최초기준가격평가일 익일로부터 만기평가일(포함)까지 "
    "만기평가가격 만기평가일의 각 기초자산 종가 (현지거래소 기준) 만기평가일 2029년 07월 30일 "
    "○ 만기상환일 : 만기평가일(불포함) 후 [2]영업일"
)

# 키움증권 4126호 — 만기평가일이 3개(종가 산술평균). 마지막 날을 골라야 한다.
KIWOOM_4126 = (
    "° 자동조기상환평가일 : 차수 자동조기상환평가일 1차 2027년 01월 25일 "
    "2차 2027년 07월 23일 3차 2028년 01월 24일 4차 2028년 07월 24일 5차 2029년 01월 23일 "
    "° 자동조기상환일 : 해당 자동조기상환평가일(불포함) 후 2영업일 "
    "° 만기평가가격 : 만기평가일 각 기초자산 종가의 산술 평균 "
    "° 만기평가일 : 2029년 07월 19일, 2029년 07월 20일, 2029년 07월 23일"
)

# 메리츠증권 525호 — 리자드가 '3-1차 / 3-2차'로 갈리고 두 줄의 날짜가 같다.
MERITZ_525 = (
    "○ 자동조기상환평가일 : 차수 자동조기상환평가일 1차 2026년 11월 27일 - 6 - "
    "차수 자동조기상환평가일 2차 2027년 03월 25일 3-1차 2027년 07월 27일 "
    "3-2차 2027년 07월 27일 4차 2027년 11월 26일 5차 2028년 03월 27일 "
    "6차 2028년 07월 27일 7차 2028년 11월 27일 8차 2029년 03월 27일 "
    "○ 자동조기상환일 : 해당 자동조기상환평가일(불포함) 후 3영업일 "
    "○ 만기평가가격 : 만기평가일 기초자산 종가 ○ 만기평가일 : 2029년 07월 27일 "
    "○ 만기일 : 2029년 08월 01일 (만기평가일(불포함) 후 3영업일, 만기 상환금액 지급일)"
)

# 하나증권 17786호 — 리자드 병합셀. 본문에서는 '2-1차 액면금액 … 2027년 08월 02일'
# 처럼 날짜가 차수에서 떨어져 나가 못 읽는다. 표(extract_tables)로만 읽힌다.
HANA_17786_TEXT = (
    "○ 자동조기상환평가일 및 상환금액 : 차수 자동조기상환평가일 상환금액 "
    "1차 2027년 02월 01일 액면금액 ×113.55% 2-1차 액면금액 ×127.10% 2027년 08월 02일 "
    "2-2차 액면금액 ×127.10% 3차 2028년 01월 31일 액면금액 ×140.65% "
    "4차 2028년 07월 31일 액면금액 ×154.20% 5차 2029년 01월 31일 액면금액 ×167.75% "
    "○ 자동조기상환일 : 해당 차수 자동조기상환평가일(불포함) 후 3영업일 "
    "○ 만기평가가격 : 만기평가일 각 기초자산 종가 ○ 만기평가일 : 2029년 07월 27일 "
    "○ 만기일 : 2029년 08월 03일"
)
HANA_17786_TABLES = [[
    ["차수", "자동조기상환평가일", "상환금액"],
    ["1차", "2027년 02월 01일", "액면금액 ×113.55%"],
    ["2-1차", "2027년 08월 02일", "액면금액 ×127.10%"],
    ["2-2차", None, "액면금액 ×127.10%"],
    ["3차", "2028년 01월 31일", "액면금액 ×140.65%"],
    ["4차", "2028년 07월 31일", "액면금액 ×154.20%"],
    ["5차", "2029년 01월 31일", "액면금액 ×167.75%"],
]]

# 한화투자증권 9555호 — 표 칸 안에서 '2026년 11월 24 / 일'로 줄이 갈린다.
HANWHA_9555_TEXT = (
    "ｏ 자동조기상환평가일 및 상환금액: 자동조기상환평 차 수 상환금액 가일 "
    "2026년 11월 24 1차 액면금액 X 110.45% 일 2027년 03월 24 2차 액면금액 X 120.90% 일 "
    "ｏ 자동조기상환일 : 해당 차수 자동조기상환평가일(불포함) 후 [3]영업일 "
    "ｏ 만기평가가격 : 만기평가일 각 기초자산 종가 ｏ 만기평가일 : 2029년 07월 24일"
)
HANWHA_9555_TABLES = [[
    ["차 수", "자동조기상환평\n가일", "상환금액"],
    ["1차", "2026년 11월 24\n일", "액면금액 X 110.45%"],
    ["2차", "2027년 03월 24\n일", "액면금액 X 120.90%"],
    ["3차", "2027년 07월 26\n일", "액면금액 X 131.35%"],
    ["4차", "2027년 11월 24\n일", "액면금액 X 141.80%"],
    ["5차", "2028년 03월 24\n일", "액면금액 X 152.25%"],
    ["6차", "2028년 07월 24\n일", "액면금액 X 162.70%"],
    ["7차", "2028년 11월 24\n일", "액면금액 X 173.15%"],
    ["8차", "2029년 03월 26\n일", "액면금액 X 183.60%"],
]]


def _product(**kw):
    """저장하지 않는 Product 인스턴스 — extract_schedule은 필드만 읽는다."""
    kw.setdefault("issuer", "테스트증권")
    kw.setdefault("product_no", "1")
    return Product(**kw)


class CrossVerifiedProductTests(SimpleTestCase):
    """SEIBro 확정값이 있는 4개 상품 — 조기상환 회차가 한 날도 어긋나면 안 된다.

    기대값의 조기상환 회차는 SEIBro 확정값 그대로다. 마지막 한 칸만 다르다 —
    SEIBro는 그 자리에 만기일을 넣었고 설명서는 만기평가일을 적는다.
    """

    def test_신한_27859_12회차가_SEIBro와_같다(self):
        p = _product(barriers_raw=[75] * 6 + [70] * 6,
                     base_eval_date=_d("2026-08-07"), sub_end=_d("2026-08-07"),
                     expiry_date=_d("2029-08-10"))
        dates, why = extract_schedule(p, SHINHAN_27859)
        self.assertEqual(why, "차수표")
        self.assertEqual(dates, _dates(
            "2026-11-09", "2027-02-05", "2027-05-07", "2027-08-06", "2027-11-05",
            "2028-02-07", "2028-04-28", "2028-08-07", "2028-11-07", "2029-02-07",
            "2029-05-02", "2029-08-07"))

    def test_신한_27854_월수익_36회차가_섞이지_않는다(self):
        p = _product(barriers_raw=[95, 90, 85, 80, 75, 45],
                     base_eval_date=_d("2026-08-07"), sub_end=_d("2026-08-07"),
                     expiry_date=_d("2029-08-10"))
        dates, why = extract_schedule(p, SHINHAN_27854)
        self.assertEqual(why, "차수표")
        self.assertEqual(dates, _dates(
            "2027-02-05", "2027-08-06", "2028-02-07", "2028-08-07", "2029-02-07",
            "2029-08-07"))

    def test_KB_4496_리자드_조건줄을_회차로_세지_않는다(self):
        p = _product(barriers_raw=[90, 90, 85, 80, 75, 70],
                     base_eval_date=_d("2026-08-07"), sub_end=_d("2026-08-07"),
                     expiry_date=_d("2029-08-10"))
        dates, why = extract_schedule(p, KB_4496)
        self.assertEqual(why, "차수표")
        self.assertEqual(dates, _dates(
            "2027-02-05", "2027-08-06", "2028-02-07", "2028-08-07", "2029-02-07",
            "2029-08-07"))

    def test_KB_4486_3회차(self):
        p = _product(barriers_raw=[75, 70, 70, 65],
                     base_eval_date=_d("2026-08-07"), sub_end=_d("2026-08-07"),
                     expiry_date=_d("2027-08-10"))
        dates, why = extract_schedule(p, KB_4486)
        self.assertEqual(why, "차수표")
        self.assertEqual(dates, _dates(
            "2026-11-06", "2027-02-05", "2027-05-07", "2027-08-06"))

    def test_네_상품_모두_SEIBro_조기상환_회차와_완전히_같다(self):
        """만기 한 칸을 뺀 나머지는 SEIBro 확정값과 글자 그대로 같아야 한다."""
        cases = [
            (SHINHAN_27859, [75] * 6 + [70] * 6, "2029-08-10",
             ["2026-11-09", "2027-02-05", "2027-05-07", "2027-08-06", "2027-11-05",
              "2028-02-07", "2028-04-28", "2028-08-07", "2028-11-07", "2029-02-07",
              "2029-05-02"]),
            (SHINHAN_27854, [95, 90, 85, 80, 75, 45], "2029-08-10",
             ["2027-02-05", "2027-08-06", "2028-02-07", "2028-08-07", "2029-02-07"]),
            (KB_4496, [90, 90, 85, 80, 75, 70], "2029-08-10",
             ["2027-02-05", "2027-08-06", "2028-02-07", "2028-08-07", "2029-02-07"]),
            (KB_4486, [75, 70, 70, 65], "2027-08-10",
             ["2026-11-06", "2027-02-05", "2027-05-07"]),
        ]
        for text, bars, expiry, seibro_early in cases:
            p = _product(barriers_raw=bars, base_eval_date=_d("2026-08-07"),
                         sub_end=_d("2026-08-07"), expiry_date=_d(expiry))
            dates, _ = extract_schedule(p, text)
            self.assertEqual([d.isoformat() for d in dates[:-1]], seibro_early)
            # 마지막 칸은 만기일이 아니라 만기평가일이어야 한다
            self.assertLess(dates[-1], _d(expiry))


class FormatCoverageTests(SimpleTestCase):
    """발행사별 표기 포맷 — 하나라도 깨지면 그 발행사 전체가 근사로 남는다."""

    def test_괄호나열형_NH(self):
        p = _product(barriers_raw=[75, 75, 75, 70, 70, 70, 65, 55],
                     base_eval_date=_d("2026-07-24"), expiry_date=_d("2028-07-24"))
        dates, why = extract_schedule(p, NH_369)
        self.assertEqual(why, "괄호나열")
        self.assertEqual(dates, _dates(
            "2026-10-22", "2027-01-20", "2027-04-21", "2027-07-21", "2027-10-20",
            "2028-01-20", "2028-04-20", "2028-07-20"))

    def test_중간기준가격형_삼성(self):
        p = _product(barriers_raw=[90] * 6 + [85] * 3 + [80, 80, 75],
                     base_eval_date=_d("2026-07-23"), expiry_date=_d("2029-07-25"))
        dates, why = extract_schedule(p, SAMSUNG_31243)
        self.assertEqual(why, "중간기준가격")
        self.assertEqual(dates[0], _d("2026-10-23"))
        # 삼성은 만기평가일이 아니라 '최종기준가격'으로 적는다
        self.assertEqual(dates[-1], _d("2029-07-23"))

    def test_콜론_없는_만기평가일_교보(self):
        p = _product(barriers_raw=[85, 80, 80, 80, 75, 75, 75, 70, 70],
                     base_eval_date=_d("2026-07-29"), expiry_date=_d("2029-08-01"))
        dates, why = extract_schedule(p, KYOBO_21)
        self.assertEqual(why, "차수표")
        self.assertEqual(dates[-1], _d("2029-07-30"))

    def test_리자드_같은날짜_메리츠(self):
        p = _product(barriers_raw=[65, 65, 65, 65, 65, 60, 60, 55, 50],
                     base_eval_date=_d("2026-07-27"), expiry_date=_d("2029-08-01"))
        dates, why = extract_schedule(p, MERITZ_525)
        self.assertEqual(why, "차수표+리자드")
        self.assertEqual(dates, _dates(
            "2026-11-27", "2027-03-25", "2027-07-27", "2027-11-26", "2028-03-27",
            "2028-07-27", "2028-11-27", "2029-03-27", "2029-07-27"))

    def test_리자드_병합셀은_표로_읽는다_하나(self):
        p = _product(barriers_raw=[75, 75, 75, 75, 70, 60],
                     base_eval_date=_d("2026-07-28"), expiry_date=_d("2029-08-03"))
        # 본문만으로는 2회차 날짜를 가릴 수 없어 저장하지 않는다
        self.assertIsNone(extract_schedule(p, HANA_17786_TEXT)[0])
        dates, why = extract_schedule(p, HANA_17786_TEXT, HANA_17786_TABLES)
        self.assertEqual(why, "표추출")
        self.assertEqual(dates, _dates(
            "2027-02-01", "2027-08-02", "2028-01-31", "2028-07-31", "2029-01-31",
            "2029-07-27"))

    def test_표에서_날짜가_줄바꿈으로_갈린_한화(self):
        p = _product(barriers_raw=[85, 80, 80, 75, 75, 75, 70, 60, 50],
                     base_eval_date=_d("2026-07-24"), expiry_date=_d("2029-07-29"))
        self.assertIsNone(extract_schedule(p, HANWHA_9555_TEXT)[0])
        dates, why = extract_schedule(p, HANWHA_9555_TEXT, HANWHA_9555_TABLES)
        self.assertEqual(why, "표추출")
        self.assertEqual(dates, _dates(
            "2026-11-24", "2027-03-24", "2027-07-26", "2027-11-24", "2028-03-24",
            "2028-07-24", "2028-11-24", "2029-03-26", "2029-07-24"))


class MaturityDateRuleTests(SimpleTestCase):
    """만기평가일이 여러 날 나열되면 가장 늦은 날을 쓴다."""

    def test_여러_날짜면_마지막_날(self):
        self.assertEqual(extract_maturity_date(KIWOOM_4126), _d("2029-07-23"))

    def test_키움_4126_전체_스케줄(self):
        p = _product(barriers_raw=[85, 85, 85, 80, 75, 70],
                     base_eval_date=_d("2026-07-23"), expiry_date=_d("2029-07-25"))
        dates, _ = extract_schedule(p, KIWOOM_4126)
        self.assertEqual(dates, _dates(
            "2027-01-25", "2027-07-23", "2028-01-24", "2028-07-24", "2029-01-23",
            "2029-07-23"))

    def test_만기평가일이_본문에만_있고_날짜가_없으면_못_읽는다(self):
        text = ("ｏ 자동조기상환평가일 : 차수 자동조기상환평가일 1차 2027년 01월 25일 "
                "ｏ 자동조기상환일 : 뒤 2영업일 "
                "최초기준가격평가일(불포함)로부터 만기평가일(포함)까지 관찰한다")
        self.assertIsNone(extract_maturity_date(text))


class RejectionTests(SimpleTestCase):
    """확신 없는 값은 저장하지 않는다 — 커버리지보다 안전이 먼저다."""

    def test_회차수가_배리어수와_다르면_저장하지_않는다(self):
        p = _product(barriers_raw=[90, 80, 70],       # 4가 아니라 3
                     base_eval_date=_d("2026-08-07"), expiry_date=_d("2027-08-10"))
        dates, why = extract_schedule(p, KB_4486)
        self.assertIsNone(dates)
        self.assertIn("배리어", why)

    def test_배리어가_없으면_대상이_아니다(self):
        p = _product(barriers_raw=None, expiry_date=_d("2027-08-10"))
        self.assertEqual(extract_schedule(p, KB_4486), (None, "배리어 없음"))

    def test_회차_번호가_비면_저장하지_않는다(self):
        text = ("ｏ 자동조기상환평가일 : 차수 자동조기상환평가일 1차 2027년 01월 25일 "
                "3차 2028년 01월 24일 ｏ 자동조기상환일 : 뒤 2영업일 "
                "ｏ 만기평가일 : 2028년 07월 24일")
        dates, why = extract_early_dates(text)
        self.assertIsNone(dates)
        self.assertIn("불연속", why)

    def test_리자드_날짜가_어긋나면_저장하지_않는다(self):
        text = ("○ 자동조기상환평가일 : 차수 자동조기상환평가일 1차 2026년 11월 27일 "
                "2-1차 2027년 03월 25일 2-2차 2027년 03월 26일 "
                "○ 자동조기상환일 : 뒤 3영업일 ○ 만기평가일 : 2027년 07월 27일")
        self.assertEqual(extract_early_dates(text), (None, "리자드 포맷 미지원"))

    def test_만기평가일이_만기일보다_한참_앞이면_저장하지_않는다(self):
        # 만기평가일 2027-08-06인데 만기일이 2027-12-31이면 다른 상품을 읽은 것이다
        p = _product(barriers_raw=[75, 70, 70, 65],
                     base_eval_date=_d("2026-08-07"), expiry_date=_d("2027-12-31"))
        dates, why = extract_schedule(p, KB_4486)
        self.assertIsNone(dates)
        self.assertIn("간격 이상", why)

    def test_1회차가_기준일보다_앞서면_저장하지_않는다(self):
        p = _product(barriers_raw=[75, 70, 70, 65],
                     base_eval_date=_d("2027-01-01"), expiry_date=_d("2027-08-10"))
        dates, why = extract_schedule(p, KB_4486)
        self.assertIsNone(dates)
        self.assertIn("이전", why)

    def test_삼성_월수익형은_회차수가_안_맞아_저장하지_않는다(self):
        # 중간기준가격이 35개(매월)인데 조기상환은 6회차뿐인 상품 — 골라내는 규칙을
        # 세우지 않았으므로 근사로 남긴다. 틀린 확정값보다 낫다.
        mid = ", ".join(f"2026년 {m:02d}월 21일" for m in range(1, 13))
        text = f"○ 중간기준가격 : {mid} 각 종가 ○ 최종기준가격 : 2029년 07월 23일 종가"
        p = _product(barriers_raw=[95, 90, 90, 85, 85, 40],
                     base_eval_date=_d("2025-12-01"), expiry_date=_d("2029-07-26"))
        dates, why = extract_schedule(p, text)
        self.assertIsNone(dates)
        self.assertIn("배리어", why)


class TargetSelectionTests(TestCase):
    """대상 선정 — 확정된 건은 다시 내려받지 않고, 근사인 건은 반드시 잡는다."""

    def test_평가일이_근사면_기준일이_있어도_대상이다(self):
        p = Product.objects.create(
            issuer="신한투자증권", product_no="27859", prospectus_url="http://x",
            barriers_raw=[90, 80, 70], base_eval_date=_d("2026-08-07"))
        self.assertTrue(needs_parse(p))

    def test_기준일과_확정평가일이_다_있으면_대상이_아니다(self):
        p = Product.objects.create(
            issuer="신한투자증권", product_no="27860", prospectus_url="http://x",
            barriers_raw=[90, 80, 70], base_eval_date=_d("2026-08-07"),
            eval_dates=["2027-02-05", "2027-08-06", "2029-02-07"])
        self.assertFalse(needs_parse(p))

    def test_회차수가_안_맞는_평가일은_확정이_아니라서_대상이다(self):
        p = Product.objects.create(
            issuer="신한투자증권", product_no="27861", prospectus_url="http://x",
            barriers_raw=[90, 80, 70], base_eval_date=_d("2026-08-07"),
            eval_dates=["2027-02-05", "2027-08-06"])
        self.assertTrue(needs_parse(p))

    def test_배리어_없는_ELB는_기준일만_채우면_다시_안_본다(self):
        # 조기상환 회차가 없어 평가일은 영원히 미확정이다. 매일 헛되이 받으면 안 된다.
        p = Product.objects.create(
            issuer="신영증권", product_no="320", product_type="ELB",
            prospectus_url="http://x", base_eval_date=_d("2026-07-29"))
        self.assertFalse(needs_parse(p))


class SaveBehaviourTests(TestCase):
    """저장 규칙 — 확정된 값은 덮어쓰지 않고, 근사인 값만 채운다."""

    def _run(self, text, tables=None):
        target = ("core.management.commands.parse_prospectus_dates"
                  ".Command._fetch_text")
        out = io.StringIO()
        with patch(target, return_value=(text, tables or [])):
            call_command("parse_prospectus_dates", delay=0, stdout=out)
        return out.getvalue()

    def test_근사였던_상품에_설명서_평가일을_채운다(self):
        p = Product.objects.create(
            issuer="KB증권", product_no="4486", prospectus_url="http://x",
            barriers_raw=[75, 70, 70, 65], sub_end=_d("2026-08-07"),
            expiry_date=_d("2027-08-10"))
        self._run(KB_4486)
        p.refresh_from_db()
        self.assertEqual(p.base_eval_date, _d("2026-08-07"))
        self.assertEqual(p.eval_dates,
                         ["2026-11-06", "2027-02-05", "2027-05-07", "2027-08-06"])

    def test_이미_확정된_평가일은_덮어쓰지_않는다(self):
        # SEIBro가 넣어 둔 값(마지막 칸이 만기일)을 그대로 둔다
        seibro = ["2026-11-06", "2027-02-05", "2027-05-07", "2027-08-10"]
        p = Product.objects.create(
            issuer="KB증권", product_no="4486", prospectus_url="http://x",
            barriers_raw=[75, 70, 70, 65], sub_end=_d("2026-08-07"),
            base_eval_date=_d("2026-08-07"), expiry_date=_d("2027-08-10"),
            eval_dates=seibro)
        self._run(KB_4486)     # 확정분은 기본 실행 대상이 아니다
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, seibro)

    def test_확정분_대조에서_다르면_경고만_하고_그대로_둔다(self):
        seibro = ["2026-11-06", "2027-02-05", "2027-05-07", "2027-08-10"]
        p = Product.objects.create(
            issuer="KB증권", product_no="4486", prospectus_url="http://x",
            barriers_raw=[75, 70, 70, 65], sub_end=_d("2026-08-07"),
            base_eval_date=_d("2026-08-07"), expiry_date=_d("2027-08-10"),
            eval_dates=seibro)
        target = ("core.management.commands.parse_prospectus_dates"
                  ".Command._fetch_text")
        out = io.StringIO()
        with patch(target, return_value=(KB_4486, [])):
            call_command("parse_prospectus_dates", delay=0, verify_fixed=True,
                         stdout=out)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, seibro)
        self.assertIn("상충 1건", out.getvalue())

    def test_파싱_실패하면_평가일을_건드리지_않는다(self):
        p = Product.objects.create(
            issuer="KB증권", product_no="4486", prospectus_url="http://x",
            barriers_raw=[90, 80, 70], sub_end=_d("2026-08-07"),
            expiry_date=_d("2027-08-10"))
        self._run(KB_4486)     # 회차 4개 vs 배리어 3개
        p.refresh_from_db()
        self.assertIsNone(p.eval_dates)


class MaturityGapReportTests(TestCase):
    """만기평가일 차이 보고 커맨드 — 읽기만 하고 아무것도 고치지 않는다."""

    def test_저장값을_바꾸지_않고_차이만_센다(self):
        seibro = ["2026-11-06", "2027-02-05", "2027-05-07", "2027-08-10"]
        p = Product.objects.create(
            issuer="KB증권", product_no="4486", prospectus_url="http://x",
            barriers_raw=[75, 70, 70, 65], sub_end=_d("2026-08-07"),
            base_eval_date=_d("2026-08-07"), expiry_date=_d("2027-08-10"),
            eval_dates=seibro)
        target = ("core.management.commands.parse_prospectus_dates"
                  ".Command._fetch_text")
        out = io.StringIO()
        with patch(target, return_value=(KB_4486, [])):
            call_command("report_maturity_eval_gap", delay=0, stdout=out)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, seibro)
        text = out.getvalue()
        self.assertIn("마지막 회차가 다른 건 1건", text)
        self.assertIn("+4일", text)     # 2027-08-10 vs 2027-08-06
