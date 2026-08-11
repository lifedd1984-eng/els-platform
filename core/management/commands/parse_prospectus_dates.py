"""
간이투자설명서(PDF)에서 **최초기준가격평가일·실제 발행일·조기상환 평가일**을 추출해 저장한다.

왜 필요한가
  KOFIA 응답에는 발행일 필드가 없어 Product.issue_date에 청약종료일(index 17)을
  그대로 넣어 왔다(447건 전부 issue_date == sub_end). 그 값을 기준가 산정일로 쓰는 바람에
  낙인 레벨·조기상환 판정·신호검증의 기준가가 통째로 어긋났다.
  (증권사 실제 조기상환 통지문 대조로 확인 — 평가일 시세는 정확했고 기준가만 틀렸다.)

  반면 최초기준가격평가일은 간이투자설명서에 명시돼 있다. 18개사 72건 전수조사 결과
  발행사별로 일관되며 16개사는 '발행일 당일', 삼성증권·키움증권만 '발행일 −1영업일'이다.
  → PDF에서 직접 읽어 base_eval_date에 저장하면 추정 없이 정확한 기준가를 얻는다.

  조기상환 평가일도 같은 PDF 안에 회차별 절대 날짜로 이미 적혀 있다. 그동안은
  SEIBro 공시(backfill_eval_dates)만 원천으로 썼는데 그 수집이 일요일에만 돌아서
  신규 상품은 최대 8일간 '기준일+N개월' 근사로 남았다. 설명서는 매일 읽으므로
  등록 당일 확정값을 채울 수 있다. SEIBro 경로는 그대로 둔다 — 2026-07-18 이전
  발행분은 설명서 URL이 없어 SEIBro가 여전히 유일한 원천이다.

동작
  - prospectus_url이 있는 상품만 대상. 채울 게 없으면 건너뛴다(재개 가능).
  - KOFIA 파일서버는 SSL 중간인증서가 없어 verify=False 필수(경고는 억제).
  - pdfplumber로 전 페이지 텍스트를 뽑고 공백을 한 칸으로 정규화한 뒤 정규식 매칭.
    (설명서마다 자간·줄바꿈이 제각각이라 정규화 없이는 매칭이 안 된다.)
    표 안에서 날짜가 '2026년 11월 24 / 일'처럼 줄바꿈으로 쪼개지는 발행사가 있어
    extract_tables() 결과도 같이 들고 다니다 텍스트 파싱이 실패했을 때만 쓴다.
  - 패턴 미검출은 로그만 남기고 계속 진행한다. DLS·DLB 등 기타파생결합증권은
    최초기준가격 개념 자체가 없어 정상적으로 실패한다.

사용:
  python manage.py parse_prospectus_dates --limit 20     (시범)
  python manage.py parse_prospectus_dates                (미처리분 전체)
  python manage.py parse_prospectus_dates --force        (이미 채워진 것도 재파싱)
  python manage.py parse_prospectus_dates --verify-fixed (확정분도 읽어 대조만 — 저장 안 함)
"""

import io
import re
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests
import urllib3
from django.conf import settings
from django.core.management.base import BaseCommand

from core.management.commands.fix_currency import extract_issue_currency, seibro_conflict
from core.models import HistoricalIssue, Product

LOG_PATH = Path(settings.BASE_DIR) / "logs" / "prospectus_dates.log"

TIMEOUT = 50
HEADERS = {"User-Agent": "Mozilla/5.0"}

