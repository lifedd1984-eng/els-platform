"""
기초자산 시세 조회 (yfinance) 및 낙인 거리 계산.

ELS는 워스트오브 구조 → 여러 기초자산 중 가장 많이 하락한 자산 기준으로 낙인 판정.
낙인 판정 기준: 현재가 / 발행일 기준가 × 100 (= 현재 레벨 %) 이 KI 배리어 이하로 떨어지면 낙인.
"""

import re

# 기초자산명(엑셀 표기) → yfinance 티커
# KOSPI200은 지수(^KS200)가 nan을 줘서 KODEX200 ETF로 대체
TICKER_MAP = {
    # ── 지수 ──
    "KOSPI200": "069500.KS", "KOSPI 200": "069500.KS", "코스피200": "069500.KS",
    "KOSDAQ150": "229200.KS", "코스닥150": "229200.KS",
    "S&P500": "^GSPC", "S&P 500": "^GSPC", "SP500": "^GSPC",
    "Nikkei225": "^N225", "Nikkei 225": "^N225", "니케이225": "^N225",
    "Euro Stoxx 50": "^STOXX50E", "EuroStoxx50": "^STOXX50E", "Euro 50": "^STOXX50E",
    "LG이노텍": "011070.KS", "삼성증권": "016360.KS", "삼성전기": "009150.KS",
    "EUROSTOXX50": "^STOXX50E", "유로스탁스50": "^STOXX50E",
    "HSCEI": "^HSCE", "항셍중국기업지수": "^HSCE",
    "HSI": "^HSI", "NASDAQ100": "^NDX", "나스닥": "^NDX",
    # ── 국내 종목 ──
    "삼성전자": "005930.KS",
    "SK하이닉스": "000660.KS",
    "현대차": "005380.KS",
    "NAVER": "035420.KS", "네이버": "035420.KS",
    "기아": "000270.KS",
    "LG에너지솔루션": "373220.KS",
    "포스코홀딩스": "005490.KS",
    "셀트리온": "068270.KS",
    "한화에어로스페이스": "012450.KS",
    "카카오": "035720.KS",
    "현대모비스": "012330.KS",
    "LG전자": "066570.KS",
    "HD현대중공업": "329180.KS",
    "KB금융": "105560.KS",
    "하나금융지주": "086790.KS",
    "두산에너빌리티": "034020.KS",
    "삼성SDI": "006400.KS",
    "한국가스공사": "036460.KS",
    "한국전력": "015760.KS",
    "LG화학": "051910.KS",
    "POSCO홀딩스": "005490.KS",
    # ── 해외 종목 ──
    "Micron": "MU", "마이크론": "MU", "MU": "MU",
    "Applied Materials": "AMAT", "AMAT": "AMAT",
    "Intel": "INTC", "인텔": "INTC", "INTC": "INTC",
    "Palantir": "PLTR", "PALANTIR": "PLTR", "PALANTIR-A": "PLTR", "팔란티어": "PLTR", "PLTR": "PLTR",
    "Tesla": "TSLA", "TESLA": "TSLA", "테슬라": "TSLA", "TSLA": "TSLA",
    "NVIDIA": "NVDA", "엔비디아": "NVDA", "NVDA": "NVDA",
    "Broadcom": "AVGO", "BROADCOM": "AVGO", "브로드컴": "AVGO", "AVGO": "AVGO",
    "AMD": "AMD",
    "Qualcomm": "QCOM", "QUALCOMM": "QCOM", "QCOM": "QCOM",
    "TSMC": "TSM", "티에스엠씨": "TSM", "TSM": "TSM",
    "Alphabet": "GOOGL", "ALPHABET": "GOOGL", "ALPHABET-A": "GOOGL", "GOOGL": "GOOGL",
    "Amazon": "AMZN", "AMZN": "AMZN",
    "Apple": "AAPL", "AAPL": "AAPL",
    "Microsoft": "MSFT", "MSFT": "MSFT",
    "META": "META", "META PLATFORMS": "META", "메타": "META", "메타플랫폼스": "META",
    "Google": "GOOGL", "Oracle": "ORCL", "ORCL": "ORCL",
    "Eli Lilly": "LLY", "일라이릴리": "LLY", "LLY": "LLY",
    "LS ELECTRIC": "010120.KS", "LS일렉트릭": "010120.KS", "엘에스일렉트릭": "010120.KS",
}


# 쉼표 분리 시 회사명 접미사("Advanced Micro Devices, Inc.")를 자산으로 오인하지 않게 재결합
_CORP_SUFFIXES = {"inc", "inc.", "ltd", "ltd.", "co", "co.", "corp", "corp.",
                  "llc", "plc", "n.v.", "s.a.", "class a", "class b"}


