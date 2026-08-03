"""
간이투자설명서(PDF)에서 **최초기준가격평가일**과 **실제 발행일**을 추출해 저장한다.

왜 필요한가
  KOFIA 응답에는 발행일 필드가 없어 Product.issue_date에 청약종료일(index 17)을
  그대로 넣어 왔다(447건 전부 issue_date == sub_end). 그 값을 기준가 산정일로 쓰는 바람에
  낙인 레벨·조기상환 판정·신호검증의 기준가가 통째로 어긋났다.
  (증권사 실제 조기상환 통지문 대조로 확인 — 평가일 시세는 정확했고 기준가만 틀렸다.)

  반면 최초기준가격평가일은 간이투자설명서에 명시돼 있다. 18개사 72건 전수조사 결과
  발행사별로 일관되며 16개사는 '발행일 당일', 삼성증권·키움증권만 '발행일 −1영업일'이다.
  → PDF에서 직접 읽어 base_eval_date에 저장하면 추정 없이 정확한 기준가를 얻는다.

동작
  - prospectus_url이 있는 상품만 대상. base_eval_date가 이미 있으면 건너뛴다(재개 가능).
  - KOFIA 파일서버는 SSL 중간인증서가 없어 verify=False 필수(경고는 억제).
  - pdfplumber로 전 페이지 텍스트를 뽑고 공백을 한 칸으로 정규화한 뒤 정규식 매칭.
    (설명서마다 자간·줄바꿈이 제각각이라 정규화 없이는 매칭이 안 된다.)
  - 패턴 미검출은 로그만 남기고 계속 진행한다. DLS·DLB 등 기타파생결합증권은
    최초기준가격 개념 자체가 없어 정상적으로 실패한다.

사용:
  python manage.py parse_prospectus_dates --limit 20     (시범)
  python manage.py parse_prospectus_dates                (미처리분 전체)
  python manage.py parse_prospectus_dates --force        (이미 채워진 것도 재파싱)
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

from core.models import Product

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


def _to_date(m):
    """정규식 매치(연,월,일 그룹) → date. 비정상 값이면 None."""
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
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


class Command(BaseCommand):
    help = "간이투자설명서 PDF에서 최초기준가격평가일·실제 발행일 추출 후 저장"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="N건만 처리(시범용, 0=전체)")
        parser.add_argument("--force", action="store_true",
                            help="이미 base_eval_date가 있는 상품도 재파싱")
        parser.add_argument("--delay", type=float, default=1.0,
                            help="요청 간격(초) — KOFIA 부담 최소화")
        parser.add_argument("--issuer", type=str, default="", help="특정 발행사만(테스트용)")

    def handle(self, *args, **opts):
        delay = opts["delay"]

        qs = Product.objects.exclude(prospectus_url="")
        if not opts["force"]:
            qs = qs.filter(base_eval_date__isnull=True)
        if opts["issuer"]:
            qs = qs.filter(issuer=opts["issuer"])
        targets = list(qs.order_by("id"))
        if opts["limit"]:
            targets = targets[: opts["limit"]]

        total = len(targets)
        self.stdout.write(f"대상: {total}건"
                          f"{' (--force: 기존값 포함)' if opts['force'] else ' (미처리분)'}")
        if not total:
            return
        self._log(f"=== 시작 {total}건 ===")

        ok = fail = 0
        fail_reasons = defaultdict(int)          # 사유 → 건수
        fail_by_type = defaultdict(int)          # 상품유형 → 실패 건수
        deltas = defaultdict(lambda: defaultdict(int))   # 발행사 → {일수차: 건수}
        started = time.time()

        try:
            for i, p in enumerate(targets, 1):
                try:
                    text = self._fetch_text(p.prospectus_url)
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

                if i % 20 == 0 or i == total:
                    elapsed = (time.time() - started) / 60
                    line = (f"진행 {i}/{total}, 성공 {ok}, 실패 {fail}, "
                            f"경과 {elapsed:.0f}m")
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

    # ------------------------------------------------------------------ 수집
    def _fetch_text(self, url):
        """설명서 PDF를 받아 전 페이지 텍스트를 공백 정규화해 반환."""
        import pdfplumber

        res = requests.get(url, timeout=TIMEOUT, verify=False, headers=HEADERS)
        res.raise_for_status()
        parts = []
        with pdfplumber.open(io.BytesIO(res.content)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        # 설명서는 자간·줄바꿈이 제각각이라 공백을 한 칸으로 눌러야 패턴이 걸린다
        return re.sub(r"\s+", " ", "\n".join(parts))

    # ------------------------------------------------------------------ 로그
    def _log(self, msg):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
        except Exception:
            pass
