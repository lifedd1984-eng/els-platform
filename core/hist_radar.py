"""
과거 레이더 재현·성과검증 공용 헬퍼 (simulate_historical / verify_historical).

SEIBro 전수수집분(HistoricalIssue)은 현 서비스(Product)와 표기 체계가 달라
그대로는 시세를 못 붙인다. 이 모듈이 그 간극만 메우고,
백테스트·레이더 판정 로직 자체는 기존 코드(core.backtest / core.models 상수)를 그대로 쓴다.

핵심 3가지
  1) 기초자산 → 티커 해결: SEIBro가 주는 **자산 ISIN**을 1순위로 쓴다.
     (KR7xxxxxx00y → xxxxxx.KS, 주요 지수는 ISIN 직접 매핑)
     이름 표기가 서비스와 완전히 달라("에스케이하이닉스", "TESLA INC")
     market.resolve_ticker만으론 대부분 미해결이기 때문.
  2) PriceStore: 티커별 전 기간 시세를 **딱 한 번** 받아 메모리에 두고
     여러 상품이 공유한다(지수형이 87%라 티커 종류가 적어 효율이 매우 높다).
     auto_adjust=False — 증권사 고시 종가와 맞추기 위함(core.market과 동일 정책).
  3) reproduce_radar(): 현 서비스 _compute_radar_pool과 **같은 순서**로 그룹 내 상대평가를
     재현한다. 단 낙인·막차배리어 컷만은 AdaptiveGate가 준 **시대적응형 컷**으로 갈아끼운다
     (고정 상수는 2026년 시장 기준이라 과거 빈티지에선 통과율이 0~10%로 전멸한다).
     조기상환·손실확률·수익률 상위비율·점수식·등급컷은 서비스 상수 그대로.
     현 서비스 로직(core.models)은 절대 건드리지 않는다 — 이 모듈은 과거 재현 전용.
"""

import time
from bisect import bisect_left
from datetime import date, timedelta

from core import market
from core.models import (
    RADAR_KI_EXCL, RADAR_LAST_MAX, RADAR_EARLY_MIN, RADAR_LOSS_MAX,
    RADAR_SCORE_SHIFT, RADAR_TOP_STRONG, RADAR_TOP_WEAK, RADAR_YIELD_TOP_PCT,
)

# ── 시뮬 불가 사유 코드 (HistoricalIssue.sim_skip) ────────────────────
SKIP_GATE = "게이트미달"      # 값싼 사전 게이트 탈락 → 배지 후보 아님, 시뮬 생략
SKIP_TICKER = "티커미해결"    # 기초자산을 티커로 못 붙임
SKIP_PRICE = "시세부족"       # 시세 없음 / 공통구간 너무 짧음
SKIP_COND = "조건부족"        # 배리어·평가주기 결측 → 애초에 시뮬 불가

# 공통 시세 구간이 이 연수를 밑돌면 백테스트 표본이 못 미더워 스킵
MIN_COMMON_YEARS = 8

# ── 시뮬 사전 게이트 상한 (simulate_historical 전용) ──────────────────
# 배지 컷은 verify_historical이 **시대적응형**(트레일링 퍼센타일)으로 정하므로
# 시뮬 단계에서 2026년 기준 고정 상수(지수형 45/종목형 35)로 자르면 과거 후보를 놓친다.
#
# 근거 — AdaptiveGate로 2010~2026년 전 주차의 컷을 실제로 산출해 본 최대값:
#     지수형  cut_ki 최대 60.33 (2022-07-11 주차) / cut_last 최대 85.0 (2015-07-20 주차)
#     종목형  cut_ki 최대 55.00 (2024-03-18 주차) / cut_last 최대 75.0 (2024-03-18 주차)
#   여기에 여유를 얹은 값이 아래 상한이다(전수 수집이 끝나면 분포 꼬리가 더 벌어질 수 있어
#   지수형도 종목형과 같은 65로 맞췄다 — 상한은 느슨할수록 안전하다).
# → 이 상한을 넘는 상품은 어떤 시대 컷으로도 통과 못 하므로 백테스트를 생략해도 안전하다.
# → 반대로 상한이 빡빡하면 '당시라면 배지였을' 후보가 시뮬 없이 사라진다(=조용한 누락).
#   그래서 verify_historical은 적응형 컷이 이 상한을 넘는 그룹을 세어 경고한다.
# 실측 통과율(2016년 이후 로컬 표본): 낙인 상한 63.6% (옛 고정상수는 8.7%),
#   막차 상한 99.6% — 막차 쪽은 사실상 비구속이며 극단값만 걸러낸다.
PRE_KI_MAX = {"지수형": 65, "종목형": 65}
PRE_LAST_MAX = 90

