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
    "Euro Stoxx 50": "^STOXX50E", "EuroStoxx50": "^STOXX50E",
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
    "META": "META",
}


def split_assets(assets_raw: str):
    """'KOSPI200 , SK하이닉스' → ['KOSPI200', 'SK하이닉스']."""
    return [a.strip() for a in re.split(r"[,/]+", assets_raw or "") if a.strip()]


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


def resolve_ticker(asset_name: str):
    """기초자산명 → 티커. 매핑 실패 시 None."""
    name = asset_name.strip()
    # 지수형 자산명 뒤의 " Index" 접미사 제거 (예: "KOSPI200 Index" → "KOSPI200")
    name = re.sub(r"\sIndex$", "", name, flags=re.IGNORECASE).strip()
    if name in TICKER_MAP:
        return TICKER_MAP[name]
    # 부분 일치 (대소문자 무시)
    upper = name.upper()
    for k, v in TICKER_MAP.items():
        if k.upper() == upper:
            return v
    return None


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
