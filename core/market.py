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
# 설명서 확정값(base_eval_date) 798건 전수 + SEIBro 공시 기준가 역매칭 1,156쌍
# 가격대조로 확정한 규칙이다. (2026-08-05 전수검증)
#
# ⚠ 예전엔 '기준일 + back(거래일 오프셋)'으로 하루를 되짚었는데 **그게 버그의 근원**이었다.
#   back=1은 자산마다 제 거래소 달력을 타기 때문에, 한국은 휴장인데 해외는 개장한 날
#   (5/1 근로자의날, 7/17 제헌절 등)이 사이에 끼면 엉뚱한 날 종가를 집었다.
#   발행일 자체가 해외 휴장일(성금요일 등)이면 하루 더 밀렸다.
#   실측 정확도 — 국내자산은 어떤 gap에서도 100%였지만 해외자산은
#   gap 2일에서 10%, gap 4일에서 38%까지 무너졌다.
#   → back을 없애고 **기준일을 날짜 하나로 확정**한 뒤 자산마다 '그 날짜 이하의
#     마지막 종가'를 쓰면 전 구간 100%가 된다.
#     재검증: 확정값 798/798(100%), 가격대조 90.5%(현행 78.2%), 개선 142쌍·악화 0쌍.
BASE_EVAL_OFFSET_DAYS = {          # issue_date(청약종료일) + N일 = 최초기준가격평가일
    "NH투자증권": 1,
    "미래에셋증권": 1,
    "한화투자증권": 1,
    "현대차증권": 1,
}                                   # 그 외 12개사는 +0일
# 상품마다 값이 달라 규칙화 불가 — 반드시 설명서를 파싱해야 하는 발행사.
# 전수검증에서 어느 규칙에도 안 맞았고 추론 정확도가 사실상 0%다.
# ⚠ 그렇다고 기준가를 None으로 두면 화면에서 레벨이 사라진다 —
#   사용자에게 보이는 변화라 조 팀장 판단 사항이다. 지금은 옛 로직 그대로 둔다.
BASE_EVAL_UNSTABLE_ISSUERS = {"유안타증권", "유진투자증권"}

# 기준일 = 청약마감일(=배정일). 설명서 확정값 174건 전수에서 예외 없이 일치했고
# 가격대조 일치율도 100%다. (옛 이름 BASE_EVAL_BACK1_ISSUERS — '발행일 −1거래일'로
# 근사하던 시절의 이름이라, 규칙이 날짜 확정으로 바뀌면서 이름도 바꿨다.)
BASE_EVAL_SUBEND_ISSUERS = {"키움증권", "삼성증권", "대신증권"}


def prev_business_day(d):
    """직전 영업일 (주말만 건너뛴다).

    공휴일까지 볼 필요는 없다 — fetch_price_on이 '그 날짜 이하 마지막 종가'를 주므로
    휴장일을 집어도 자동으로 직전 거래일 종가로 내려간다.
    """
    from datetime import timedelta
    d = d - timedelta(days=1)
    while d.weekday() >= 5:        # 5=토, 6=일
        d -= timedelta(days=1)
    return d