def split_assets(assets_raw: str):
    """'KOSPI200 , SK하이닉스' → ['KOSPI200', 'SK하이닉스'].

    'Advanced Micro Devices, Inc.'처럼 회사명 안의 쉼표는 구분자가 아니므로
    접미사 조각은 앞 자산에 다시 붙인다.
    """
    parts = [a.strip() for a in re.split(r"[,/]+", assets_raw or "") if a.strip()]
    merged = []
    for part in parts:
        if merged and part.lower().rstrip(")").split("(")[0].strip() in _CORP_SUFFIXES:
            merged[-1] = f"{merged[-1]}, {part}"
        else:
            merged.append(part)
    return merged


# 표시 전용 축약 매핑 (화면에만 사용 — 원본 assets_raw 저장값은 절대 변경 금지).
# 키는 소문자로 저장하고 대소문자 무시로 매칭한다.
_DISPLAY_SHORTEN_MAP = {
    # ── 지수 ──
    "kospi200 index": "KOSPI200",
    "s&p500 index": "S&P500",
    "euro stoxx 50 index": "Euro 50",
    "eurostoxx50": "Euro 50",
    "euro stoxx 50": "Euro 50",
    "nikkei225 index": "Nikkei225",
    "hscei index": "HSCEI",
    "kosdaq150 index": "KOSDAQ150",
    # ── 해외 종목 (실데이터 등장 이름 전수 반영) ──
    "micron technology": "Micron",
    "tesla inc.(uw)": "Tesla",
    "tesla inc.(us)": "Tesla",
    "tesla inc.": "Tesla",
    "advanced micro devices, inc.": "AMD",
    "advanced micro devices": "AMD",
    "broadcom inc.": "Broadcom",
    "broadcom limited": "Broadcom",
    "nvidia corporation": "NVIDIA",
    "nvidia corporation(nasdaq)": "NVIDIA",
    "alphabet inc.": "Alphabet",
    "alphabet inc(nasdaq)": "Alphabet",
    "amazone inc": "Amazon",
    "eli lilly and company": "Eli Lilly",
    "intel corporation": "Intel",
    "palantir technologies inc. class a": "Palantir",
    "tsmc adr": "TSMC",
    "taiwan semiconductor manufacturing": "TSMC",
}


def shorten_asset_display(assets_raw: str) -> str:
    """기초자산 원본 문자열을 화면 표시용으로만 축약한다 (저장값 변경 금지).

    - split_assets() 로 분리 (구분자 [,/]+)
    - 각 자산명: 명시 매핑(대소문자 무시) 우선, 없으면 끝의 " Index" 접미사만 제거,
      그 외(한글 종목명 등)는 원본 유지
    - "/" 로 조인
    """
    out = []
    for name in split_assets(assets_raw):
        key = name.lower()
        if key in _DISPLAY_SHORTEN_MAP:
            out.append(_DISPLAY_SHORTEN_MAP[key])
        elif re.search(r"\sIndex$", name, re.IGNORECASE):
            out.append(re.sub(r"\sIndex$", "", name, flags=re.IGNORECASE).strip())
        else:
            out.append(name)
    return "/".join(out)


# ── 자동 학습 티커맵 (Yahoo 검색으로 해결한 신규 자산을 영구 저장) ──
import json as _json
import os as _os

_LEARNED_PATH = _os.path.abspath(
    _os.path.join(_os.path.dirname(__file__), "..", "data", "ticker_learned.json"))
_learned_cache = None


def _load_learned():
    global _learned_cache
    if _learned_cache is None:
        try:
            with open(_LEARNED_PATH, encoding="utf-8") as f:
                _learned_cache = _json.load(f)
        except Exception:
            _learned_cache = {}
    return _learned_cache


def learn_ticker(name, ticker):
    """이름→티커 매핑을 학습 저장소에 추가(영구)."""
    m = dict(_load_learned())
    m[name.strip()] = ticker
    _os.makedirs(_os.path.dirname(_LEARNED_PATH), exist_ok=True)
    with open(_LEARNED_PATH, "w", encoding="utf-8") as f:
        _json.dump(m, f, ensure_ascii=False, indent=0, sort_keys=True)
    global _learned_cache
    _learned_cache = m


_KRX_PATH = _os.path.abspath(
    _os.path.join(_os.path.dirname(__file__), "..", "data", "krx_master.json"))
_krx_cache = None


def _krx_norm(s):
    return re.sub(r"[^0-9A-Z가-힣]", "", (s or "").upper())