# KOFIA 파일서버 인증서 체인 불완전 → verify=False + 경고 억제
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 날짜 패턴 (설명서 72건 표본으로 검증, 68/72 추출 성공) ──────────────
D = r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"
BASE_PATTERNS = [
    re.compile(r"최초\s*기준\s*가격\s*평가일\s*[:：]?\s*" + D),   # 17개사 표준
    re.compile(r"최초\s*기준\s*가격\s*결정일\s*[:：]?\s*" + D),
    re.compile(r"최초\s*기준\s*가격\s*산정일\s*[:：]?\s*" + D),
    re.compile(r"기준\s*가격\s*평가일\s*[:：]?\s*" + D),
    re.compile(r"기준\s*가격\s*결정일\s*[:：]?\s*" + D),
    re.compile(r"최초\s*기준일\s*[:：]?\s*" + D),
    # 삼성증권 전용 — '○ 최초기준가격 : 2026년 07월 30일종가' (날짜 뒤에 공백 없이 '종가').
    # 콜론을 필수로 둬야 '최초기준가격의 60%' 같은 구조 설명문에 오매칭하지 않는다.
    re.compile(r"최초\s*기준\s*가격\s*[:：]\s*" + D),
]
ISSUE_PATTERN = re.compile(r"발\s*행\s*일\s*(?:\(?예정\)?)?\s*[:：]?\s*\[?\s*" + D)
DATE_RE = re.compile(D)


def _to_date(m, g=1):
    """정규식 매치(연,월,일 그룹) → date. 비정상 값이면 None."""
    if not m:
        return None
    try:
        return date(int(m.group(g)), int(m.group(g + 1)), int(m.group(g + 2)))
    except (ValueError, TypeError):
        return None


def extract_dates(text):
    """정규화된 설명서 텍스트에서 (최초기준가격평가일, 발행일) 추출. 못 찾으면 None."""
    base = None
    for pat in BASE_PATTERNS:
        base = _to_date(pat.search(text))
        if base:
            break
    issued = _to_date(ISSUE_PATTERN.search(text))
    return base, issued


# ── 조기상환 평가일 스케줄 패턴 ────────────────────────────────────────
# 설명서 999건 표본 조사 결과 포맷은 네 가지다.
#   ① 차수표     "차수 자동조기상환평가일 … 1차 2026년 11월 09일 2차 …"  (14개사)
#   ② 괄호나열   "자동조기상환평가일 : (1) 2026년 10월 22일, (2) …"      (NH투자증권)
#   ③ 중간기준가격 "중간기준가격 : 2026년 10월 23일, 2027년 01월 22일 …"  (삼성증권)
#   ④ 리자드     "2-1차 / 2-2차"처럼 한 회차가 두 줄로 쪼개진 표          (일부 상품)
# 표는 반드시 '스케줄 머리말 ~ 다음 항목' 사이에서만 읽는다. 월수익지급평가일·
# 쿠폰지급평가일이 바로 뒤에 36회짜리 표로 붙는 상품이 있어 창을 안 끊으면 섞인다.
SCHED_HEADS = [
    re.compile(r"자동\s*조기\s*상환\s*평가일\s*(?:및\s*상환\s*금액)?\s*[:：]"),
    re.compile(r"조기\s*상환\s*평가일\s*(?:및\s*상환\s*금액)?\s*[:：]"),
    re.compile(r"차\s*수\s*자동\s*조기\s*상환\s*평가일"),
]
SCHED_TAILS = [
    re.compile(r"자동\s*조기\s*상환일\s*[:：]"),
    re.compile(r"자동\s*조기\s*상환\s*금액\s*지급일"),
    re.compile(r"자동\s*조기\s*상환\s*지급일"),
    re.compile(r"조기\s*상환일\s*[:：]"),
    re.compile(r"월수익\s*지급"),
    re.compile(r"쿠폰\s*지급"),
    re.compile(r"만기\s*평가\s*가격"),
    re.compile(r"만기\s*평가일"),
    re.compile(r"손익\s*구조"),
]
# 앞에 숫자·하이픈이 붙은 "3-1차"를 "1차"로 잘못 읽으면 회차가 통째로 밀린다
TURN_RE = re.compile(r"(?<![\d\-])(\d{1,2})\s*차\s*" + D)
LIZARD_RE = re.compile(r"(?<![\d\-])(\d{1,2})\s*-\s*(\d{1,2})\s*차\s*(?:" + D + r")?")
PAREN_RE = re.compile(r"\((\d{1,2})\)\s*" + D)
SAMSUNG_MID = re.compile(r"중간\s*기준\s*가격\s*[:：]\s*((?:" + D + r"\s*[,，]?\s*)+)")
SAMSUNG_FIN = re.compile(r"최종\s*기준\s*가격\s*[:：]\s*((?:" + D + r"\s*[,，]?\s*)+)")
# 콜론이 없는 표 형태("만기평가일 2029년 07월 30일" — 교보증권)도 받는다.
# 날짜가 바로 뒤에 붙어야만 매칭되므로 "만기평가일(포함)까지" 같은 본문은 안 걸린다.
MATURITY_RE = re.compile(r"만기\s*평가일\s*[:：]?\s*((?:" + D + r"\s*[,，]?\s*)+)")


