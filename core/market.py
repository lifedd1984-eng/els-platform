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


def auto_resolve_ticker(name: str):
    """영문 종목명을 Yahoo Finance 검색으로 자동 해결. 실패 시 None.
    한글/비ASCII·인덱스명은 오매핑 위험이 커서 시도하지 않는다(→ 수동 알림 대상).
    반환된 종목명이 질의와 일치하는지 검증해 엉뚱한 매핑을 막는다."""
    import urllib.request
    import urllib.parse
    q = re.sub(r"\sIndex$", "", (name or "").strip(), flags=re.IGNORECASE).strip()
    if not q or not q.isascii() or len(q) < 2:
        return None
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
    """현재가(최근 종가) 조회. 실패 시 None."""
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


def fetch_price_on(ticker: str, target_date):
    """target_date 근처(±7일) 종가 조회 → 발행일 기준가. 실패 시 None."""
    import yfinance as yf
    from datetime import timedelta
    try:
        start = (target_date - timedelta(days=3)).strftime("%Y-%m-%d")
        end = (target_date + timedelta(days=7)).strftime("%Y-%m-%d")
        h = yf.Ticker(ticker).history(start=start, end=end)
        price = h["Close"].dropna()
        if len(price):
            return float(price.iloc[0])  # 발행일 이후 첫 거래일 종가
    except Exception:
        pass
    return None


_history_cache = {}  # (ticker, date.today()) → [(date, close), ...]


def fetch_history(ticker: str, days: int = 365):
    """최근 days일 일별 종가 [(date, close), ...]. 실패 시 []. 하루 단위 캐시."""
    import yfinance as yf
    from datetime import date as _date

    key = (ticker, _date.today())
    if key in _history_cache:
        return _history_cache[key]
    rows = []
    try:
        h = yf.Ticker(ticker).history(period=f"{days}d")
        closes = h["Close"].dropna()
        rows = [(idx.date(), float(v)) for idx, v in closes.items()]
    except Exception:
        pass
    _history_cache[key] = rows
    return rows