def _krx_master():
    """KRX 상장 종목마스터 {정규화이름: 티커}. 7일 캐시, 실패 시 있으면 스테일 사용."""
    global _krx_cache
    if _krx_cache is not None:
        return _krx_cache
    from datetime import date
    # 캐시 로드
    cached = None
    try:
        with open(_KRX_PATH, encoding="utf-8") as f:
            cached = _json.load(f)
    except Exception:
        cached = None
    fresh = cached and cached.get("date") == date.today().isoformat()
    if fresh:
        _krx_cache = cached["map"]
        return _krx_cache
    # 갱신 시도 (FinanceDataReader)
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
        m = {}
        for _, r in df.iterrows():
            code, name, mkt = str(r["Code"]), str(r["Name"]), str(r.get("Market") or "")
            if not code or not name or name == "nan":
                continue
            suffix = ".KQ" if "KOSDAQ" in mkt.upper() else (
                ".KN" if "KONEX" in mkt.upper() else ".KS")
            m[_krx_norm(name)] = code.zfill(6) + suffix
        _os.makedirs(_os.path.dirname(_KRX_PATH), exist_ok=True)
        with open(_KRX_PATH, "w", encoding="utf-8") as f:
            _json.dump({"date": date.today().isoformat(), "map": m}, f, ensure_ascii=False)
        _krx_cache = m
    except Exception:
        _krx_cache = cached["map"] if cached else {}   # 스테일 폴백
    return _krx_cache


def auto_resolve_ticker(name: str):
    """신규 종목명을 자동 해결. 실패 시 None.
    ① KRX 종목마스터(한글·영문 국내주 정확) → ② 영문명은 Yahoo 검색(해외주).
    Yahoo는 반환 종목명 일치를 검증해 오매핑을 막고, 한글은 KRX에서만 찾는다."""
    q = re.sub(r"\sIndex$", "", (name or "").strip(), flags=re.IGNORECASE).strip()
    # 국가 접미사 제거 — "NVIDIA Corporation(US)" 형태 (하나증권 표기)
    q = re.sub(r"\(\s*(US|USA|JP|HK|KR|미국|일본|홍콩)\s*\)$", "", q, flags=re.IGNORECASE).strip()
    if not q or len(q) < 2:
        return None

    # ① KRX 국내 상장 종목 (한글/영문 모두 정확)
    krx = _krx_master()
    tk = krx.get(_krx_norm(q))
    if tk:
        return tk

    # ② 해외주: 영문명만 Yahoo 검색 (한글은 오매핑 위험 → 포기)
    if not q.isascii():
        return None
    import urllib.request
    import urllib.parse
    q_norm = re.sub(r"[^A-Z0-9 ]", "", q.upper()).strip()
    if not q_norm:
        return None
    url = "https://query1.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode(
        {"q": q, "quotesCount": 8, "newsCount": 0, "lang": "en-US", "region": "US"})
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        data = _json.load(urllib.request.urlopen(req, timeout=10))
    except Exception:
        return None
    for x in data.get("quotes", []):
        if x.get("quoteType") != "EQUITY":
            continue
        sym = (x.get("symbol") or "").strip()
        sn = re.sub(r"[^A-Z0-9 ]", "", (x.get("shortname") or x.get("longname") or "").upper())
        # 질의 전체가 종목명에 포함되거나, 심볼과 일치해야 채택 (오매핑 방지)
        if sym and (q_norm in sn or q_norm.replace(" ", "") == sym.upper().replace(".", "")):
            return sym
    return None


def resolve_ticker(asset_name: str):
    """기초자산명 → 티커. 매핑 실패 시 None."""
    name = asset_name.strip()
    # 지수형 자산명 뒤의 " Index" 접미사 제거 (예: "KOSPI200 Index" → "KOSPI200")
    name = re.sub(r"\sIndex$", "", name, flags=re.IGNORECASE).strip()
    # 정식 회사명 → 축약명 정규화 (예: "Micron Technology" → "Micron",
    # "Palantir Technologies Inc. Class A" → "Palantir") — 표시용 맵 재사용
    name = _DISPLAY_SHORTEN_MAP.get(name.lower(), name)
    if name in TICKER_MAP:
        return TICKER_MAP[name]
    # 부분 일치 (대소문자 무시)
    upper = name.upper()
    for k, v in TICKER_MAP.items():
        if k.upper() == upper:
            return v
    # 자동 학습 저장소 (Yahoo 검색으로 확정된 신규 자산)
    learned = _load_learned()
    return learned.get(name) or learned.get(asset_name.strip())