def _dates_in(s):
    return [d for d in (_to_date(m) for m in DATE_RE.finditer(s)) if d]


def _schedule_section(text):
    """조기상환 스케줄이 적힌 구간만 잘라낸다. 머리말이 없으면 None."""
    start = None
    for pat in SCHED_HEADS:
        m = pat.search(text)
        if m and (start is None or m.end() < start):
            start = m.end()
    if start is None:
        return None
    end = len(text)
    for pat in SCHED_TAILS:
        m = pat.search(text, start)
        if m and m.start() < end:
            end = m.start()
    return text[start:end]


def _turns_to_dates(turns):
    """{회차: 날짜} → 오름차순 리스트. 1..N 연속이 아니면 (None, 사유)."""
    nums = sorted(turns)
    if nums != list(range(1, len(nums) + 1)):
        return None, f"회차 불연속 {nums}"
    dates = [turns[n] for n in nums]
    if any(a >= b for a, b in zip(dates, dates[1:])):
        return None, "회차 날짜가 오름차순이 아님"
    return dates, None


def extract_early_dates(text, tables=None):
    """조기상환 평가일 목록(만기 제외)을 뽑는다. 반환 (dates, 포맷명) 또는 (None, 사유).

    본문 → 삼성 나열형 → 표 순서로 시도한다. 앞 단계가 실패해도 곧장 포기하지 않고
    다음 단계로 넘긴다 — 한화투자증권 리자드처럼 본문에서는 날짜가 쪼개져 못 읽지만
    표에서는 온전히 읽히는 상품이 있다.
    """
    dates, why = _early_dates_from_text(text)
    if dates:
        return dates, why
    tdates, twhy = _early_dates_from_tables(tables)
    if tdates:
        return tdates, "표추출"
    return None, why or twhy or "스케줄 구간 미검출"


def _early_dates_from_text(text):
    """정규화 본문에서 차수표·괄호나열·중간기준가격을 순서대로 시도한다."""
    sec = _schedule_section(text)
    if sec is not None:
        turns = {}
        conflict = None
        for m in TURN_RE.finditer(sec):
            n, d = int(m.group(1)), _to_date(m, 2)
            if d is None:
                continue
            if turns.get(n, d) != d:
                return None, f"회차 {n} 날짜 충돌"
            turns[n] = d
        lizard = list(LIZARD_RE.finditer(sec))
        if lizard:
            # "2-1차 / 2-2차"는 같은 회차의 조건 분기다. 두 줄의 날짜가 완전히
            # 같을 때만 그 회차로 인정한다. 본문에서 한쪽 날짜가 비면(병합셀)
            # 어느 쪽이 맞는지 본문만으로는 가릴 수 없으니 표 쪽에 넘긴다.
            sub = {}
            for m in lizard:
                n = int(m.group(1))
                sub.setdefault(n, set())
                d = _to_date(m, 3)
                if d:
                    sub[n].add(d)
            for n, ds in sub.items():
                if len(ds) != 1:
                    conflict = "리자드 포맷 미지원"
                    break
                d = next(iter(ds))
                if turns.get(n, d) != d:
                    conflict = f"리자드 회차 {n} 날짜 충돌"
                    break
                turns[n] = d
        if conflict:
            return None, conflict
        if turns:
            dates, why = _turns_to_dates(turns)
            return (dates, "차수표" + ("+리자드" if lizard else "")) if dates else (None, why)
        pairs = [(int(m.group(1)), _to_date(m, 2)) for m in PAREN_RE.finditer(sec)]
        pairs = [(n, d) for n, d in pairs if d]
        if pairs:
            if [n for n, _ in pairs] != list(range(1, len(pairs) + 1)):
                return None, "괄호 번호 불연속"
            dates = [d for _, d in pairs]
            if any(a >= b for a, b in zip(dates, dates[1:])):
                return None, "회차 날짜가 오름차순이 아님"
            return dates, "괄호나열"
    m = SAMSUNG_MID.search(text)
    if m:
        dates = _dates_in(m.group(1))
        if dates and all(a < b for a, b in zip(dates, dates[1:])):
            return dates, "중간기준가격"
        return None, "중간기준가격 날짜 이상"
    return None, None


