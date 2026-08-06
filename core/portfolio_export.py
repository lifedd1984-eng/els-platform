"""보유계약 엑셀 다운로드 — 조 팀장이 쓰던 관리 양식(17열) 재현.

컬럼과 변환 규칙은 2026-08-06 조 팀장이 준 실제 시트 6행을 운영 DB와 한 칸씩
대조해 확정했다. 이 모듈의 제1원칙은 **숫자를 만들어 내지 않는 것**이다 —
구할 수 없는 칸은 비우고, 계산으로 채운 칸은 비고에 '추정'을 남겨 확정값과 구분한다.

단위·형식 (조 팀장 시트 대조로 확정)
    투자금액·예상수익 : 만원 (DB는 원 단위 → 30,000,000원 = 3,000)
    발행일            : YYYYMMDD 정수 (20260403)
    만기일            : YY.MM.DD 문자열 (29.04.02)
    투자월·첫 조기    : YYYYMM 정수 (202604)
    금리              : 연 % (Product.yield_rate 그대로)

예상수익 = 투자금액(만원) × 금리 × 주기 ÷ 12, 반올림.
같은 행의 보이는 값들만으로 재현되도록 일부러 '주기'를 쓴다(1차까지 개월수가
따로인 비균등 상품도 시트 안에서 계산이 맞아떨어져야 하므로).
"""

import math
import re
from datetime import date

# DB는 원 단위, 조 팀장 시트는 만원 단위 (30,000,000원 → 3,000)
AMOUNT_UNIT = 10000

# 만기-발행일 일수를 개월로 환산할 때 쓰는 평균 월 길이
_MONTH_DAYS = 30.44

COLUMNS = ["증권사", "상품번호", "기초자산", "발행일", "만기일", "금리", "투자금액",
           "상환여부", "낙인", "1회차", "마지막", "주기", "구분", "투자월",
           "첫 조기", "비고", "예상수익"]

COLUMN_WIDTHS = [12, 10, 26, 11, 10, 8, 10, 9, 8, 7, 8, 6, 8, 9, 9, 16, 10]


def _round_half_up(value):
    """엑셀 ROUND와 같은 반올림(0.5는 올림).

    파이썬 기본 round()는 은행가 반올림이라 2.5를 2로 내린다 — 조 팀장이 엑셀에서
    쓰던 값과 어긋나므로 쓰지 않는다. 금액·개월수 모두 음수가 없어 floor로 충분하다.
    """
    return int(math.floor(value + 0.5))


def assets_display(product):
    """기초자산 표시 문자열 — 원문 이름 그대로, 구분자만 '/'로 정리.

    축약(K/N/S, 삼전 …)은 쓰지 않는다. 조 팀장이 손으로 쓰던 개인 표기이고
    시스템이 흉내 내면 뜻이 갈릴 수 있어 원문 유지로 확정했다(2026-08-06).
    정리 자체는 이미 화면에서 쓰는 market.shorten_asset_display에 맡긴다 —
    "Palantir   , Micron" 같은 지저분한 구분자와 " Index" 접미사만 다듬는다.
    """
    from core import market
    return market.shorten_asset_display(product.assets_raw or "")


def period_months(product):
    """평가주기(개월). Product.period_months가 없으면 만기·배리어 개수로 유도.

    유도식 = (만기 - 발행일 개월수) ÷ 배리어 개수.
    조 팀장 보유 188건 대조에서 DB 값과 188/188 일치했다(2026-08-06).
    eval_dates 간격 중앙값으로 구해도 166/166 일치해 답이 같으므로,
    결측이 적은 쪽(배리어·만기)만 요구하는 나눗셈을 쓴다.
    """
    if product.period_months:
        return product.period_months
    barriers = product.barriers_raw or []
    start, end = product.issued_on, product.expiry_date
    if not (barriers and start and end):
        return None
    total = _round_half_up((end - start).days / _MONTH_DAYS)
    return _round_half_up(total / len(barriers)) or None