# ── 시대적응형 컷 파라미터 ────────────────────────────────────────────
GATE_WINDOW_WEEKS = 52    # 트레일링 윈도우 길이(주). 그 주 월요일 '이전' 발행분만.
GATE_MIN_SAMPLE = 100     # 윈도우 표본이 이 미만이면 확장 윈도우 → 그래도 미만이면 컷 산출 불가


# ──────────────────────────────────────────────────────────────
# 1. 기초자산 → 티커
# ──────────────────────────────────────────────────────────────
# 실체는 core.market으로 옮겼다 (2026-08-05) — 운영 기준가 결정도 같은 매핑을 쓰는데,
# 백테스트 전용 모듈에 두면 core.models를 끌어들여 import가 꼬인다.
# 여기서는 기존 호출부(simulate_historical / verify_historical / scripts)가 그대로
# 돌도록 이름만 재수출한다. 매핑을 고칠 때는 core/market.py 한 곳만 고치면 된다.
INDEX_ISIN_TICKER = market.INDEX_ISIN_TICKER
FOREIGN_NAME_TICKER = market.FOREIGN_NAME_TICKER
_TAIL_RE = market._TAIL_RE
_KR7_RE = market._KR7_RE
_norm_name = market._norm_name
resolve_asset_ticker = market.resolve_seibro_ticker


def issue_tickers(issue):
    """상품의 기초자산 티커 목록(중복 제거, 원 순서 유지). 하나라도 실패하면 None."""
    out = []
    assets = issue.assets or []
    if not assets:
        return None
    for a in assets:
        if not isinstance(a, dict):
            return None
        tk = resolve_asset_ticker(a)
        if not tk:
            return None
        if tk not in out:
            out.append(tk)
    return out or None


# ──────────────────────────────────────────────────────────────
# 2. 시세 저장소 (티커별 전 기간 1회 조회 후 메모리 캐시)
# ──────────────────────────────────────────────────────────────
class PriceStore:
    """티커 → 전 기간 종가 Series 캐시.

    · 조회는 티커당 딱 한 번. 실패도 캐시해 재조회 낭비를 막는다.
    · auto_adjust=False (core.market.fetch_price_on과 동일 정책 — 배당 소급조정 종가는
      증권사 고시 기준가와 어긋난다).
    · close_on()은 fetch_price_on과 같은 의미(대상일 이하 마지막 종가, 휴장 보정)를
      네트워크 왕복 없이 준다. 캐시에 없으면 fetch_price_on으로 폴백.
    """

    def __init__(self, start="1996-01-01", throttle=0.4, logger=None):
        self.start = start
        self.throttle = throttle
        self.log = logger or (lambda msg: None)
        self._series = {}   # 요청 티커 → pandas.Series | None
        self.fetched = 0
        self.failed = []

    def get(self, ticker):
        """티커의 전 기간 종가 Series (tz-naive index). 실패 시 None."""
        if ticker in self._series:
            return self._series[ticker]
        s = self._download(ticker)
        if (s is None or len(s) == 0) and ticker.endswith(".KS"):
            # 코스닥 종목은 .KS로 안 잡힌다 → .KQ 재시도
            alt = ticker[:-3] + ".KQ"
            s = self._download(alt)
            if s is not None and len(s):
                self.log(f"  [티커보정] {ticker} → {alt}")
        if s is None or len(s) == 0:
            self._series[ticker] = None
            self.failed.append(ticker)
            return None
        self._series[ticker] = s
        self.fetched += 1
        return s

    def _download(self, ticker):
        import yfinance as yf

        if self.throttle:
            time.sleep(self.throttle)
        backoffs = [2, 5]  # 총 3회 시도
        for attempt in range(3):
            try:
                h = yf.Ticker(ticker).history(start=self.start, auto_adjust=False)
                s = h["Close"].dropna()
                if len(s):
                    s.index = s.index.tz_localize(None)
                    return s
                return None  # 응답은 왔는데 비었다 → 존재하지 않는 티커, 재시도 무의미
            except Exception:
                pass
            if attempt < len(backoffs):
                time.sleep(backoffs[attempt])
        return None

    def close_on(self, ticker, target, max_gap_days=10):
        """target일 종가(휴장이면 직전 거래일). 캐시 미보유 시 yfinance 직접 조회."""
        import pandas as pd

        s = self.get(ticker)
        if s is None:
            return market.fetch_price_on(ticker, target)
        i = s.index.searchsorted(pd.Timestamp(target), side="right") - 1
        if i < 0:
            return None
        d = s.index[i].date()
        if (target - d).days > max_gap_days:
            # 상장폐지·데이터 공백 — 한참 전 종가를 기준가로 쓰면 안 된다
            return None
        return float(s.iloc[i])