def _early_dates_from_tables(tables):
    """extract_tables() 결과에서 차수표를 찾는다. 본문 파싱이 실패했을 때만 쓴다.

    한화투자증권처럼 표 칸 안에서 '2026년 11월 24 / 일'로 줄이 갈리면
    본문만으로는 날짜가 붙지 않는다. 표는 열이 살아 있어 그대로 읽힌다.
    리자드 병합셀은 pdfplumber가 윗줄에 값을 주고 아랫줄을 비워 두므로
    '아랫줄이 비어 있다'는 사실 자체가 구조적 근거다 — 추측이 아니다.
    """
    if not tables:
        return None, None
    for tb in tables:
        if not tb or len(tb) < 3:
            continue
        head = re.sub(r"\s+", "", " ".join(str(c or "") for c in tb[0]))
        if "차수" not in head or "조기상환평가일" not in head:
            continue
        turns = {}
        for row in tb[1:]:
            cells = [re.sub(r"\s+", " ", str(c or "")).strip() for c in row]
            label = next((c for c in cells if re.fullmatch(r"\d{1,2}(-\d{1,2})?\s*차", c)), None)
            if not label:
                continue
            n = int(label.split("차")[0].split("-")[0])
            found = [d for c in cells if c != label for d in _dates_in(c)]
            if not found:
                if "-" in label:
                    continue          # 병합셀 아랫줄 — 윗줄 날짜가 이 회차의 값이다
                return None, "표에 회차 날짜 없음"
            if len(set(found)) != 1 or turns.get(n, found[0]) != found[0]:
                return None, f"표 회차 {n} 날짜 충돌"
            turns[n] = found[0]
        if turns:
            dates, why = _turns_to_dates(turns)
            return (dates, None) if dates else (None, why)
    return None, None


def extract_maturity_date(text, samsung=False):
    """만기평가일. 여러 날짜가 나열되면 **가장 늦은 날**을 쓴다.

    기초자산별로 다른 날짜가 아니라 '종가 N개의 산술평균'을 쓰는 상품이라
    마지막 관찰이 끝나야 손익이 확정된다. 설명서도 그렇게 못박아 뒀다 —
    미래에셋증권 제37977호 '최종관찰일 : 만기평가일(만기평가일이 복수인 경우
    그 중 마지막 날)'.

    산술로도 확인했다. 후보가 둘 이상인 설명서 75건 전부에서
    '가장 늦은 후보 + N영업일 == 만기일'이 성립했고(N은 그 설명서에 적힌
    상환일 규칙), 다른 후보로는 한 건도 맞지 않았다. 예: 키움 4126호는
    후보 [07/19, 07/20, 07/23]에 만기일 07/25 — 07/23 + 2영업일로만 맞는다.
    """
    if samsung:
        m = SAMSUNG_FIN.search(text)      # 삼성증권은 '최종기준가격'이 만기평가일이다
        if m:
            cands = _dates_in(m.group(1))
            if cands:
                return max(cands)
    m = MATURITY_RE.search(text)
    if m:
        cands = _dates_in(m.group(1))
        if cands:
            return max(cands)
    return None


# 만기평가일과 만기일(상환 지급일)의 간격. 설명서상 '만기평가일 후 2~6영업일'이라
# 설명서 457건 실측 분포는 +2~+12일이었다. 넉넉히 14일로 잡되 음수는 허용하지 않는다.
MATURITY_GAP_MAX_DAYS = 14


