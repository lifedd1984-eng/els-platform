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
    SAMSUNG_FORMATS,
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

# 삼성증권 31248호 — 월수익형. 나열된 35개는 매월 지급 평가일이라 조기상환 회차가
# 아니고, 그 중 6·12·18·24·30번째만 조기상환 평가일이다. 한 칸만 밀려도 조용히
# 틀린 확정값이 남으므로 전문을 그대로 박아 둔다(공백 정규화만 함).
# '12차 월수익 중간기준 / 가격결정일' 사이에 쪽 머리말이 끼어드는 것도 원문 그대로다.
SAMSUNG_31248_MONTHLY = (
    "○ 최초기준가격 : 2026년 07월 23일종가 ○ 월수익 중간기준가격 : 2026년 08월 21일, 2026년 09월 18일, "
    "2026년 10월 23일, 2026년 11월 20일, 2026년 12월 23일, 2027년 01월 22일, 2027년 02월 22일, "
    "2027년 03월 23일, 2027년 04월 23일, 2027년 05월 21일, 2027년 06월 23일, 2027년 07월 23일, "
    "2027년 08월 23일, 2027년 09월 22일, 2027년 10월 22일, 2027년 11월 22일, 2027년 12월 23일, "
    "2028년 01월 21일, 2028년 02월 22일, 2028년 03월 23일, 2028년 04월 21일, 2028년 05월 23일, "
    "2028년 06월 23일, 2028년 07월 21일, 2028년 08월 23일, 2028년 09월 21일, 2028년 10월 23일, "
    "2028년 11월 22일, 2028년 12월 22일, 2029년 01월 23일, 2029년 02월 22일, 2029년 03월 23일, "
    "2029년 04월 23일, 2029년 05월 23일, 2029년 06월 22일 각 종가 "
    "○ 자동조기상환가격 : 6차 월수익 중간기준가격결정일, 12차 월수익 중간기준 전자공시시스템 "
    "dart.fss.or.kr Page 75 가격결정일, 18차 월수익 중간기준가격결정일, 24차 월수익 중간기준가격결정 일, "
    "30차 월수익 중간기준가격결정일 각 종가 ○ 최종기준가격 : 2029년 07월 23일종가"
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


class SamsungMonthlyTurnRuleTests(SimpleTestCase):
    """삼성증권 월수익형 — 나열된 35개 중 규칙이 가리키는 번호만 조기상환 회차다.

    이 파일에서 유일하게 '목록의 순서 ≠ 회차'인 포맷이라 인덱스 오프셋이 유일한
    위험이다. 6차가 1-based인지 0-based인지, 목록에 최초기준가격이 들어가는지에 따라
    한 달씩 밀린다. 그래서 기대값을 SEIBro 확정값으로 못 박고, 한 칸 앞·뒤 값이
    나오면 반드시 실패하게 해 뒀다.
    """

    BARRIERS = [95, 90, 90, 85, 85, 40]
    # SEIBro 확정값(ISIN KR6SS0008FY4). 2026-08-19 운영 DB 읽기 전용 조회.
    SEIBRO_EARLY = ["2027-01-22", "2027-07-23", "2028-01-21", "2028-07-21", "2029-01-23"]
    # 같은 목록을 한 칸 앞(0-based)·한 칸 뒤로 읽었을 때 나오는 값. 절대 나오면 안 된다.
    SHIFTED_BACK = ["2026-12-23", "2027-06-23", "2027-12-23", "2028-06-23", "2028-12-22"]
    SHIFTED_FWD = ["2027-02-22", "2027-08-23", "2028-02-22", "2028-08-23", "2029-02-22"]

    def _p(self, **kw):
        kw.setdefault("barriers_raw", self.BARRIERS)
        return _product(issuer="삼성증권", product_no="31248",
                        base_eval_date=_d("2026-07-23"), sub_end=_d("2026-07-23"),
                        expiry_date=_d("2029-07-26"), **kw)

    def test_조기상환_5회차가_SEIBro_확정값과_같다(self):
        dates, why = extract_schedule(self._p(), SAMSUNG_31248_MONTHLY)
        self.assertEqual(why, "월수익 중간기준가격")
        self.assertEqual([d.isoformat() for d in dates[:-1]], self.SEIBRO_EARLY)
        # 삼성은 만기평가일을 '최종기준가격'으로 적는다. 만기일(2029-07-26)이 아니다.
        self.assertEqual(dates[-1], _d("2029-07-23"))

    def test_한_칸_밀린_값은_나오지_않는다(self):
        dates, _ = extract_schedule(self._p(), SAMSUNG_31248_MONTHLY)
        got = [d.isoformat() for d in dates[:-1]]
        self.assertNotEqual(got, self.SHIFTED_BACK)
        self.assertNotEqual(got, self.SHIFTED_FWD)
        # 목록의 5·6·7번째를 직접 확인 — 6차는 여섯 번째다(1-based)
        early, _ = extract_early_dates(SAMSUNG_31248_MONTHLY)
        self.assertEqual(early[0], _d("2027-01-22"))

    def test_규칙이_없는_일반_삼성_상품은_목록_그대로_쓴다(self):
        """'자동조기상환가격' 항목이 없으면 중간기준가격 목록 자체가 회차다."""
        p = _product(issuer="삼성증권", barriers_raw=[90] * 6 + [85] * 3 + [80, 80, 75],
                     base_eval_date=_d("2026-07-23"), expiry_date=_d("2029-07-25"))
        dates, why = extract_schedule(p, SAMSUNG_31243)
        self.assertEqual(why, "중간기준가격")
        self.assertEqual(dates[0], _d("2026-10-23"))

    def test_차수가_목록_범위를_넘으면_저장하지_않는다(self):
        text = SAMSUNG_31248_MONTHLY.replace("30차 월수익", "40차 월수익")
        dates, why = extract_early_dates(text)
        self.assertIsNone(dates)
        self.assertIn("범위 밖", why)

    def test_차수가_오름차순이_아니면_저장하지_않는다(self):
        text = SAMSUNG_31248_MONTHLY.replace("18차 월수익", "11차 월수익")
        dates, why = extract_early_dates(text)
        self.assertIsNone(dates)
        self.assertIn("오름차순", why)

    def test_규칙이_중간기준가격을_가리키지_않으면_저장하지_않는다(self):
        text = SAMSUNG_31248_MONTHLY.replace(
            "○ 자동조기상환가격 : 6차 월수익 중간기준가격결정일, 12차 월수익 중간기준 전자공시시스템 "
            "dart.fss.or.kr Page 75 가격결정일, 18차 월수익 중간기준가격결정일, "
            "24차 월수익 중간기준가격결정 일, 30차 월수익 중간기준가격결정일 각 종가",
            "○ 자동조기상환가격 : 6차 월수익 지급평가일, 12차 월수익 지급평가일 각 종가")
        dates, why = extract_early_dates(text)
        self.assertIsNone(dates)
        self.assertIn("중간기준가격을 가리키지 않음", why)

    def test_회차수가_배리어수와_다르면_저장하지_않는다(self):
        # 규칙은 5회차인데 배리어가 6개가 아니면 회차 정렬을 믿을 수 없다
        dates, why = extract_schedule(self._p(barriers_raw=[95, 90, 85, 40]),
                                      SAMSUNG_31248_MONTHLY)
        self.assertIsNone(dates)
        self.assertIn("배리어", why)


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

    def test_월별_나열만_있고_차수_규칙이_없으면_저장하지_않는다(self):
        # 중간기준가격이 매월 나열됐는데 '자동조기상환가격' 규칙 항목이 없으면
        # 목록의 몇 번째가 조기상환 회차인지 가릴 근거가 없다. 그때는 목록 전체가
        # 회차로 남아 배리어 수와 어긋나므로 근사로 남긴다. 틀린 확정값보다 낫다.
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

    def test_삼성_월수익형_근사였던_상품에_평가일을_채운다(self):
        p = Product.objects.create(
            issuer="삼성증권", product_no="2926", product_type="ELB",
            prospectus_url="http://x", barriers_raw=[100] * 6,
            sub_end=_d("2026-07-23"), expiry_date=_d("2029-07-25"))
        self._run(SAMSUNG_31248_MONTHLY)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, [
            "2027-01-22", "2027-07-23", "2028-01-21", "2028-07-21", "2029-01-23",
            "2029-07-23"])

    def test_삼성_월수익형_확정분은_덮어쓰지_않는다(self):
        # SEIBro가 넣어 둔 값(마지막 칸이 만기일)을 그대로 둔다
        seibro = ["2027-01-22", "2027-07-23", "2028-01-21", "2028-07-21",
                  "2029-01-23", "2029-07-26"]
        p = Product.objects.create(
            issuer="삼성증권", product_no="31248", prospectus_url="http://x",
            barriers_raw=[95, 90, 90, 85, 85, 40], sub_end=_d("2026-07-23"),
            base_eval_date=_d("2026-07-23"), expiry_date=_d("2029-07-26"),
            eval_dates=seibro)
        self._run(SAMSUNG_31248_MONTHLY)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, seibro)

    def test_파싱_실패하면_평가일을_건드리지_않는다(self):
        p = Product.objects.create(
            issuer="KB증권", product_no="4486", prospectus_url="http://x",
            barriers_raw=[90, 80, 70], sub_end=_d("2026-08-07"),
            expiry_date=_d("2027-08-10"))
        self._run(KB_4486)     # 회차 4개 vs 배리어 3개
        p.refresh_from_db()
        self.assertIsNone(p.eval_dates)