def build_price_frame(store, tickers, before):
    """여러 티커의 공통 거래일 종가 DataFrame. before(발행일) **이전** 구간만.

    ⚠ 선견 편향 차단 지점 — 발행일 당일·이후 시세는 통째로 잘라낸다.
    실패/구간부족이면 (None, 사유코드).
    """
    import pandas as pd

    series = {}
    for tk in tickers:
        s = store.get(tk)
        if s is None or len(s) == 0:
            return None, SKIP_PRICE
        s = s[s.index < pd.Timestamp(before)]   # ← 발행일 이전만
        if len(s) == 0:
            return None, SKIP_PRICE
        series[tk] = s

    prices = pd.DataFrame(series).dropna()      # 공통 거래일만
    if len(prices) < 60:
        return None, SKIP_PRICE
    span_days = (prices.index[-1].date() - prices.index[0].date()).days
    if span_days < MIN_COMMON_YEARS * 365:
        return None, SKIP_PRICE
    return prices, ""


# ──────────────────────────────────────────────────────────────
# 3. 유형 분류 · 값싼 사전 게이트
# ──────────────────────────────────────────────────────────────
def asset_type_of(issue):
    """SEIBro 기초자산유형 → 레이더 그룹. '지수'만 지수형, 나머지는 종목형."""
    return "지수형" if (issue.basset_sort or "").strip() == "지수" else "종목형"


def prefilter(issue, asset_type):
    """시뮬 전 값싼 게이트. 통과면 "", 아니면 sim_skip에 넣을 사유코드.

    ⚠ 여기 쓰는 컷은 **느슨한 상한**(PRE_KI_MAX/PRE_LAST_MAX)이다.
    실제 배지 컷은 verify_historical이 그 시점 분포에서 적응형으로 정하는데,
    과거엔 컷이 더 관대해지므로(지수형 ~50, 종목형 ~60) 2026년 고정 상수로 미리 자르면
    당시엔 배지를 받았을 후보를 통째로 놓친다.
    상한을 넘는 상품은 어떤 시대 컷으로도 통과 못 하므로 백테스트를 생략해도 안전하다.
    (배지 없는 대조군은 시뮬값 없이도 실제 결과 판정만으로 검증 가능하다.)
    """
    bars = issue.stepdown_barriers or []
    if not bars or not issue.period_months:
        return SKIP_COND
    if any(not isinstance(b, (int, float)) for b in bars):
        return SKIP_COND   # 배리어 파싱 결손(None 섞임) → 판정식이 성립 안 함
    if issue.ki is None or issue.yield_rate is None:
        return SKIP_GATE
    if issue.ki >= PRE_KI_MAX[asset_type]:
        return SKIP_GATE
    if bars[-1] is None or bars[-1] > PRE_LAST_MAX:
        return SKIP_GATE
    return ""


# ──────────────────────────────────────────────────────────────
# 4. 레이더 재현 (그룹 내 상대평가)
# ──────────────────────────────────────────────────────────────
def week_monday(d: date) -> date:
    """발행일이 속한 주의 월요일 (청약주차 근사)."""
    return d - timedelta(days=d.weekday())