def extract_schedule(product, text, tables=None):
    """상품에 저장할 평가일 전체(조기상환 + 만기평가일). 반환 (dates, 포맷) 또는 (None, 사유)."""
    from datetime import timedelta

    bars = product.barriers_raw or []
    if not bars:
        return None, "배리어 없음"
    early, why = extract_early_dates(text, tables)
    if early is None:
        return None, why
    maturity = extract_maturity_date(text, samsung=(why == "중간기준가격"))
    if maturity is None:
        return None, "만기평가일 미검출"
    dates = early + [maturity]
    # 회차 수가 배리어와 맞을 때만 저장한다 — Product.fixed_eval_dates의 판정과
    # 같은 안전장치다. 부분 일치는 회차 정렬이 어긋나 신뢰할 수 없다.
    if len(dates) != len(bars):
        return None, f"회차 {len(dates)}개 vs 배리어 {len(bars)}개"
    if any(a >= b for a, b in zip(dates, dates[1:])):
        return None, "평가일이 오름차순이 아님"
    base = product.base_eval_date or product.sub_end
    if base and dates[0] <= base:
        return None, f"1회차({dates[0]})가 기준일({base}) 이전"
    exp = product.expiry_date
    if exp and not (timedelta(0) <= exp - maturity <= timedelta(days=MATURITY_GAP_MAX_DAYS)):
        return None, f"만기평가일({maturity})과 만기일({exp}) 간격 이상"
    return dates, why


# 청약종료일 대비 허용 창 — 실측 437건 분포는 0일 328건 / +1일 108건 / +3일 1건이라
# +14일이면 충분히 넉넉하고, 다른 차수의 설명서(보통 한 달 이상 떨어짐)는 확실히 막는다.
SANE_BACK_DAYS = 7
SANE_FWD_DAYS = 14


def _dates_sane(product, base, issued):
    """추출한 날짜가 이 상품의 청약종료일 근처인지 검증.

    같은 상품번호가 차수마다 재사용되는 발행사가 있어(메리츠 등) 다른 차수의
    설명서를 읽으면 엉뚱한 날짜가 들어온다. (2026-08-03)

    경계는 둘 다 **청약종료일** 기준으로 잡는다. 예전엔 상한이 만기일이라
    여유가 중앙값 1,098일이었고, 같은 상품번호의 다른 차수 날짜가 만기 안쪽이면
    그대로 통과했다 — 가드가 사실상 없었다.
    하한도 sub_start를 먼저 봤는데, sub_start가 발행일보다 미래인 상품이 있어
    (유안타증권 등) 정상 날짜까지 기각됐다. 유안타는 상품마다 값이 달라
    설명서 파싱이 반드시 필요한 발행사라 그대로 두면 못 채운다. (2026-08-04)

    청약종료일이 아예 없으면 예전 상한(만기일)만 적용한다 — 없는 것보다 낫다.
    """
    from datetime import timedelta

    anchor = product.sub_end or product.sub_start
    if not anchor:
        hi = product.expiry_date
        return not (hi and any(d and d > hi for d in (base, issued)))
    lo = anchor - timedelta(days=SANE_BACK_DAYS)
    hi = anchor + timedelta(days=SANE_FWD_DAYS)
    return all(lo <= d <= hi for d in (base, issued) if d)


def needs_parse(p):
    """이 상품에서 설명서로 아직 채울 게 남았는지. 전부 확정이면 내려받지 않는다.

    평가일 쪽은 배리어가 있는 상품만 본다. ELB·DLB는 조기상환 회차 자체가 없어
    영원히 '미확정'으로 남는데, 그걸 대상으로 두면 매일 116건을 헛되이 내려받는다.
    """
    if p.base_eval_date is None:
        return True
    return bool(p.barriers_raw) and p.fixed_eval_dates is None