class MaturityEvalFixTests(TestCase):
    """마지막 회차 교체 커맨드 — 만기 칸 하나만 바꾸고 조기상환 회차는 손대지 않는다.

    SEIBro가 넣어 둔 조기상환 회차는 설명서 전수 대조에서 완벽히 일치한 값이다.
    여기서 한 칸이라도 움직이면 조기상환 판정·알림이 통째로 어긋나므로,
    '앞 회차 불변'을 모든 테스트에서 함께 못 박는다.
    """

    # KB증권 4486호 설명서 기준. SEIBro는 마지막 칸에 만기일(08-10)을 넣었고
    # 설명서의 만기평가일은 08-06이다.
    SEIBRO = ["2026-11-06", "2027-02-05", "2027-05-07", "2027-08-10"]
    EARLY = ["2026-11-06", "2027-02-05", "2027-05-07"]
    FIXED = ["2026-11-06", "2027-02-05", "2027-05-07", "2027-08-06"]

    def _p(self, **kw):
        kw.setdefault("prospectus_url", "http://x")
        kw.setdefault("barriers_raw", [75, 70, 70, 65])
        kw.setdefault("eval_dates", list(self.SEIBRO))
        kw.setdefault("expiry_date", _d("2027-08-10"))
        kw.setdefault("product_no", "4486")
        return Product.objects.create(
            issuer="KB증권", sub_end=_d("2026-08-07"),
            base_eval_date=_d("2026-08-07"), **kw)

    def _run(self, text=KB_4486, tables=None, **opts):
        target = ("core.management.commands.parse_prospectus_dates"
                  ".Command._fetch_text")
        out = io.StringIO()
        with patch(target, return_value=(text, tables or [])):
            call_command("fix_maturity_eval_date", delay=0, stdout=out, **opts)
        return out.getvalue()

    def test_기본_실행은_아무것도_저장하지_않는다(self):
        p = self._p()
        text = self._run()
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.SEIBRO)
        self.assertIn("교체 예정 1건", text)
        self.assertIn("저장하지 않았다", text)

    def test_apply하면_마지막_칸만_바뀐다(self):
        p = self._p()
        self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.FIXED)
        # 조기상환 회차는 한 날도 움직이지 않아야 한다
        self.assertEqual(p.eval_dates[:-1], self.EARLY)

    def test_교체_뒤에도_확정_평가일로_읽힌다(self):
        p = self._p()
        self._run(apply=True)
        p.refresh_from_db()
        self.assertIsNotNone(p.fixed_eval_dates)
        self.assertEqual(len(p.fixed_eval_dates), len(p.barriers_raw))
        self.assertEqual(p.fixed_eval_dates[-1], _d("2027-08-06"))
        # 마지막 회차가 직전 회차보다 뒤여야 스케줄이 성립한다
        self.assertLess(p.fixed_eval_dates[-2], p.fixed_eval_dates[-1])

    def test_설명서_URL이_없으면_대상이_아니다(self):
        p = self._p(prospectus_url="")
        text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.SEIBRO)
        self.assertIn("대상: 0건", text)

    def test_조기상환_회차가_다르면_손대지_않는다(self):
        # 저장된 1회차가 설명서와 다르다 — 다른 차수의 설명서일 수 있어 근거가 없다
        wrong = ["2026-11-05", "2027-02-05", "2027-05-07", "2027-08-10"]
        p = self._p(eval_dates=list(wrong))
        text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, wrong)
        self.assertIn("상충 1건", text)

    def test_설명서_회차수가_배리어와_다르면_저장하지_않는다(self):
        # 배리어 5개인데 설명서는 조기상환 3 + 만기 1 = 4회차다
        p = self._p(barriers_raw=[75, 70, 70, 70, 65],
                    eval_dates=self.SEIBRO + ["2027-08-10"])
        before = list(p.eval_dates)
        text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, before)
        self.assertIn("못 읽음 1건", text)

    def test_회차수가_안_맞는_저장값은_애초에_대상이_아니다(self):
        # eval_dates 4개 vs 배리어 3개 — fixed_eval_dates가 성립하지 않는다
        p = self._p(barriers_raw=[75, 70, 65])
        text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.SEIBRO)
        self.assertIn("대상: 0건", text)

    def test_이미_만기평가일이_들어_있으면_그대로_둔다(self):
        # 마지막 칸이 만기일이 아니면 손댈 이유가 없다
        p = self._p(eval_dates=list(self.FIXED))
        text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.FIXED)
        self.assertIn("대상: 0건", text)

    def test_설명서를_못_읽으면_손대지_않는다(self):
        p = self._p()
        text = self._run(text="평가일이 하나도 적혀 있지 않은 문서", apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.SEIBRO)
        self.assertIn("못 읽음 1건", text)

    def test_삼성_월수익형도_최종기준가격을_읽는다(self):
        # 삼성은 만기평가일을 '최종기준가격'으로 적는다. 포맷 판정이 어긋나면
        # 월수익형이 통째로 '못 읽음'으로 빠진다.
        seibro = ["2027-01-22", "2027-07-23", "2028-01-21", "2028-07-21",
                  "2029-01-23", "2029-07-26"]
        p = Product.objects.create(
            issuer="삼성증권", product_no="31248", prospectus_url="http://x",
            barriers_raw=[95, 90, 90, 85, 85, 40], sub_end=_d("2026-07-23"),
            base_eval_date=_d("2026-07-23"), expiry_date=_d("2029-07-26"),
            eval_dates=seibro)
        self._run(text=SAMSUNG_31248_MONTHLY, apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, seibro[:-1] + ["2029-07-23"])
        self.assertEqual(p.eval_dates[:-1], seibro[:-1])