def fetch_current_price(ticker: str):
    """현재가(최근 종가) 조회. 실패 시 None.

    ※ 급등락이 커도 걸러내지 않는다 — 2026-07-31 국내 자산 +24~30% 동시 급등은
      실제 대폭등장이었다(태훈님 확인). 이상치 필터를 넣었다가 진짜 거래일을
      버리는 사고가 나서 제거했다. 시세는 있는 그대로 쓴다.
    """
    import yfinance as yf
    try:
        h = yf.Ticker(ticker).history(period="5d")
        if len(h):
            price = h["Close"].dropna()
            if len(price):
                return float(price.iloc[-1])
    except Exception:
        pass
    return None


def fetch_price_on(ticker: str, target_date, back: int = 0):
    """target_date 당일 종가 (휴장이면 직전 거래일 종가). 실패 시 None.

    back=N 이면 그보다 N 거래일 더 거슬러 올라간 종가를 준다
    (해외 자산 시차 보정용 — 증권사는 현지 거래일 종가를 기준가로 쓴다).

    auto_adjust=False 필수: 기본값은 배당·분할을 소급 반영한 '조정 종가'라
    증권사가 고시하는 실제 종가와 어긋난다. 배당주는 시간이 지날수록
    과거 가격이 계속 낮아져 오차가 누적된다.
    (실측: 브로드컴 2026-04-23 조정 419.28 vs 실제 419.94 = 증권사 고시값)

    주의: 예전엔 (target−3일) 창의 '첫' 거래일 종가를 반환해 발행일보다
    며칠 前 종가를 기준가로 잡는 버그가 있었음 (급등락 구간에서 낙인
    레벨·상환판정·신호검증이 모두 어긋남). target 이하 '마지막' 종가가 정답."""
    import yfinance as yf
    from datetime import timedelta
    try:
        start = (target_date - timedelta(days=10 + back * 5)).strftime("%Y-%m-%d")
        end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
        h = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        price = h["Close"].dropna()
        # end가 배타적이라 창 안은 전부 target 이하 → 마지막 = 당일(또는 직전 거래일)
        if len(price) > back:
            return float(price.iloc[-1 - back])
    except Exception:
        pass
    return None


# ── 최초기준가격 산정일 ──────────────────────────────────────────────
# 간이투자설명서 전수조사(18개사 72건) 결과: 최초기준가격평가일은 발행사별로 일관되며
# 대부분 '발행일 당일'이고 일부만 '발행일 −1영업일'(납입·배정일 종가)이다.
# ⚠ 72건 표본으로는 삼성증권·키움증권 둘만 보였지만, 나중에 설명서 확정값 757건을
#   전수로 다시 도출했더니 대신증권도 −1영업일이었다. 명단은 아래
#   BASE_EVAL_BACK1_ISSUERS 한 곳만 보고 판단할 것. (2026-08-04 주석 정정)
# Product.base_eval_date(설명서에서 파싱)가 있으면 그 날짜가 정답이고,
# 없을 때만 issue_date + 발행사 규칙으로 근사한다.
#
# ⚠ issue_date는 이름과 달리 **청약종료일**이다(kofia_scraper가 같은 필드를 양쪽에 넣음).
#   아래 일수는 18개사 76건의 간이투자설명서를 실제 파싱해 측정한 값이며,
#   "발행일 기준" 관행(BASE_EVAL_BACK1_ISSUERS = 발행일 −1영업일)과는 기준점이 달라
#   결과가 다르다. 이 발행사들은 발행일이 청약종료일의 익영업일이라,
#   둘을 합치면 평가일 = 청약종료일(+0)이 된다.
#   → 파생 추론 말고 이 실측표를 쓸 것.
BASE_EVAL_OFFSET_DAYS = {          # issue_date(청약종료일) + N일 = 최초기준가격평가일
    "NH투자증권": 1,
    "미래에셋증권": 1,
    "한화투자증권": 1,
    "현대차증권": 1,
}                                   # 그 외 12개사는 +0일
# 상품마다 값이 달라 규칙화 불가 — 반드시 설명서를 파싱해야 하는 발행사
BASE_EVAL_UNSTABLE_ISSUERS = {"유안타증권", "유진투자증권"}

# issue_date가 '실제 발행일'인 행(엑셀 수입분)에 쓰는 규칙 —
# 설명서 확정값 757건 전수에서 도출(예외 없음): 이 세 곳만 발행일 −1거래일
BASE_EVAL_BACK1_ISSUERS = {"키움증권", "삼성증권", "대신증권"}