class Command(BaseCommand):
    help = "간이투자설명서 PDF에서 최초기준가격평가일·실제 발행일·조기상환 평가일 추출 후 저장"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="N건만 처리(시범용, 0=전체)")
        parser.add_argument("--force", action="store_true",
                            help="이미 채워진 상품도 재파싱(확정 평가일은 --force로도 덮어쓰지 않는다)")
        parser.add_argument("--verify-fixed", action="store_true",
                            help="확정 평가일이 있는 상품도 읽어 설명서와 대조만 한다(저장 안 함)")
        parser.add_argument("--delay", type=float, default=1.0,
                            help="요청 간격(초) — KOFIA 부담 최소화")
        parser.add_argument("--issuer", type=str, default="", help="특정 발행사만(테스트용)")

    def handle(self, *args, **opts):
        delay = opts["delay"]

        qs = Product.objects.exclude(prospectus_url="")
        if opts["issuer"]:
            qs = qs.filter(issuer=opts["issuer"])
        targets = list(qs.order_by("id"))
        if not (opts["force"] or opts["verify_fixed"]):
            # 채울 게 남은 상품만. base_eval_date는 채워졌는데 평가일이 근사인 건도
            # 여기서 걸린다 — 예전 조건(base_eval_date__isnull=True)은 그런 건을 놓쳤다.
            targets = [p for p in targets if needs_parse(p)]
        if opts["limit"]:
            targets = targets[: opts["limit"]]

        total = len(targets)
        mode = " (--force: 기존값 포함)" if opts["force"] else (
            " (--verify-fixed: 확정분 대조 포함)" if opts["verify_fixed"] else " (미처리분)")
        self.stdout.write(f"대상: {total}건{mode}")
        if not total:
            return
        self._log(f"=== 시작 {total}건{mode} ===")

        ok = fail = 0
        sched_ok = sched_fail = sched_same = sched_diff = 0
        fail_reasons = defaultdict(int)          # 사유 → 건수
        fail_by_type = defaultdict(int)          # 상품유형 → 실패 건수
        sched_reasons = defaultdict(int)         # 평가일 실패 사유 → 건수
        cur_counts = defaultdict(int)            # 발행통화 판정 → 건수
        cur_changed = []                         # 발행통화가 바뀐 상품
        sched_formats = defaultdict(int)         # 포맷 → 성공 건수
        deltas = defaultdict(lambda: defaultdict(int))   # 발행사 → {일수차: 건수}
        started = time.time()

        try:
            for i, p in enumerate(targets, 1):
                try:
                    text, tables = self._fetch_text(p.prospectus_url)
                except Exception as e:
                    fail += 1
                    fail_reasons["다운로드/PDF오류"] += 1
                    fail_by_type[p.product_type] += 1
                    self._log(f"  [{p.id}] {p.issuer} {p.product_no} 다운로드 실패: {e}")
                    time.sleep(delay)
                    continue

                base, issued = extract_dates(text)
                if not base:
                    fail += 1
                    fail_reasons["평가일 패턴 미검출"] += 1
                    fail_by_type[p.product_type] += 1
                    self._log(f"  [{p.id}] {p.issuer} {p.product_no}({p.product_type}) "
                              f"평가일 미검출")
                elif not _dates_sane(p, base, issued):
                    # 다른 차수의 설명서를 읽었거나 파싱이 어긋난 경우 —
                    # 만기 뒤 발행일 같은 값이 들어가면 화면·계산이 통째로 망가진다
                    # (실제 사고: 메리츠 4~5월 상품 4건에 8월 차수 날짜 주입)
                    fail += 1
                    fail_reasons["날짜 범위 이상"] += 1
                    fail_by_type[p.product_type] += 1
                    self._log(f"  [{p.id}] {p.issuer} {p.product_no} 날짜 범위 이상 "
                              f"(청약 {p.sub_start}~{p.sub_end} / 만기 {p.expiry_date} "
                              f"vs 기준일 {base} / 발행 {issued}) — 저장 안 함")
                else:
                    fields = ["base_eval_date"]
                    p.base_eval_date = base
                    if issued:
                        p.real_issue_date = issued
                        fields.append("real_issue_date")
                    p.save(update_fields=fields)
                    ok += 1
                    if p.issue_date:
                        deltas[p.issuer][(base - p.issue_date).days] += 1

                verdict, detail = self._handle_schedule(p, text, tables)
                if verdict == "저장":
                    sched_ok += 1
                    sched_formats[detail] += 1
                elif verdict == "동일":
                    sched_same += 1
                elif verdict == "상충":
                    sched_diff += 1
                else:
                    sched_fail += 1
                    sched_reasons[detail] += 1

                cur_verdict, cur_detail = self._handle_currency(p, text)
                cur_counts[cur_verdict] += 1
                if cur_verdict == "저장":
                    cur_changed.append(f"{p.issuer} {p.product_no}: {cur_detail}")

                if i % 20 == 0 or i == total:
                    elapsed = (time.time() - started) / 60
                    line = (f"진행 {i}/{total}, 성공 {ok}, 실패 {fail}, "
                            f"평가일 {sched_ok}, 경과 {elapsed:.0f}m")
                    self.stdout.write("  " + line)
                    self._log(line)

                time.sleep(delay)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING(
                "\n중단 요청 — 여기까지 저장됐습니다(재실행 시 이어집니다)."))

        summary = f"처리 {ok + fail}/{total} / 성공 {ok} / 실패 {fail}"
        self.stdout.write(self.style.SUCCESS(summary))
        self._log(summary)

        for reason, cnt in sorted(fail_reasons.items(), key=lambda x: -x[1]):
            line = f"  실패사유 {reason}: {cnt}건"
            self.stdout.write(line)
            self._log(line)
        if fail_by_type:
            line = "  실패 상품유형: " + ", ".join(
                f"{t or '-'} {c}건" for t, c in sorted(fail_by_type.items(), key=lambda x: -x[1]))
            self.stdout.write(line)
            self._log(line)

        # ── 조기상환 평가일 결과 ──
        line = (f"[조기상환 평가일] 저장 {sched_ok}건 / 미저장 {sched_fail}건 / "
                f"확정분 대조 일치 {sched_same}건 / 상충 {sched_diff}건")
        self.stdout.write(self.style.SUCCESS(line))
        self._log(line)
        for f_, c in sorted(sched_formats.items(), key=lambda x: -x[1]):
            line = f"  포맷 {f_}: {c}건"
            self.stdout.write(line)
            self._log(line)
        for reason, cnt in sorted(sched_reasons.items(), key=lambda x: -x[1]):
            line = f"  평가일 미저장 사유 {reason}: {cnt}건"
            self.stdout.write(line)
            self._log(line)

        # ── 발행통화 결과 ──
        if cur_counts:
            detail = " / ".join(f"{k} {v}건" for k, v
                                in sorted(cur_counts.items(), key=lambda x: -x[1]))
        else:
            detail = "처리 없음"
        line = f"[발행통화] {detail}"
        self.stdout.write(self.style.SUCCESS(line))
        self._log(line)
        for c in cur_changed[:20]:
            self.stdout.write(f"  통화 보정 {c}")

        # ── 발행사별 (평가일 − issue_date) 분포 ──
        # issue_date는 실제로는 청약종료일이므로 이 차이가 곧 '며칠 뒤가 기준일인가'다.
        if deltas:
            self.stdout.write("\n발행사별 (최초기준가격평가일 − issue_date) 일수 분포:")
            for issuer in sorted(deltas):
                dist = deltas[issuer]
                n = sum(dist.values())
                detail = ", ".join(f"{d:+d}일 {c}건"
                                   for d, c in sorted(dist.items()))
                line = f"  {issuer} (n={n}): {detail}"
                self.stdout.write(line)
                self._log(line)

    # ------------------------------------------------------- 조기상환 평가일
    def _handle_schedule(self, p, text, tables):
        """평가일 스케줄을 저장하거나 대조한다. 반환 (판정, 포맷 또는 사유)."""
        dates, why = extract_schedule(p, text, tables)
        if dates is None:
            self._log(f"  [{p.id}] {p.issuer} {p.product_no} 평가일 미저장: {why}")
            return "미저장", why
        iso = [d.isoformat() for d in dates]
        fixed = p.fixed_eval_dates
        if fixed:
            # SEIBro로 이미 확정된 건은 덮어쓰지 않는다. 다르면 그 자체가 신호다.
            cur = [d.isoformat() for d in fixed]
            if cur == iso:
                return "동일", why
            self._log(f"  [{p.id}] {p.issuer} {p.product_no} 확정 평가일과 설명서가 다름 "
                      f"(기존 {cur} vs 설명서 {iso}) — 덮어쓰지 않음")
            return "상충", why
        p.eval_dates = iso
        p.save(update_fields=["eval_dates"])
        self._log(f"  [{p.id}] {p.issuer} {p.product_no} 평가일 {len(iso)}회차 저장({why}) "
                  f"{iso[0]}~{iso[-1]}")
        return "저장", why

    # ------------------------------------------------------------------ 발행통화
    def _handle_currency(self, p, text):
        """설명서 액면가액에서 발행통화를 읽어 저장한다. 반환 (판정, 상세).

        왜 여기인가
          Product.currency는 수집 단계에서 상품설명 문자열로만 정해진다. 그런데
          KOFIA 청약중 응답에는 통화 필드가 아예 없고(val 전수 확인), 설명이
          통화를 말하지 않는 상품이 대부분이라 외화 상품이 원화로 굳어 왔다
          (2026-08-07 실측 55건). SEIBro 등록통화는 발행 **뒤에** 들어오므로
          수집 시점엔 쓸 수 없다.

          설명서는 청약 시작과 함께 나오고, 이 명령이 이미 그 PDF를 내려받아
          텍스트를 손에 들고 있다. 통화를 여기서 읽으면 **추가 요청이 0건**이고
          등록 당일 확정된다. 평가일을 같은 이유로 여기서 읽는 것과 같은 결이다.

        무엇을 근거로 보는가
          '1증권당 액면가액' 항목 하나만 본다. 설명서 본문의 '통화' 낱말은
          위험등급 안내문에 통화 이름이 나열돼 있어 근거가 되지 못한다
          (자세한 이유는 fix_currency 파일 첫머리 참조).
        """
        code, evidence = extract_issue_currency(text)
        if not code:
            return "미검출", ""
        if code == p.currency:
            return "동일", code

        # SEIBro 등록통화(예탁결제원 원부)와 어긋나면 저장하지 않는다.
        # 다른 차수의 설명서를 읽었을 때 통화가 통째로 뒤집히는 것을 막는다.
        issue = (HistoricalIssue.objects.filter(isin=p.product_code).first()
                 if p.product_code else None)
        registered = issue.currency_name if issue else ""
        if seibro_conflict(code, registered):
            self._log(f"  [{p.id}] {p.issuer} {p.product_no} 발행통화 상충 "
                      f"(설명서 {code} vs SEIBro {registered!r}) — 저장 안 함")
            return "상충", code

        before = p.currency
        p.currency = code
        p.save(update_fields=["currency"])
        self._log(f"  [{p.id}] {p.issuer} {p.product_no} 발행통화 {before!r} → {code!r} "
                  f"(근거: {evidence[:80]})")
        return "저장", code

    # ------------------------------------------------------------------ 수집
    def _fetch_text(self, url):
        """설명서 PDF를 받아 (공백 정규화한 전문, 표 목록)을 반환."""
        import pdfplumber

        res = requests.get(url, timeout=TIMEOUT, verify=False, headers=HEADERS)
        res.raise_for_status()
        parts, tables = [], []
        with pdfplumber.open(io.BytesIO(res.content)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
                try:
                    tables.extend(page.extract_tables() or [])
                except Exception:
                    # 표 추출은 보조 경로다 — 실패해도 텍스트 파싱은 계속해야 한다
                    pass
        # 설명서는 자간·줄바꿈이 제각각이라 공백을 한 칸으로 눌러야 패턴이 걸린다
        return re.sub(r"\s+", " ", "\n".join(parts)), tables

    # ------------------------------------------------------------------ 로그
    def _log(self, msg):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
        except Exception:
            pass