class MaturityEvalFixGuardTests(TestCase):
    """마지막 회차 교체 커맨드의 안전장치 — 근거가 흔들리면 저장하지 않는다.

    확정 평가일 457건을 직접 고치는 커맨드다. 회차가 한 칸이라도 밀리면
    조기상환 판정·알림·엑셀이 통째로 어긋나고, 회차 **수**가 바뀌면
    fixed_eval_dates가 성립하지 않아 화면이 근사 스케줄로 되돌아간다.
    그래서 '무엇을 안 하는가'를 실행 결과로 못 박는다.
    """

    FETCH = ("core.management.commands.parse_prospectus_dates"
             ".Command._fetch_text")
    SEIBRO = ["2026-11-06", "2027-02-05", "2027-05-07", "2027-08-10"]

    def _kb(self, **kw):
        kw.setdefault("eval_dates", list(self.SEIBRO))
        kw.setdefault("barriers_raw", [75, 70, 70, 65])
        return Product.objects.create(
            issuer="KB증권", product_no="4486", prospectus_url="http://x",
            sub_end=_d("2026-08-07"), base_eval_date=_d("2026-08-07"),
            expiry_date=_d("2027-08-10"), **kw)

    def _run(self, text=KB_4486, tables=None, **opts):
        out = io.StringIO()
        with patch(self.FETCH, return_value=(text, tables or [])):
            call_command("fix_maturity_eval_date", delay=0, stdout=out, **opts)
        return out.getvalue()

    # ---------------------------------------------------------------- 회차 불일치
    def test_불일치_사유는_앞_회차만_가리킨다(self):
        # 마지막 칸이 다른 것은 이 커맨드의 전제다. 그 칸까지 '불일치 회차'로
        # 세면 한 회차만 어긋난 건이 두 회차 어긋난 것처럼 보인다.
        p = self._kb(eval_dates=["2026-11-05", "2027-02-05", "2027-05-07",
                                 "2027-08-10"])
        text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates,
                         ["2026-11-05", "2027-02-05", "2027-05-07", "2027-08-10"])
        self.assertIn("조기상환 회차 불일치(회차 [1])", text)
        self.assertNotIn("회차 [1, 4]", text)

    def test_불일치_회차가_여럿이면_전부_적는다(self):
        p = self._kb(eval_dates=["2026-11-05", "2027-02-04", "2027-05-07",
                                 "2027-08-10"])
        text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates[0], "2026-11-05")
        self.assertIn("조기상환 회차 불일치(회차 [1, 2])", text)

    def test_상충_건은_상품번호가_로그에_남는다(self):
        self._kb(eval_dates=["2026-11-05", "2027-02-05", "2027-05-07",
                             "2027-08-10"])
        text = self._run(apply=True)
        self.assertIn("KB증권 4486", text)
        self.assertIn("손대지 않음", text)

    def test_못_읽은_건도_상품번호가_로그에_남는다(self):
        # 사유만 집계하고 넘어가면 '못 읽음 12건'을 보고도 어느 12건인지
        # 사람이 찾을 수가 없다.
        self._kb()
        text = self._run(text="평가일이 하나도 적혀 있지 않은 문서", apply=True)
        self.assertIn("KB증권 4486", text)
        self.assertIn("못 읽음 1건", text)

    def test_차이가_0일이면_사유가_남고_저장하지_않는다(self):
        # 설명서 만기평가일이 만기일과 같은 날인 경우. 2026-08-13 실측에서
        # 457건 전부 어긋났으므로 0일은 그 자체로 눈여겨볼 값이다.
        same_day = KB_4486.replace("만기평가일 : 2027년 08월 06일",
                                   "만기평가일 : 2027년 08월 10일")
        p = self._kb()
        text = self._run(text=same_day, apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.SEIBRO)
        self.assertIn("차이 0일", text)
        self.assertIn("KB증권 4486", text)

    # ---------------------------------------------------------------- 회차 수
    def test_설명서_회차가_더_많으면_상충이_아니라_미저장이다(self):
        # extract_schedule이 배리어 수를 검사하므로 실제로는 여기까지 오지
        # 않지만, 오면 fixed_eval_dates가 깨지는 자리다. 앞 회차 대조보다
        # 먼저 걸려야 '회차가 하나 밀렸다'가 '한 날이 다르다'로 둔갑하지 않는다.
        p = self._kb()
        longer = _dates("2026-11-06", "2027-02-05", "2027-05-07", "2027-08-06",
                        "2027-11-05")
        with patch("core.management.commands.fix_maturity_eval_date"
                   ".extract_schedule", return_value=(longer, "차수표")):
            text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.SEIBRO)
        self.assertIn("회차 수가 다름(저장 4개 vs 설명서 5개)", text)
        self.assertNotIn("상충 1건", text)

    def test_설명서_회차가_더_적어도_미저장이다(self):
        p = self._kb()
        shorter = _dates("2026-11-06", "2027-02-05", "2027-08-06")
        with patch("core.management.commands.fix_maturity_eval_date"
                   ".extract_schedule", return_value=(shorter, "차수표")):
            text = self._run(apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, self.SEIBRO)
        self.assertIn("회차 수가 다름(저장 4개 vs 설명서 3개)", text)

    def test_어떤_설명서를_읽어도_회차_수와_앞_회차는_그대로다(self):
        """서로 다른 발행사의 설명서를 전부 물려 봐도 불변식이 깨지지 않는다.

        _fetch_text를 한 값으로 고정하므로 모든 상품이 '남의 설명서'를 읽는
        상황이 된다. 그래도 (1) eval_dates 개수 = 배리어 개수, (2) 앞 회차
        전부 그대로, (3) 마지막 칸은 만기일보다 앞으로만 움직인다.
        """
        made = [
            # (발행사, 상품번호, 배리어수, 조기상환 회차, 만기일)
            ("KB증권", "4486", 4,
             ["2026-11-06", "2027-02-05", "2027-05-07"], "2027-08-10"),
            ("키움증권", "4126", 6,
             ["2027-01-25", "2027-07-23", "2028-01-24", "2028-07-24",
              "2029-01-23"], "2029-07-25"),
            ("신한투자증권", "27859", 12,
             ["2026-11-09", "2027-02-05", "2027-05-07", "2027-08-06",
              "2027-11-05", "2028-02-07", "2028-04-28", "2028-08-07",
              "2028-11-07", "2029-02-07", "2029-05-02"], "2029-08-10"),
            ("삼성증권", "31248", 6,
             ["2027-01-22", "2027-07-23", "2028-01-21", "2028-07-21",
              "2029-01-23"], "2029-07-26"),
        ]
        early_by_id = {}
        for issuer, no, n_bar, early, expiry in made:
            p = Product.objects.create(
                issuer=issuer, product_no=no, prospectus_url="http://x",
                barriers_raw=[70] * n_bar, sub_end=_d("2026-07-22"),
                base_eval_date=_d("2026-07-23"), expiry_date=_d(expiry),
                eval_dates=early + [expiry])
            early_by_id[p.id] = early

        # 남의 설명서부터 물린다. 그때는 아직 마지막 칸이 만기일이라 네 건 모두
        # 대상으로 잡혀 가드를 실제로 통과해야 한다. 제 설명서는 맨 뒤에 둔다.
        for text in ("평가일이 없는 문서", NH_369, KYOBO_21, MERITZ_525,
                     KB_4496, SAMSUNG_31243,
                     KB_4486, KIWOOM_4126, SHINHAN_27859,
                     SAMSUNG_31248_MONTHLY):
            self._run(text=text, apply=True)
            for p in Product.objects.all():
                early = early_by_id[p.id]
                self.assertEqual(len(p.eval_dates), len(p.barriers_raw))
                self.assertIsNotNone(p.fixed_eval_dates)
                self.assertEqual(p.eval_dates[:-1], early)
                self.assertLessEqual(_d(p.eval_dates[-1]), p.expiry_date)
                self.assertLess(_d(early[-1]), _d(p.eval_dates[-1]))

        # 헛돌지 않았는지 — 제 설명서를 만난 네 건은 전부 실제로 교체됐어야 한다.
        for p in Product.objects.all():
            self.assertLess(_d(p.eval_dates[-1]), p.expiry_date,
                            f"{p.issuer} {p.product_no}가 교체되지 않았다")

    # ---------------------------------------------------------------- 만기평가일
    def test_만기평가일_후보가_여럿이면_가장_늦은_날로_교체한다(self):
        # 키움 4126호는 후보가 [07/19, 07/20, 07/23]이고 만기일이 07/25다.
        seibro = ["2027-01-25", "2027-07-23", "2028-01-24", "2028-07-24",
                  "2029-01-23", "2029-07-25"]
        p = Product.objects.create(
            issuer="키움증권", product_no="4126", prospectus_url="http://x",
            barriers_raw=[90, 90, 85, 85, 80, 45], sub_end=_d("2026-07-22"),
            base_eval_date=_d("2026-07-23"), expiry_date=_d("2029-07-25"),
            eval_dates=list(seibro))
        text = self._run(text=KIWOOM_4126, apply=True)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, seibro[:-1] + ["2029-07-23"])
        self.assertEqual(p.eval_dates[:-1], seibro[:-1])
        self.assertIn("+2일", text)


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

    def test_삼성_월수익형도_확인_못_함으로_빠지지_않는다(self):
        # 삼성은 만기평가일을 '최종기준가격'으로 적는다. 포맷을 '중간기준가격'
        # 하나로만 보면 월수익형이 통째로 '확인 못 함'으로 빠졌다.
        seibro = ["2027-01-22", "2027-07-23", "2028-01-21", "2028-07-21",
                  "2029-01-23", "2029-07-26"]
        p = Product.objects.create(
            issuer="삼성증권", product_no="31248", prospectus_url="http://x",
            barriers_raw=[95, 90, 90, 85, 85, 40], sub_end=_d("2026-07-23"),
            base_eval_date=_d("2026-07-23"), expiry_date=_d("2029-07-26"),
            eval_dates=seibro)
        target = ("core.management.commands.parse_prospectus_dates"
                  ".Command._fetch_text")
        out = io.StringIO()
        with patch(target, return_value=(SAMSUNG_31248_MONTHLY, [])):
            call_command("report_maturity_eval_gap", delay=0, stdout=out)
        p.refresh_from_db()
        self.assertEqual(p.eval_dates, seibro)      # 읽기 전용
        text = out.getvalue()
        self.assertIn("마지막 회차가 다른 건 1건", text)
        self.assertIn("확인 못 함 0건", text)
        self.assertIn("+3일", text)     # 2029-07-26 vs 2029-07-23

    def test_삼성_판정_확대가_다른_발행사에는_닿지_않는다(self):
        # samsung 플래그는 포맷명으로만 켜진다. 삼성 외 포맷은 물론이고
        # 파싱 실패 사유 문자열도 SAMSUNG_FORMATS에 들어가면 안 된다.
        for label, text, tables in (
                ("KB_4486", KB_4486, None), ("KB_4496", KB_4496, None),
                ("NH_369", NH_369, None), ("KYOBO_21", KYOBO_21, None),
                ("KIWOOM_4126", KIWOOM_4126, None),
                ("MERITZ_525", MERITZ_525, None),
                ("SHINHAN_27859", SHINHAN_27859, None),
                ("HANWHA_9555", HANWHA_9555_TEXT, HANWHA_9555_TABLES),
                ("HANA_17786", HANA_17786_TEXT, HANA_17786_TABLES),
                ("빈 문서", "평가일이 없는 문서", None)):
            with self.subTest(label):
                _dates_, fmt = extract_early_dates(text, tables)
                self.assertNotIn(fmt, SAMSUNG_FORMATS)

    def test_삼성_두_포맷만_최종기준가격을_본다(self):
        for text, expected in ((SAMSUNG_31243, "중간기준가격"),
                               (SAMSUNG_31248_MONTHLY, "월수익 중간기준가격")):
            with self.subTest(expected):
                _dates_, fmt = extract_early_dates(text)
                self.assertEqual(fmt, expected)
                self.assertIn(fmt, SAMSUNG_FORMATS)
        self.assertEqual(SAMSUNG_FORMATS, ("중간기준가격", "월수익 중간기준가격"))
