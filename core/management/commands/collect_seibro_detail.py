"""
SEIBro 개별상품 상세 API로 과거 공모 ELS의 상세정보를 **전수 수집**한다.

표본조사용 sample_seibro_ki.py와 달리 낙인(KI)뿐 아니라
조기상환 배리어 / 회차별 수익률 / 중간평가일까지 모두 긁어와
레이더 신호의 과거 성과검증(백테스팅) 표본을 78,000건 규모로 키우는 것이 목적이다.

브라우저(playwright) 없이 순수 requests 세션으로 호출한다.
목록 화면을 한 번 GET해 세션 쿠키(WMONID/JSESSIONID)를 받아두면 WAF를 통과한다.
→ 윈도우 로컬/EC2(리눅스 ARM64) 어디서든 동일하게 동작한다.

호출 API (ISIN 하나당 2회)
  1) bassetInfoList          → "기준가대비 하한베리어 비율" = 낙인(KI)
  2) midValatSkedulRedCondiList
        <MID_VALAT_EXPRY_DT>   평가일(YYYYMMDD)
        <RED_CONDI_CONTENT>    조기상환 조건문 → 배리어(%)
        <RED_FORMULA_CONTENT>  상환금액 산식  → 회차 누적수익률(%)
     세 리스트는 같은 순서로 대응된다.

특징
  - detail_fetched 플래그로 **재개 가능**(20시간이 넘는 실행이 끊겨도 이어서 돌린다).
  - 100건마다 DB 커밋 + logs/seibro_collect.log 진행 기록.
  - 조기상환(중간평가) 행만 필수 파싱 대상. 만기상환 행은 손실구조 설명이라 형식이
    제각각이고 조건/산식 칸이 밀려 들어온 응답도 있어 best-effort로만 다룬다
    (만기 배리어가 읽히면 stepdown_barriers 마지막에 덧붙인다).
  - 파싱 실패는 detail_fetched=True로 마킹하고 parse_error에 사유만 남긴다(재조회 낭비 방지).
  - 통신 실패는 **마킹하지 않는다** — 세션 만료·차단으로 수만 건이 영구 결측되는 사고 방지.
    연속 20건 실패 → 세션 재생성, 연속 50건 실패 → 마킹 없이 중단.
  - 1,000건마다 세션 쿠키 선제 갱신(장시간 실행 대비).
  - Ctrl+C 시 현재까지 수집분을 저장하고 안전 종료.

사용:
  python manage.py collect_seibro_detail --limit 200            (시범)
  python manage.py collect_seibro_detail                        (2016~2024 전수)
  python manage.py collect_seibro_detail --issuer 미래에셋증권   (특정 발행사만)
"""

import html
import re
import statistics
import time
from datetime import date, datetime
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import HistoricalIssue

LIST_URL = "https://seibro.or.kr/websquare/control.jsp?w2xPath=/IPORTAL/user/derivCombi/BIP_CNTS07015V.xml&menuNo=199"
API_URL = "https://seibro.or.kr/websquare/engine/proworks/callServletService.jsp"

BASSET_TMPL = '<reqParam action="bassetInfoList" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask"><ISIN value="{isin}"/></reqParam>'
SKED_TMPL = '<reqParam action="midValatSkedulRedCondiList" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask"><ISIN value="{isin}"/></reqParam>'

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "Chrome/126 Safari/537.36"),
    "Referer": LIST_URL,
    "Content-Type": "text/xml",
    "X-Requested-With": "XMLHttpRequest",
}
TIMEOUT = 25

# 낙인 — sample_seibro_ki.py와 동일
KI_RE = re.compile(
    r'GUBUN value="기준가대비 하한베리어 비율"/><BASSET1 value="([^"]*)"/><BASSET2 value="([^"]*)"/><BASSET3 value="([^"]*)"'
)

# SKED 응답 태그 (<result> 블록 하나 = 상환 회차 한 줄)
BLOCK_RE = re.compile(r"<result>(.*?)</result>", re.S)
DT_RE = re.compile(r'MID_VALAT_EXPRY_DT value="(\d{8})"')
TPCD_RE = re.compile(r'RED_CONDI_TPCD value="([^"]*)"')       # 중간평가 / 만기상환
CONDI_RE = re.compile(r'RED_CONDI_CONTENT value="([^"]*)"')
FORMULA_RE = re.compile(r'RED_FORMULA_CONTENT value="([^"]*)"')