def base_price_date(product):
    """상품의 최초기준가격 산정에 쓸 (기준일, 거래일 오프셋) 반환.

    반환값을 그대로 fetch_price_on(ticker, 기준일, back=오프셋)에 넘기면 된다.
    기준일이 없으면 (None, 0).

    ⚠ 오프셋은 '기준가'에만 적용한다. 조기상환 평가일 시세 조회에는 절대 쓰지 말 것
      (평가일 종가는 지금도 증권사 고시값과 일치한다).
    """
    from datetime import timedelta

    base = getattr(product, "base_eval_date", None)
    if base:
        return base, 0
    base = getattr(product, "issue_date", None)
    if not base:
        return None, 0
    issuer = getattr(product, "issuer", "")
    # issue_date의 의미가 두 가지다:
    #   ① KOFIA 수집분 — issue_date == sub_end (청약종료일). 오프셋 표가 이 기준이다.
    #   ② 엑셀 수입분  — issue_date가 실제 발행일 (1,538건). 여기에 ①용 오프셋을
    #      그대로 더하면 하루가 이중 가산돼 기준가가 1거래일 늦은 종가가 된다.
    # 실제 발행일 기준 규칙은 설명서 확정값 757건 전수로 도출했다(예외 없음):
    #   키움·삼성·대신 = 발행일 −1거래일 / 나머지 = 발행일 당일
    # sub_end가 비어 있어도 ②다. 엑셀 수입분 369건이 여기 해당하고(전건이
    # product_code 없음 = KOFIA 미수집분), 예전 조건 `sub_end and base != sub_end`는
    # None에서 단락돼 ①의 오프셋 표로 떨어졌다. 138건이 하루씩 어긋났고 보유
    # 상품에서 최대 4.9%p까지 레벨이 틀렸다 (2026-08-03 검수).
    sub_end = getattr(product, "sub_end", None)
    if sub_end != base:
        back = 1 if issuer in BASE_EVAL_BACK1_ISSUERS else 0
        return base, back
    days = BASE_EVAL_OFFSET_DAYS.get(issuer, 0)
    # +N일 뒤가 휴장이면 fetch_price_on이 직전 거래일로 자동 보정한다
    return base + timedelta(days=days), 0


# 기준가로 쓸 수 없는 정규화 공시값 — SEIBro가 일부 상품의 최초기준가격을
# 실제 가격이 아니라 지수화 기준점(1·100·1000·10000 등)으로 공시한다.
# 그대로 쓰면 낙인·배리어 계산이 통째로 어긋나므로 걸러야 한다.
# (실측: 포스코홀딩스·기아 = 1.00, NIKKEI = 100 또는 1000, 네이버 = 10000)
def is_normalized_std_price(std_price, market_price, tol: float = 0.2):
    """공시 기준가가 실제 시세와 동떨어진 정규화 값인지 판정."""
    try:
        sp, mp = float(std_price), float(market_price)
    except (TypeError, ValueError):
        return True
    if sp <= 0 or mp <= 0:
        return True
    ratio = sp / mp
    return not (1 - tol <= ratio <= 1 + tol)


_history_cache = {}  # (ticker, date.today()) → [(date, close), ...]


def fetch_history(ticker: str, days: int = 365):
    """최근 days일 일별 종가 [(date, close), ...]. 실패 시 []. 하루 단위 캐시."""
    import yfinance as yf
    from datetime import date as _date

    # days를 키에 넣지 않으면 상품상세(365d)와 주간 고점대비(370d)가 서로의
    # 캐시를 덮어써 워커별 최초 호출이 그날의 데이터 창을 결정한다 (2026-08-03)
    today = _date.today()
    key = (ticker, days, today)
    if key in _history_cache:
        return _history_cache[key]
    rows = []
    try:
        h = yf.Ticker(ticker).history(period=f"{days}d")
        closes = h["Close"].dropna()
        rows = [(idx.date(), float(v)) for idx, v in closes.items()]
    except Exception:
        pass
    # 날짜가 키에 있을 뿐 만료가 없어, 지난 날짜 항목이 워커 재시작 때까지 계속
    # 쌓였다. days를 키에 넣으면서 항목 수가 2배가 됐고, 고유 티커 42개 × 창 2종
    # × 370행이면 워커당 하루 약 2.5MB, 30일이면 75MB다. 새로 넣을 때 지난 날짜를
    # 버린다 — 오늘 것만 남기면 되므로 LRU 같은 장치가 필요 없다. (2026-08-04)
    for stale in [k for k in _history_cache if k[2] != today]:
        del _history_cache[stale]
    _history_cache[key] = rows
    return rows
