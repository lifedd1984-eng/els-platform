"""
ELS 상품설명 파싱 모듈
els_cleaner_app.py에서 이식 — 2026-07 실데이터 105건 검증 완료 버전.
"""

import re

# ──────────────────────────────────────────────
# 지수 키워드
# ──────────────────────────────────────────────
INDEX_KEYWORDS = [
    'KOSPI200', 'KOSPI 200', '코스피200', '코스피',
    'S&P500', 'S&P 500', 'SP500',
    'Nikkei225', 'Nikkei 225', '니케이225', '니케이',
    'Euro Stoxx 50', 'EuroStoxx50', 'EUROSTOXX50', 'Eurostoxx',
    'HSCEI', 'HSI', '항셍',
    'NASDAQ100', 'NASDAQ 100', '나스닥',
    '국채', '채권', 'USD환율', 'USD/KRW', '환율',
    'WTI', '원유', 'Gold', '금',
]


def classify_asset(text):
    if not text:
        return None
    parts = [p.strip() for p in re.split(r'[,/]+', str(text)) if p.strip()]
    if not parts:
        return None
    all_index = all(
        any(kw.upper() in part.upper() for kw in INDEX_KEYWORDS)
        for part in parts
    )
    return '지수형' if all_index else '종목형'


def extract_ki(text):
    if not text:
        return None
    text = str(text)
    # NoKI 판별 (한/영 + 원금지급형 + Digital형 + 하이파이브형)
    if re.search(r'[Nn]o\s*KI|KI\s*없음|노\s*KI|NoKI', text):
        return 'NoKI'
    if re.search(r'하이파이브|Hi-Five|원금지급형|원금추가지급형|Digital형|원금보장', text):
        return 'NoKI'
    patterns = [
        r'/(\d+)KI[\]\)]',           # /25KI]  /40KI)
        r',\s*(\d+)KI\s*\(',         # ,30KI(
        r'[/\-]{1,2}\s*KI\s*(\d+)',  # /KI 30  --KI 45
        r'KI\s*(\d+)',               # KI 30  KI30  (범용 catch-all)
        r'/\s*(\d+)\s*KI\b',         # /30 KI
        r'(\d+)\s*KI\b',             # 30KI
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def extract_barriers(text):
    if not text:
        return None
    text = str(text)

    m = re.search(r'(?:Lizard|Safezone)?StepDown형\[([^\]]+)/\d+KI\]', text)
    if m:
        raw = re.sub(r'\([^)]*\)', '', m.group(1))
        vals = re.findall(r'\d+', raw)
        if vals:
            return vals

    m = re.search(r'\d+KI\s*\(([0-9,\s]+)\)', text)
    if m:
        vals = [v.strip() for v in m.group(1).split(',') if v.strip()]
        if vals:
            return vals

    m = re.search(r'\(([0-9\-]+)\)\s*\d+KI', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    m = re.search(r'/([0-9\-]+)/KI\d+', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    m = re.search(r'\d+y/\d+m\s+([0-9\-\(\)]+)\s+KI', text)
    if m:
        raw = re.sub(r'\([^)]*\)', '', m.group(1))
        vals = [v for v in raw.split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    m = re.search(r',\s*([0-9\-]+),\s*KI\s*\d+', text)
    if m:
        vals = m.group(1).split('-')
        if vals and len(vals) >= 2:
            return vals

    m = re.search(r',\s*([0-9\-]+)/KI\s+\d+', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    m = re.search(r'조기상환 기회 부여\s+([0-9\-]+)\s*/?\s*KI', text)
    if m:
        vals = m.group(1).rstrip('/').split('-')
        if vals:
            return vals

    m = re.search(r'총\s*\d+회\(([0-9\-~(~)]+)/KI', text)
    if m:
        raw = re.sub(r'\([^)]*\)', '', m.group(1))
        raw = re.sub(r'~\d+', '', raw)
        vals = [v for v in raw.split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    m = re.search(r'(?<!\d)([0-9]{2}(?:-[0-9]{2})+)/\s*\d+\s*KI', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    m = re.search(r'\(([0-9\-]+)\)\s*NoKI', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    m = re.search(r'조기상환형,\s*([0-9\-]+),\s*KI', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    m = re.search(r'원금지급형,\s*([0-9\-]+),', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    m = re.search(r'/\s*([0-9\-]+)\s+KI\s+\d+\s*/', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    # KB형: Step-Down형N/N/N/N--KI  (슬래시 구분 배리어)
    m = re.search(r'Step-Down형([\d/(L)\s]+?)--KI', text)
    if m:
        raw = re.sub(r'\(L\d+\)', '', m.group(1))
        vals = [v.strip() for v in raw.split('/') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # Lizard Step-Down형 슬래시 구분
    m = re.search(r'Lizard Step-Down형([\d/(L)\s]+?)--KI', text)
    if m:
        raw = re.sub(r'\(L\d+\)', '', m.group(1))
        vals = [v.strip() for v in raw.split('/') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # 온라인 상품 형식: N-N-N/KI NN  (콤마 없이 바로)
    m = re.search(r'상환,\s*([0-9\-]+)/KI\s*\d+', text)
    if m:
        vals = m.group(1).split('-')
        if vals:
            return vals

    # 신한 하이파이브형: 스텝다운 (N-N-N-...) 괄호 배리어
    m = re.search(r'스텝다운\s*\(([0-9\-]+)\)', text)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    return None


def extract_period(text, issue_date=None, expiry_date=None, barriers=None):
    if not text:
        text = ''
    text = str(text)
    for pat in [
        r'(\d+)개월단위 조기상환',
        r'상환주기\s*(\d+)개월',
        r'만기\s*(\d+)개월',
        r'\d+y/(\d+)m\b',
        r'/(\d+)개월/',
        r'/(\d+)개월,',
        r'조기상환 평가주기\s*(\d+)개월',
        r'(\d+)개월 평가',
        r'매\s*(\d+)개월마다',
        r'(\d+)개월마다\s*(?:총|조기|기회)',
        r'/(\d+)개월단위',
    ]:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    if issue_date and expiry_date and barriers:
        try:
            d1, d2 = str(int(issue_date)), str(int(expiry_date))
            total = (int(d2[:4]) - int(d1[:4])) * 12 + (int(d2[4:6]) - int(d1[4:6]))
            if len(barriers) > 0:
                return round(total / len(barriers))
        except Exception:
            pass
    return None