# 배리어/수익률 파서 (13개 증권사 × 4개 연도 51건 실표본 검증: 배리어 98%, 수익률 100%)
BAR_MAIN = re.compile(r'[최기]초기준가격[의\s]*[\[\(]?\s*[XxⅹxX×*]?\s*[\[\(]?\s*([\d.]+)\s*%')
BAR_ALT = re.compile(r'상환지표가\s*([\d.]+)\s*%')  # 신한 구형
Y_ADD = re.compile(r'\(\s*(?:100\s*%|1)\s*\+\s*([\d.]+)\s*%')
Y_ONLY = re.compile(r'\(\s*100\s*%\s*\)')  # (100%) = 수익 0
Y_MULT = re.compile(r'액면[^()]*?(?:의|[XxⅹxX×*])\s*([\d.]+)\s*%')

FLUSH_EVERY = 100        # DB 커밋 주기
SESSION_REFRESH = 1000   # 세션 쿠키 선제 갱신 주기
FAIL_RESET = 20          # 연속 실패 N건마다 세션 재생성
FAIL_ABORT = 50          # 연속 실패 N건 → 마킹 없이 중단
LOG_PATH = Path(settings.BASE_DIR) / "logs" / "seibro_collect.log"


class FetchError(Exception):
    """통신/세션 문제 — 파싱 결측과 구분해 detail_fetched 마킹을 하지 않는다."""


def parse_barrier(text):
    """조기상환 조건문에서 배리어(%) 추출."""
    m = BAR_MAIN.search(text) or BAR_ALT.search(text)
    return round(float(m.group(1))) if m else None


def parse_yield(text):
    """상환금액 산식에서 해당 회차 누적수익률(%) 추출."""
    m = Y_ADD.search(text)
    if m:
        return float(m.group(1))
    if Y_ONLY.search(text):
        return 0.0
    m = Y_MULT.search(text)
    if m:
        v = float(m.group(1))
        return round(v - 100, 4) if v >= 100 else v
    return None


def _parse_sked(xml):
    """SKED 응답을 회차 행 리스트로. [{tpcd, date, condi, formula}, ...]

    한 <result> 블록이 한 행이므로 블록 단위로 뽑아야 평가일·조건·산식이 어긋나지 않는다.
    """
    rows = []
    for blk in BLOCK_RE.findall(xml):
        tpcd = TPCD_RE.search(blk)
        dt = DT_RE.search(blk)
        condi = CONDI_RE.search(blk)
        formula = FORMULA_RE.search(blk)
        rows.append({
            "tpcd": html.unescape(tpcd.group(1)) if tpcd else "",
            "date": _to_date(dt.group(1)) if dt else None,
            "condi": html.unescape(condi.group(1)) if condi else "",
            "formula": html.unescape(formula.group(1)) if formula else "",
        })
    return rows


def _row(**kw):
    """HistoricalIssue 업데이트용 dict (누락 키는 None/빈값)."""
    row = {
        "ki": None, "stepdown_barriers": None, "eval_dates": None,
        "step_yields": None, "yield_rate": None, "period_months": None,
        "first_eval_months": None, "parse_error": "",
    }
    row.update(kw)
    return row


def _to_date(s):
    if not s or len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def months_between(d1, d2):
    """d1→d2 개월수. 연·월 차이를 기본으로 하고 '일' 차이가 크면 ±1 보정한다.

    (평가일은 발행일 기준 n개월 뒤 영업일이라 며칠씩 앞뒤로 밀린다.)
    """
    if not d1 or not d2:
        return None
    months = (d2.year - d1.year) * 12 + (d2.month - d1.month)
    diff = d2.day - d1.day
    if diff <= -15:
        months -= 1
    elif diff >= 15:
        months += 1
    return months