def _shift_ym(base, months):
    """base(date)에서 months개월 뒤의 YYYYMM 정수. 월 단위라 말일 보정이 필요 없다."""
    total = base.year * 12 + (base.month - 1) + months
    return (total // 12) * 100 + (total % 12) + 1


def first_eval_ym(investment):
    """첫 조기상환 시점 → (YYYYMM 정수 | None, 확정 여부).

    ① Product.eval_dates(SEIBro 확정값) 첫 회차 → 확정
    ② 없으면 발행일 + 1차까지 개월수로 계산 → 추정
       조 팀장 보유분 중 확정값이 있는 166건으로 채점하면 월 단위 153/166(92.2%)
       일치한다. 어긋난 13건은 전부 계산이 한 달 늦은 쪽이었다(증권사가 평가일을
       발행일+N개월보다 며칠 앞에 잡는 경우). 그래서 계산으로 채운 칸은 비고에
       '추정'을 남긴다 — 확정값인 척하지 않기 위해서다.
    """
    p = investment.product
    fixed = p.fixed_eval_dates
    if fixed:
        return int(fixed[0].strftime("%Y%m")), True
    base = p.issued_on or investment.invested_at
    months = p.first_eval_months or period_months(p)
    if not (base and months):
        return None, False
    return _shift_ym(base, months), False


def status_display(investment):
    """상환여부 — 보유중은 빈칸, 조기상환은 '완료'(조 팀장 표기).

    만기상환·낙인후상환은 DB 라벨을 그대로 쓴다. '완료'로 뭉뚱그리면 손실 상환까지
    성공처럼 보이므로 임의로 묶지 않는다.
    """
    if investment.status == "보유중":
        return ""
    return "완료" if investment.status == "조기상환" else investment.status


def note(investment, schedule_note=""):
    """비고 — 상품 구조 표시 + 스케줄 신뢰도 + 사용자 메모.

    조 팀장 시트의 '월지급'·'L50'은 둘 다 Product.description에서 그대로 나온다
    (2026-08-06 확인).
        삼성 30895 → "[월지급식] 3년/3개월,25KI(...)%,... 세전 연 19.8% (월 1.65%)"
        키움 1827 → "[스텝다운리자드] 2년/4개월/80-80-75(L50)-75-70-60/KI35"
    'L50'은 리자드 50이다(조 팀장 확인). 마커가 없는 리자드 상품은 '리자드'로 적는다.
    """
    parts = []
    desc = investment.product.description or ""
    if "월지급" in desc:
        parts.append("월지급")
    marker = re.search(r"\(L(\d+)\)", desc)
    if marker:
        parts.append(f"L{marker.group(1)}")
    elif re.search(r"리자드|리쟈드|Lizard", desc, re.IGNORECASE):
        parts.append("리자드")
    if schedule_note:
        parts.append(schedule_note)
    if investment.memo:
        parts.append(investment.memo)
    return " ".join(parts)


def _sort_key(investment):
    """조 팀장 시트 정렬 — 발행일 → 증권사 → 상품번호(숫자는 숫자로)."""
    p = investment.product
    no = (p.product_no or "").strip()
    return (p.issued_on or date.max, p.issuer or "",
            (0, int(no), "") if no.isdigit() else (1, 0, no))


def build_row(investment):
    """투자 1건 → 17열 한 행."""
    p = investment.product
    period = period_months(p)
    issued = p.issued_on

    amount = investment.amount / AMOUNT_UNIT
    if amount == int(amount):
        amount = int(amount)
    else:
        amount = round(amount, 2)

    ym, confirmed = first_eval_ym(investment)
    if ym is None:
        schedule_note = "확인필요"
    else:
        schedule_note = "" if confirmed else "추정"

    expected = ""
    if p.yield_rate is not None and period:
        expected = _round_half_up(amount * p.yield_rate / 100 * period / 12)

    if p.is_no_ki:
        ki = "노낙인"
    else:
        ki = p.ki if p.ki is not None else ""

    return [
        p.issuer,
        p.product_no,
        assets_display(p),
        int(issued.strftime("%Y%m%d")) if issued else "",
        p.expiry_date.strftime("%y.%m.%d") if p.expiry_date else "",
        p.yield_rate if p.yield_rate is not None else "",
        amount,
        status_display(investment),
        ki,
        p.barrier_first if p.barrier_first is not None else "",
        p.barrier_last if p.barrier_last is not None else "",
        period if period else "",
        p.asset_type or "",
        int(issued.strftime("%Y%m")) if issued else "",
        ym if ym is not None else "",
        note(investment, schedule_note),
        expected,
    ]


def build_rows(investments):
    """투자 목록 → 17열 행 리스트 (조 팀장 시트 정렬)."""
    return [build_row(inv) for inv in sorted(investments, key=_sort_key)]