def base_price_date(product):
    """상품의 최초기준가격 산정일 → (기준일, 0). 기준일이 없으면 (None, 0).

    반환값을 그대로 fetch_price_on(ticker, 기준일, back=오프셋)에 넘기면 된다.

    ⚠ 두 번째 값(거래일 오프셋)은 **항상 0**이다. 위 주석대로 오프셋 자체가 버그의
      근원이라 폐지했다. 호출부(peak_as_of·views 차트·check_redemptions·verify_radar)가
      아직 2튜플을 풀어 쓰고 있어 형태만 남겨 뒀다 — back=0은 '그 날짜 이하 마지막 종가'
      라는 새 규칙과 정확히 같은 의미다. 호출부 정리는 별도 작업.
    """
    from datetime import timedelta

    # ① 설명서에서 파싱한 확정값이 언제나 정답 (798건 전수 100%)
    base = getattr(product, "base_eval_date", None)
    if base:
        return base, 0

    issue = getattr(product, "issue_date", None)
    if not issue:
        return None, 0
    issuer = getattr(product, "issuer", "")
    sub_end = getattr(product, "sub_end", None)

    # ② 규칙화 불가 발행사 — 옛 로직 그대로 (둘 다 back=0·issue_date였다)
    if issuer in BASE_EVAL_UNSTABLE_ISSUERS:
        return issue, 0

    # ③ 기준일 = 청약마감일(배정일) 발행사
    if issuer in BASE_EVAL_SUBEND_ISSUERS:
        # sub_end가 비어 있으면 issue_date는 '실제 발행일'(엑셀 수입분)이므로
        # 그 직전 영업일이 배정일이다.
        return (sub_end or prev_business_day(issue)), 0

    # ④ 그 외 — 기준일 = 실제 발행일
    real = getattr(product, "real_issue_date", None)
    if real:
        return real, 0
    # issue_date의 의미가 두 가지다:
    #   · 엑셀 수입분 — issue_date가 곧 실제 발행일 (sub_end와 다르거나 sub_end가 없다)
    #   · KOFIA 수집분 — issue_date == sub_end (청약종료일). 이때만 오프셋 표를 쓴다.
    # KOFIA분 오프셋 표는 이번 검증에서 교차확인이 안 됐고 확정값 기준 현행 정확도가
    # 95%라 그대로 둔다 (2026-08-05).
    if issue != sub_end:
        return issue, 0
    return issue + timedelta(days=BASE_EVAL_OFFSET_DAYS.get(issuer, 0)), 0


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


# ── SEIBro 기초자산 → 티커 ────────────────────────────────────────────
# SEIBro(HistoricalIssue)는 서비스와 표기 체계가 완전히 다르다
# ("에스케이하이닉스", "TESLA INC", "코스피 200지수"). 그래서 이름 대신
# **자산 ISIN**을 1순위로 쓴다 (KR7xxxxxx00y → xxxxxx.KS, 지수는 직접 매핑).
# ※ 원래 core.hist_radar에 있던 것을 옮겼다 — 기준가 결정(아래)이 백테스트 전용
#   모듈에 의존하면 안 되기 때문. hist_radar는 이 이름들을 그대로 재수출한다.
#
# ⚠ 레버리지·Quanto·KRW Hedged·Decrement·증권사 자체지수는 **일부러 넣지 않았다**.
#   기초지수와 레벨 궤적이 달라(FX·배당조정) 기준가 대비 비율이 어긋난다.
#   → 티커미해결로 빠져 폴백되는 편이 오판보다 낫다.
INDEX_ISIN_TICKER = {
    "KSD101000028": "069500.KS",   # 코스피 200지수 (지수 ^KS200은 nan → KODEX200 ETF)
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
    "applied materials inc": "AMAT",
}

# SEIBro 이름 꼬리표(거래소 코드·ISIN이 붙어 오는 경우) 제거용
_TAIL_RE = re.compile(r"\s+(?:EXOF|CHAN)\s+\S+.*$", re.IGNORECASE)
_KR7_RE = re.compile(r"^KR7(\d{6})\d{3}$")


def _norm_name(name: str) -> str:
    """SEIBro 자산명 정규화 — 꼬리표 제거 + 공백 축약 + 소문자."""
    n = _TAIL_RE.sub("", (name or "").strip())
    return re.sub(r"\s+", " ", n).strip().lower()


def resolve_seibro_ticker(asset: dict):
    """SEIBro assets의 한 원소({name, isin, std_price}) → 티커. 실패 시 None.

    ISIN(기계값) → 이름 매핑 → resolve_ticker(서비스 티커맵) 순으로 시도한다.
    """
    if not isinstance(asset, dict):
        return None
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
    return resolve_ticker(_TAIL_RE.sub("", name).strip()) or None