class Command(BaseCommand):
    help = "과거 공모 ELS 상세정보(낙인·배리어·수익률·평가일) 전수 수집"

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int, default=2016)
        parser.add_argument("--end-year", type=int, default=2024)
        parser.add_argument("--limit", type=int, default=0, help="N건만 처리(시범용, 0=전체)")
        parser.add_argument("--delay", type=float, default=0.3, help="요청 간격(초)")
        parser.add_argument("--issuer", type=str, default="", help="특정 발행사만(테스트용)")

    def handle(self, *args, **opts):
        self.delay = opts["delay"]
        self.session = None

        qs = HistoricalIssue.objects.filter(
            product_type="ELS",
            recu_whcd="공모",
            issue_date__year__range=(opts["start_year"], opts["end_year"]),
            detail_fetched=False,
        )
        if opts["issuer"]:
            qs = qs.filter(issuer=opts["issuer"])
        targets = list(qs.order_by("issue_date").values_list("isin", "issue_date"))
        if opts["limit"]:
            targets = targets[: opts["limit"]]

        total = len(targets)
        self.stdout.write(f"수집 대상: {total}건 "
                          f"({opts['start_year']}~{opts['end_year']}, 공모 ELS, 미조회분)")
        if not total:
            return

        self._log(f"=== 시작 {total}건 ({opts['start_year']}~{opts['end_year']}"
                  f"{', ' + opts['issuer'] if opts['issuer'] else ''}) ===")

        stats = {"done": 0, "ki": 0, "bar": 0, "yld": 0, "miss": 0, "fail": 0}
        errors = {}
        buf = []  # flush 대기 (최대 FLUSH_EVERY건 — 메모리에 전부 쌓지 않는다)
        started = time.time()
        seen = 0     # 시도 건수(세션 갱신 주기 계산용)
        streak = 0   # 연속 통신 실패
        status = "완료"

        try:
            self._new_session()
            for isin, issue_date in targets:
                seen += 1
                if seen % SESSION_REFRESH == 0:
                    # 장시간 실행 대비 선제 갱신 — 서버가 세션을 만료시키기 전에 새로 받는다
                    self._new_session("주기 갱신")

                try:
                    row = self._collect_one(isin, issue_date)
                except KeyboardInterrupt:
                    raise
                except FetchError as e:
                    # 통신/세션 문제 — 마킹하지 않고 넘긴다(재실행 시 다시 시도된다)
                    streak += 1
                    stats["fail"] += 1
                    self._log(f"  [{isin}] 통신실패({streak}연속): {e}")
                    if streak >= FAIL_ABORT:
                        status = "중단(연속실패)"
                        self.stdout.write(self.style.ERROR(
                            f"\n연속 {streak}건 통신 실패 — 서버 차단/네트워크 장애로 판단해 중단합니다. "
                            f"(실패분은 미조회 상태로 남아 재실행 시 이어집니다)"))
                        break
                    if streak % FAIL_RESET == 0:
                        self._new_session(f"연속 {streak}건 실패")
                    time.sleep(min(self.delay * streak, 10))
                    continue
                except Exception as e:
                    # 예상 못한 개별 예외 — 데이터 문제로 보고 결측 마킹
                    row = _row(parse_error="응답없음")
                    self._log(f"  [{isin}] 예외: {e}")

                streak = 0
                buf.append((isin, row))
                stats["done"] += 1
                if row.get("ki") is not None:
                    stats["ki"] += 1
                if row.get("stepdown_barriers"):
                    stats["bar"] += 1
                if row.get("step_yields"):
                    stats["yld"] += 1
                if row.get("parse_error"):
                    stats["miss"] += 1
                    for reason in row["parse_error"].split(","):
                        errors[reason] = errors.get(reason, 0) + 1

                if len(buf) >= FLUSH_EVERY:
                    self._flush(buf)
                    self._progress(stats, total, started)
        except KeyboardInterrupt:
            status = "중단(사용자)"
            self.stdout.write(self.style.WARNING("\n중단 요청 — 현재까지 저장 후 종료합니다."))
        except FetchError as e:
            # 세션 자체를 못 만드는 상황(차단·네트워크 단절) — 저장만 하고 종료
            status = "중단(세션실패)"
            self.stdout.write(self.style.ERROR(f"\n{e} — 현재까지 저장 후 종료합니다."))
        finally:
            self._flush(buf)
            self._close_session()

        elapsed = (time.time() - started) / 60
        summary = (
            f"{status}: 처리 {stats['done']}/{total} "
            f"/ 낙인 {stats['ki']} / 배리어 {stats['bar']} / 수익률 {stats['yld']} "
            f"/ 결측 {stats['miss']} / 통신실패(미마킹) {stats['fail']} / 경과 {elapsed:.0f}m"
        )
        self.stdout.write(self.style.SUCCESS(summary))
        self._log(summary)
        for reason, cnt in sorted(errors.items(), key=lambda x: -x[1]):
            line = f"  결측사유 {reason}: {cnt}건"
            self.stdout.write(line)
            self._log(line)

    # ------------------------------------------------------------------ 수집
    def _collect_one(self, isin, issue_date):
        """ISIN 1건 = BASSET(낙인) + SKED(배리어/수익률/평가일) 2회 호출 후 파싱."""
        reasons = []

        basset = self._post(BASSET_TMPL.format(isin=isin))
        time.sleep(self.delay)
        sked = self._post(SKED_TMPL.format(isin=isin))
        time.sleep(self.delay)

        # 낙인
        ki = None
        m = KI_RE.search(basset)
        if m:
            vals = [int(x) for x in m.groups() if x.strip().isdigit()]
            ki = min(vals) if vals else None
        if ki is None:
            reasons.append("낙인없음")

        # SKED 응답은 <result> 블록 단위로 조기상환(중간평가) 행과 만기상환 행이 섞여 있다.
        # 블록별로 뽑아야 평가일·조건·산식의 대응이 어긋나지 않는다.
        rows = _parse_sked(sked)
        mids = [r for r in rows if "중간" in r["tpcd"]]
        matures = [r for r in rows if "만기" in r["tpcd"]]

        # 응답은 정상인데 쓸 내용이 하나도 없다 = 상세 미공개 종목
        if ki is None and not rows:
            return _row(parse_error="응답없음")

        if not mids:
            reasons.append("조기상환없음")

        # --- 조기상환(중간평가) 행: 필수 파싱 대상 -------------------------------
        # 하나라도 실패하면 백테스팅에 쓸 수 없으므로 통째로 결측 처리한다.
        barriers = [parse_barrier(r["condi"]) for r in mids]
        if mids and all(b is not None for b in barriers):
            stepdown = barriers
        else:
            stepdown = None
            if mids:
                reasons.append("배리어없음")

        yields = [parse_yield(r["formula"]) for r in mids]
        if mids and all(y is not None for y in yields):
            # 전 회차가 0% = "액면금액 ×100%" 식으로만 공시된 종목.
            # 조기상환에 쿠폰이 0일 수는 없으므로 실제 수익률 미공시로 보고 결측 처리한다.
            if any(y > 0 for y in yields):
                step_yields = yields
            else:
                step_yields = None
                reasons.append("수익률미공시")
        else:
            step_yields = None
            if mids:
                reasons.append("수익률없음")

        dts = [r["date"] for r in mids if r["date"]]
        if mids and not dts:
            reasons.append("평가일없음")

        # --- 만기상환 행: best-effort (실패해도 결측 처리하지 않는다) ------------
        # 만기 행은 손실구조 설명이라 형식이 제각각이고, 조건/산식 칸이 밀려 들어온
        # 응답도 있다. 첫 행(수익상환 조건 "N% 이상")의 배리어만 신뢰하고,
        # 두 번째 행("N% 미만" 손실조건)은 무시한다.
        # 서비스의 barriers(Product.barrier_last)와 형식을 맞추기 위해 맨 뒤에 덧붙인다.
        if stepdown and matures:
            last_bar = parse_barrier(matures[0]["condi"])
            if last_bar is not None:
                stepdown = stepdown + [last_bar]

        eval_dates = [d.isoformat() for d in dts] or None
        period_months = first_eval_months = yield_rate = None

        if dts:
            first_eval_months = months_between(issue_date, dts[0])
            if len(dts) >= 2:
                gaps = [months_between(dts[i], dts[i + 1]) for i in range(len(dts) - 1)]
                gaps = [g for g in gaps if g and g > 0]
                if gaps:
                    period_months = int(round(statistics.median(gaps)))
            elif first_eval_months and first_eval_months > 0:
                period_months = first_eval_months

            # 연환산 수익률 = 마지막 회차 누적수익 ÷ (발행일~마지막 평가일 개월수) × 12
            if step_yields:
                n = min(len(dts), len(step_yields))
                total_months = months_between(issue_date, dts[n - 1]) if n else None
                if n and total_months and total_months > 0:
                    yield_rate = round(step_yields[n - 1] / total_months * 12, 2)

        return _row(
            ki=ki,
            stepdown_barriers=stepdown,
            eval_dates=eval_dates,
            step_yields=step_yields,
            yield_rate=yield_rate,
            period_months=period_months,
            first_eval_months=first_eval_months,
            parse_error=",".join(reasons)[:60],
        )

    def _post(self, body):
        """상세 API 호출. 실패하면 세션을 새로 받아 재시도(총 3회), 끝내 실패하면 FetchError."""
        last = None
        for attempt in range(3):
            try:
                res = self.session.post(API_URL, data=body.encode("utf-8"), timeout=TIMEOUT)
                if res.status_code != 200:
                    raise FetchError(f"HTTP {res.status_code}")
                if not res.text.strip():
                    raise FetchError("빈 응답")
                return res.text
            except KeyboardInterrupt:
                raise
            except Exception as e:
                last = e
                if attempt < 2:
                    time.sleep(1 + attempt * 2)
                    self._new_session(f"요청 실패 재시도 {attempt + 1}/2: {e}")
        raise FetchError(str(last))

    # ------------------------------------------------------------------ 세션
    def _new_session(self, why=""):
        """세션을 새로 만들고 목록 화면 GET으로 쿠키(WMONID/JSESSIONID)를 받는다.

        쿠키 없이 API를 때리면 WAF에 막히므로 이 GET이 필수다. 3회까지 재시도한다.
        """
        self._close_session()
        if why:
            self._log(f"  세션 재생성({why})")
        last = None
        for attempt in range(3):
            try:
                s = requests.Session()
                s.headers.update(HEADERS)
                s.get(LIST_URL, timeout=TIMEOUT)
                self.session = s
                return
            except KeyboardInterrupt:
                raise
            except Exception as e:
                last = e
                self._log(f"  세션 생성 실패({attempt + 1}/3): {e}")
                time.sleep(2 + attempt * 3)
        raise FetchError(f"SEIBro 세션 생성 실패: {last}")

    def _close_session(self):
        try:
            if self.session:
                self.session.close()
        except Exception:
            pass
        self.session = None

    # ------------------------------------------------------------------ 저장·로그
    def _flush(self, buf):
        """버퍼를 DB에 반영하고 비운다. 파싱 결측건도 detail_fetched=True로 마킹."""
        if not buf:
            return
        with transaction.atomic():
            for isin, row in buf:
                HistoricalIssue.objects.filter(isin=isin).update(
                    detail_fetched=True,
                    ki=row.get("ki"),
                    stepdown_barriers=row.get("stepdown_barriers"),
                    eval_dates=row.get("eval_dates"),
                    step_yields=row.get("step_yields"),
                    yield_rate=row.get("yield_rate"),
                    period_months=row.get("period_months"),
                    first_eval_months=row.get("first_eval_months"),
                    parse_error=row.get("parse_error", ""),
                )
        buf.clear()

    def _progress(self, stats, total, started):
        done = stats["done"]
        elapsed = (time.time() - started) / 60
        remain = (elapsed / done) * (total - done) if done else 0
        line = (f"진행 {done}/{total}, 성공 {done - stats['miss']}, 결측 {stats['miss']}, "
                f"통신실패 {stats['fail']}, 경과 {elapsed:.0f}m, 예상잔여 {remain:.0f}m")
        self.stdout.write("  " + line)
        self._log(line)

    def _log(self, msg):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
        except Exception:
            pass