def reproduce_radar(issues, asset_type, cut_ki=None, cut_last=None):
    """(주차, 유형) 그룹의 배지를 재현한다 → {isin: (tier, rank)}.

    현 서비스 core.models._compute_radar_pool과 **같은 순서**로 판정한다.
      게이트 ① 낙인 있음 ② 낙인 < 컷 ③ 1년내 조기상환 >= RADAR_EARLY_MIN
             ④ 손실확률 < RADAR_LOSS_MAX ⑤ 막차 배리어 <= 컷
      ⑥ 통과분 중 수익률 상위 RADAR_YIELD_TOP_PCT
      점수 = 수익률 − 낙인 + RADAR_SCORE_SHIFT, 내림차순 순위 → 등급

    cut_ki/cut_last를 주면 그 값(시대적응형 컷)을 쓰고, 안 주면 서비스 고정 상수를 쓴다.
    ③④는 백테스트 기반 절대 지표라 시대 보정 없이 서비스 상수 그대로다.
    배지 없는 상품도 (tier="", rank=순위 or None)로 반환해 '처리됨'을 남긴다.
    """
    ki_excl = RADAR_KI_EXCL[asset_type] if cut_ki is None else cut_ki
    last_max = RADAR_LAST_MAX[asset_type] if cut_last is None else cut_last

    survivors = []
    for p in issues:
        bars = p.stepdown_barriers or []
        if p.ki is None or p.ki >= ki_excl:
            continue
        if p.sim_early_1y is None or p.sim_loss_prob is None:
            continue                                    # 시뮬 미보유 → 배지 후보 아님
        if p.sim_early_1y < RADAR_EARLY_MIN:
            continue
        if p.sim_loss_prob >= RADAR_LOSS_MAX:
            continue
        if not bars or bars[-1] is None or bars[-1] > last_max:
            continue
        survivors.append(p)

    # ⑥ 수익률 상위 비율 (게이트 통과분 기준) — 서비스와 동일하게 컷값 이상을 남긴다
    if survivors:
        ys = sorted(p.yield_rate or 0 for p in survivors)
        # RADAR_YIELD_TOP_PCT=0.5이면 ys[len//2] — 서비스의 중앙값 컷과 완전히 동일
        cut = ys[min(int(len(ys) * (1 - RADAR_YIELD_TOP_PCT)), len(ys) - 1)]
        survivors = [p for p in survivors if (p.yield_rate or 0) >= cut]

    ranked = sorted(survivors,
                    key=lambda p: (p.yield_rate or 0) - p.ki + RADAR_SCORE_SHIFT,
                    reverse=True)

    out = {}
    for p in issues:
        out[p.isin] = ("", None)
    for i, p in enumerate(ranked):
        if i < RADAR_TOP_STRONG:
            tier = "아주 강한 신호"
        elif i < RADAR_TOP_WEAK:
            tier = "강한 신호"
        else:
            tier = ""
        out[p.isin] = (tier, i + 1)
    return out


