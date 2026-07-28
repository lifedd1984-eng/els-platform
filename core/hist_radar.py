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
  3) reproduce_radar(): 현 서비스 _compute_radar_pool과 **같은 순서·같은 상수**로
     그룹 내 상대평가를 재현한다. 상수를 바꾸면 재현 결과도 따라간다.
"""

import re
import time
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


# ──────────────────────────────────────────────────────────────
# 1. 기초자산 → 티커
# ──────────────────────────────────────────────────────────────
# 주요 지수 (SEIBro 자산 ISIN 기준 — 이름 표기 흔들림에 영향받지 않는다).
# ⚠ 레버리지·Quanto·KRW Hedged·Decrement·증권사 자체지수는 **일부러 넣지 않았다**.
#   기초지수와 레벨 궤적이 달라(FX·배당조정) 기준가 대비 비율이 어긋난다.
#   → 티커미해결로 빠져 표본에서 제외되는 편이 오판보다 낫다.
INDEX_ISIN_TICKER = {
    "KSD101000028": "069500.KS",   # 코스피 200지수 (지수 ^KS200은 nan → KODEX200 ETF, market.py와 동일)
    "KSD102000045": "229200.KS",   # 코스닥 150지수
    "KSD310000145": "^STOXX50E",   # DOW JONES EURO STOXX 50
    "KSD310000568": "^GSPC",       # S&P 500
    "KSD310000679": "^GSPC",       # SPX (같은 지수 다른 표기)
    "KSD310000306": "^HSCE",       # HSCEI
    "KSD310000319": "^HSI",        # 항셍
    "KSD310000499": "^N225",       # NIKKEI 225
    "KSD310000103": "000300.SS",   # CSI 300
    "KSD310000110": "^GDAXI",      # DAX
    "KSD310000076": "^FCHI",       # CAC 40
    "KSD310000228": "^FTSE",       # FTSE 100
    "KSD310000471": "^NDX",        # NASDAQ 100
    "KSD310000622": "^AXJO",       # S&P/ASX 200
    "KSD310000025": "^AXJO",       # ASX 200
    "KSD310000867": "^TWII",       # TWSE
    "KSD310000204": "^SX7E",       # EURO STOXX BANKS
}

# 해외 개별종목 — SEIBro 표기(대문자·법인격 포함)가 서비스 표기와 달라 별도 매핑.
# 키는 소문자·공백정규화된 이름.
FOREIGN_NAME_TICKER = {
    "tesla inc": "TSLA", "tesla motors inc.": "TSLA", "tesla inc.": "TSLA",
    "nvidia corp": "NVDA", "nvidia corporation": "NVDA",
    "advanced micro devices inc": "AMD",
    "palantir technologies inc cl a": "PLTR",
    "micron technology inc": "MU",
    "apple inc": "AAPL",
    "amazon.com inc": "AMZN", "amazon com inc": "AMZN",
    "netflix inc": "NFLX",
    "meta platforms inc cl a": "META", "facebook inc cl a": "META",
    "intel corp": "INTC",
    "alphabet inc cl a": "GOOGL", "alphabet inc cl c": "GOOG",
    "google inc cl a": "GOOGL", "google inc cl c": "GOOG",
    "broadcom inc": "AVGO", "broadcom corp": "AVGO", "broadcom limited": "AVGO",
    "microsoft corp": "MSFT",
    "boeing co": "BA",
    "starbucks corp": "SBUX",
    "qualcomm inc": "QCOM",
    "oracle corp": "ORCL",
    "general motors co": "GM",
    "bank of america corp": "BAC",
    "gilead sciences inc": "GILD",
    "walt disney co": "DIS",
    "nike inc cl b": "NKE",
    "electronic arts inc": "EA",
    "eli lilly & co": "LLY", "eli lilly and company": "LLY",
    "activision blizzard inc": "ATVI",
    "arm holdings plc sponsored adr": "ARM",
    "tencent holdings ltd adr": "TCEHY",
    "alibaba group holding ltd adr": "BABA",
    "weibo corp adr": "WB",
    "jd.com inc adr": "JD",
    "baidu inc adr": "BIDU",
    "visa inc cl a": "V",
    "johnson & johnson": "JNJ",
    "pfizer inc": "PFE",
    "salesforce inc": "CRM", "salesforce.com inc": "CRM",
    "adobe inc": "ADBE",
    "advanced micro devices, inc.": "AMD",
    "coupang inc cl a": "CPNG",
}

# SEIBro 이름 꼬리표(거래소 코드·ISIN이 붙어 오는 경우) 제거용
_TAIL_RE = re.compile(r"\s+(?:EXOF|CHAN)\s+\S+.*$", re.IGNORECASE)
_KR7_RE = re.compile(r"^KR7(\d{6})\d{3}$")


def _norm_name(name: str) -> str:
    """이름 정규화 — 꼬리표 제거 + 공백 축약 + 소문자."""
    n = _TAIL_RE.sub("", (name or "").strip())
    return re.sub(r"\s+", " ", n).strip().lower()


def resolve_asset_ticker(asset: dict):
    """HistoricalIssue.assets의 한 원소({name, isin, std_price}) → 티커. 실패 시 None.

    ISIN(기계값) → 이름 매핑 → market.resolve_ticker(서비스 티커맵) 순으로 시도한다.
    """
    isin = (asset.get("isin") or "").strip().upper()
    if isin in INDEX_ISIN_TICKER:
        return INDEX_ISIN_TICKER[isin]
    m = _KR7_RE.match(isin)
    if m:
        # 국내 상장주 — 표준코드에서 종목코드 6자리를 뽑아낸다(우선주 포함).
        # 코스닥 종목은 .KS가 비어 나오므로 PriceStore가 .KQ로 자동 재시도한다.
        return f"{m.group(1)}.KS"

    name = asset.get("name") or ""
    key = _norm_name(name)
    if key in FOREIGN_NAME_TICKER:
        return FOREIGN_NAME_TICKER[key]
    # 마지막으로 서비스 티커맵(부분일치·학습티커 포함)에 맡긴다
    return market.resolve_ticker(_TAIL_RE.sub("", name).strip()) or None


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

    레이더 배지는 ① 낙인 < 임계 ② 막차 배리어 <= 임계 를 **반드시** 통과해야 하므로,
    여기서 걸리는 상품은 시뮬을 돌려도 배지를 못 받는다 → 백테스트를 아예 생략한다.
    (배지 없는 대조군은 시뮬값 없이도 실제 결과 판정만으로 검증 가능하다.)
    """
    bars = issue.stepdown_barriers or []
    if not bars or not issue.period_months:
        return SKIP_COND
    if any(not isinstance(b, (int, float)) for b in bars):
        return SKIP_COND   # 배리어 파싱 결손(None 섞임) → 판정식이 성립 안 함
    if issue.ki is None or issue.yield_rate is None:
        return SKIP_GATE
    if issue.ki >= RADAR_KI_EXCL[asset_type]:
        return SKIP_GATE
    if bars[-1] is None or bars[-1] > RADAR_LAST_MAX[asset_type]:
        return SKIP_GATE
    return ""


# ──────────────────────────────────────────────────────────────
# 4. 레이더 재현 (그룹 내 상대평가)
# ──────────────────────────────────────────────────────────────
def week_monday(d: date) -> date:
    """발행일이 속한 주의 월요일 (청약주차 근사)."""
    return d - timedelta(days=d.weekday())


def reproduce_radar(issues, asset_type):
    """(주차, 유형) 그룹의 배지를 재현한다 → {isin: (tier, rank)}.

    현 서비스 core.models._compute_radar_pool과 **같은 순서·같은 상수**를 쓴다.
      게이트 ① 낙인 있음 ② 낙인 < 임계 ③ 1년내 조기상환 >= 임계
             ④ 손실확률 < 임계 ⑤ 막차 배리어 <= 임계
      ⑥ 통과분 중 수익률 상위 RADAR_YIELD_TOP_PCT
      점수 = 수익률 − 낙인 + RADAR_SCORE_SHIFT, 내림차순 순위 → 등급
    배지 없는 상품도 (tier="", rank=순위 or None)로 반환해 '처리됨'을 남긴다.
    """
    ki_excl = RADAR_KI_EXCL[asset_type]
    last_max = RADAR_LAST_MAX[asset_type]

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