# ── 최초기준가격 결정 순서 ────────────────────────────────────────────
# ① SEIBro 공시 최초기준가격(std_price) — 발행사가 신고한 **공식값**. 있으면 무조건 이것.
# ② 없거나 못 믿을 값이면 → 폴백 = base_price_date()가 정한 기준일의 종가.
#
# ⚠ ②를 갈아끼울 지점은 fallback_ref_basis() **한 곳뿐**이다.
#   update_prices / refresh_ref_price 모두 이 함수만 부르므로 여기만 고치면 전부 따라온다.
#
# 배경(2026-08-04~05 실측): 추론이 키움증권 물량에서 하루씩 어긋나 보유 자산의
#   기준가가 ±1~8% 틀어졌다. 방향이 양쪽이라 어떤 건은 레벨이 실제보다
#   **높게(안전하게)** 표시됐다 — 공시값을 최우선으로 두는 이유다.
#   ②의 날짜 규칙 자체도 전수검증으로 바로잡았다(base_price_date 주석 참고).

# 서비스가 지수 대신 ETF를 프록시로 쓰는 티커.
# 공시값은 지수 포인트(예: 코스피200 1126.33)인데 서비스 현재가는 ETF 가격이라
# 스케일이 ~100배 어긋난다. 레벨은 '현재가/기준가' 비율이라 **폴백(같은 ETF 종가)**
# 으로 계산하면 정확하다 — 그래서 여기에만 공시값을 넣지 않는다.
PROXY_INDEX_TICKERS = {"069500", "229200"}

# 시세 비교가 불가능할 때 기각할 정규화 상수 — SEIBro가 일부 상품의 최초기준가격을
# 실제 가격이 아니라 지수화 기준점으로 공시한다(실측: 1.00 / 100 / 1000 / 10000).
_NORMALIZED_CONSTANTS = {1.0, 10.0, 100.0, 1000.0, 10000.0}

# 공시값을 못 쓴 사유 코드 (집계·디버깅용)
REF_SKIP_NO_CODE = "코드없음"        # Product.product_code 비어 있음
REF_SKIP_NO_ISSUE = "이력없음"       # product_code로 HistoricalIssue를 못 찾음
REF_SKIP_UNMATCHED = "대응불가"      # 자산 개수 불일치·티커 미해결·중복 등 모호
REF_SKIP_PROXY = "지수프록시"        # 국내 지수(서비스는 ETF 프록시) → 일부러 제외
REF_SKIP_ZERO = "공시0"              # std_price 결측 또는 0
REF_SKIP_NORMALIZED = "정규화값"     # 시세와 동떨어진 지수화 기준점 → 기각


def _cmp_ticker(ticker: str) -> str:
    """비교용 티커 키 — 국내 종목의 .KS/.KQ 접미사 차이를 흡수한다.

    SEIBro ISIN은 시장 구분 없이 항상 .KS를 주는데 서비스 티커맵은 코스닥을
    .KQ로 들고 있어, 접미사를 그대로 비교하면 같은 종목이 어긋난다.
    """
    t = (ticker or "").strip()
    return t[:-3] if re.match(r"^\d{6}\.(KS|KQ|KN)$", t) else t.upper()