# ──────────────────────────────────────────────────────────────
# 5. 시대적응형 게이트 (트레일링 퍼센타일)
# ──────────────────────────────────────────────────────────────
class AdaptiveGate:
    """낙인·막차배리어 컷을 '그 시점의 직전 발행 분포'에서 퍼센타일로 산출한다.

    왜 필요한가
      현 서비스 상수(지수형 낙인<45, 종목형<35)는 2026년 시장에 맞춰 튜닝됐다.
      과거 지수형 ELS는 낙인이 통상 50~65였으므로 같은 절대컷을 들이대면
      2016~2022년 빈티지는 통과율 0~10%로 사실상 전멸해 검증 표본이 안 나온다.
      '그 시절 기준으로 상위 몇 %인가'로 바꾸면 시대별 선별력이 유지된다.

    어떻게
      ① 앵커: 최신 연도(기본 = 데이터 최대 연도) 발행분에서 현 상수의 통과 비율을 잰다.
         P_ki   = 그 해 발행분 중 ki < RADAR_KI_EXCL[유형] 인 비율(%)
         P_last = 그 해 발행분 중 last <= RADAR_LAST_MAX[유형] 인 비율(%)
         → "현 레이더는 그 시장에서 상위 P%를 고른다"는 선별 강도를 숫자로 고정.
      ② 각 (주차, 유형)마다 그 주 월요일 **이전** 52주 발행분에서
         cut_ki = percentile(윈도우 ki, P_ki), cut_last = percentile(윈도우 last, P_last).
      ③ 표본 < GATE_MIN_SAMPLE이면 데이터 시작부터의 확장 윈도우로 폴백,
         그래도 미달이면 컷 산출 불가(그 그룹은 배지 없음, 대조군 판정은 정상 수행).

    ⚠ 선견 편향 차단: 윈도우는 항상 `issue_date < monday` — 같은 주도, 미래도 안 본다.
      (앵커 퍼센타일만은 전 기간을 본다. 이건 '현 서비스의 선별 강도'라는 튜닝 상수를
       읽어오는 것이지 개별 시점의 시장 정보가 아니다 — 상수 자체가 이미 2026년 산물.)
    """

    def __init__(self, rows, anchor_year=None):
        """rows: [(issue_date, asset_type, ki|None, last_barrier|None)] — 전 기간 전량."""
        self.by_type = {}       # 유형 → {"ki": (ords, vals), "last": (ords, vals)}
        self.anchor_year = anchor_year
        self.anchor = {}        # 유형 → {"P_ki": %, "P_last": %, "n_ki":, "n_last":}
        self.no_cut_groups = 0  # 표본 부족으로 컷을 못 낸 그룹 수
        self._cache = {}        # (monday, 유형) → cuts dict | None

        buckets = {}
        years = set()
        for d, at, ki, last in rows:
            if d is None:
                continue
            years.add(d.year)
            b = buckets.setdefault(at, {"ki": [], "last": []})
            if ki is not None:
                b["ki"].append((d.toordinal(), float(ki)))
            if last is not None:
                b["last"].append((d.toordinal(), float(last)))
        if not years:
            return
        if not self.anchor_year:
            self.anchor_year = max(years)

        for at, b in buckets.items():
            store = {}
            for kind in ("ki", "last"):
                pairs = sorted(b[kind])
                store[kind] = ([o for o, _ in pairs], [v for _, v in pairs])
            self.by_type[at] = store
            self.anchor[at] = self._anchor_pcts(at, b)

    def _anchor_pcts(self, asset_type, bucket):
        """앵커 연도 발행분에서 현 상수의 통과 비율(%)을 실측 — 하드코딩 없음."""
        y0 = date(self.anchor_year, 1, 1).toordinal()
        y1 = date(self.anchor_year, 12, 31).toordinal()
        ki_v = [v for o, v in bucket["ki"] if y0 <= o <= y1]
        last_v = [v for o, v in bucket["last"] if y0 <= o <= y1]
        ki_excl = RADAR_KI_EXCL.get(asset_type, RADAR_KI_EXCL["종목형"])
        last_max = RADAR_LAST_MAX.get(asset_type, RADAR_LAST_MAX["종목형"])
        return {
            "P_ki": (100.0 * sum(1 for v in ki_v if v < ki_excl) / len(ki_v)) if ki_v else None,
            "P_last": (100.0 * sum(1 for v in last_v if v <= last_max) / len(last_v))
                      if last_v else None,
            "n_ki": len(ki_v), "n_last": len(last_v),
            "ref_ki": ki_excl, "ref_last": last_max,
        }

    def _window(self, asset_type, kind, monday):
        """[monday-52주, monday) 구간 값들. 부족하면 데이터 시작~monday로 확장.
        반환 (값리스트, 윈도우종류)."""
        store = self.by_type.get(asset_type)
        if not store:
            return [], "없음"
        ords, vals = store[kind]
        hi = bisect_left(ords, monday.toordinal())        # monday 당일·이후는 제외(선견 차단)
        lo = bisect_left(ords, (monday - timedelta(weeks=GATE_WINDOW_WEEKS)).toordinal())
        win = vals[lo:hi]
        if len(win) >= GATE_MIN_SAMPLE:
            return win, "52주"
        win = vals[:hi]                                    # 확장 윈도우(데이터 시작부터)
        if len(win) >= GATE_MIN_SAMPLE:
            return win, "확장"
        return win, "부족"

    def cuts(self, monday, asset_type):
        """(주차, 유형)의 컷. 산출 불가면 None.

        반환: {cut_ki, cut_last, eff_ki, eff_last, n_ki, n_last, window}
          eff_* = **실효 컷** — 윈도우 안에서 실제로 통과하는 최대 낙인/막차 레벨.
          낙인은 5단위 이산값이라 컷을 반올림할 필요가 없고, 대신 사람이 읽을 땐
          "그 시절엔 낙인 50이 컷이었다"가 와닿으므로 실효값을 따로 보여준다.
        """
        key = (monday, asset_type)
        if key in self._cache:
            return self._cache[key]

        import numpy as np

        res = None
        anc = self.anchor.get(asset_type) or {}
        p_ki, p_last = anc.get("P_ki"), anc.get("P_last")
        ki_win, ki_kind = self._window(asset_type, "ki", monday)
        last_win, last_kind = self._window(asset_type, "last", monday)

        if (p_ki is not None and p_last is not None
                and ki_kind != "부족" and last_kind != "부족"):
            cut_ki = float(np.percentile(ki_win, p_ki))
            cut_last = float(np.percentile(last_win, p_last))
            passed_ki = [v for v in ki_win if v < cut_ki]
            passed_last = [v for v in last_win if v <= cut_last]
            res = {
                "cut_ki": round(cut_ki, 2), "cut_last": round(cut_last, 2),
                "eff_ki": max(passed_ki) if passed_ki else None,
                "eff_last": max(passed_last) if passed_last else None,
                "n_ki": len(ki_win), "n_last": len(last_win),
                "window": ki_kind if ki_kind == last_kind else f"{ki_kind}/{last_kind}",
            }
        else:
            self.no_cut_groups += 1

        self._cache[key] = res
        return res