def disclosed_asset_prices(assets_raw: str, seibro_assets):
    """서비스 자산명 → (공시 기준가|None, 사유) 매핑.

    자산 대응은 **티커**로 맞춘다 — 서비스는 'Micron', SEIBro는
    'MICRON TECHNOLOGY INC'처럼 표기가 전혀 달라 이름 비교가 불가능하기 때문.
    SEIBro는 자산 ISIN을 함께 주므로(US5951121038 등) 그쪽이 훨씬 안전하다.

    다음 중 하나라도 걸리면 **상품 전체를 폴백**한다 (틀린 값을 넣는 게 최악):
      · 자산 개수가 서로 다름
      · 어느 한쪽이라도 티커를 못 붙임
      · 같은 티커가 둘 이상 → 어느 자산에 붙일지 모호
      · 1:1 대응이 안 됨
    """
    names = split_assets(assets_raw)
    if not names:
        return {}
    if not seibro_assets or not isinstance(seibro_assets, (list, tuple)):
        return {n: (None, REF_SKIP_NO_ISSUE) for n in names}
    if len(names) != len(seibro_assets):
        return {n: (None, REF_SKIP_UNMATCHED) for n in names}

    svc = []                       # [(서비스 자산명, 비교키)]
    for n in names:
        tk = resolve_ticker(n)
        if not tk:
            return {n2: (None, REF_SKIP_UNMATCHED) for n2 in names}
        svc.append((n, _cmp_ticker(tk)))
    if len({k for _, k in svc}) != len(svc):
        return {n: (None, REF_SKIP_UNMATCHED) for n in names}

    sei = {}                       # 비교키 → SEIBro 자산 dict
    for a in seibro_assets:
        tk = resolve_seibro_ticker(a)
        if not tk:
            return {n: (None, REF_SKIP_UNMATCHED) for n in names}
        key = _cmp_ticker(tk)
        if key in sei:
            return {n: (None, REF_SKIP_UNMATCHED) for n in names}
        sei[key] = a
    if any(k not in sei for _, k in svc):
        return {n: (None, REF_SKIP_UNMATCHED) for n in names}

    out = {}
    for name, key in svc:
        if key in PROXY_INDEX_TICKERS:
            out[name] = (None, REF_SKIP_PROXY)
            continue
        raw = sei[key].get("std_price")
        try:
            price = float(str(raw).replace(",", "").strip())
        except (TypeError, ValueError):
            price = 0.0
        out[name] = (price, "") if price > 0 else (None, REF_SKIP_ZERO)
    return out


def disclosed_ref_prices(product):
    """상품의 SEIBro 공시 최초기준가격 {서비스 자산명: (기준가|None, 사유)}.

    연결 키는 Product.product_code == HistoricalIssue.isin 하나뿐이다.
    (product_code가 비어 있는 상품이 아직 많아 커버리지가 낮다 — 백필이 채우면
     여기 손대지 않아도 커버리지가 자동으로 올라간다.)
    """
    names = split_assets(getattr(product, "assets_raw", "") or "")
    code = (getattr(product, "product_code", "") or "").strip()
    if not code:
        return {n: (None, REF_SKIP_NO_CODE) for n in names}
    from core.models import HistoricalIssue      # 지연 임포트 (market은 django-free 유지)
    assets = (HistoricalIssue.objects.filter(isin=code)
              .values_list("assets", flat=True).first())
    if assets is None:
        return {n: (None, REF_SKIP_NO_ISSUE) for n in names}
    return disclosed_asset_prices(getattr(product, "assets_raw", "") or "", assets)


def fallback_ref_basis(product):
    """폴백 기준가의 (기준일, 거래일 오프셋). fetch_price_on에 그대로 넘기면 된다.

    ⚠ **기준가 결정 순서 ②의 유일한 교체 지점.**
      폴백 규칙이 또 바뀌면 이 함수(와 그 아래 base_price_date) 몸통만 고치면
      update_prices와 refresh_ref_price가 함께 따라온다. 호출부는 손댈 필요 없다.
    """
    return base_price_date(product)


def pick_ref_price(disclosed, fallback):
    """기준가 최종 결정 → (기준가, 출처). 출처는 '공시' 또는 '폴백'.

    공시값이 시세와 동떨어지면(정규화 기준점) 기각하고 폴백한다.
    비교할 폴백 시세가 아예 없으면 정규화 상수만 걸러내고 공시값을 쓴다 —
    아무 값도 없는 것보다 공식값이 낫다.
    """
    if disclosed and disclosed > 0:
        if fallback is None:
            if float(disclosed) not in _NORMALIZED_CONSTANTS:
                return float(disclosed), "공시"
        elif not is_normalized_std_price(disclosed, fallback):
            return float(disclosed), "공시"
    return fallback, "폴백"


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
