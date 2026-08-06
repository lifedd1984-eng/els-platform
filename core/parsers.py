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
    'KOSDAQ150', 'KOSDAQ 150', '코스닥150', '코스닥',
    'S&P500', 'S&P 500', 'SP500',
    'Nikkei225', 'Nikkei 225', '니케이225', '니케이',
    'Euro Stoxx 50', 'EuroStoxx50', 'EUROSTOXX50', 'Eurostoxx',
    'HSCEI', 'HSI', '항셍',
    'NASDAQ100', 'NASDAQ 100', '나스닥',
    '국채', '채권', 'USD환율', 'USD/KRW', '환율',
    'WTI', '원유', 'Gold', '금',
]

# ──────────────────────────────────────────────
# 원금보장 표기 — 상품유형 판정과 낙인부재 판정이 공유하는 단 하나의 목록
# ──────────────────────────────────────────────
# 한글은 부분문자열이 연속하지 않는다. '원금지급형' 하나만 적으면
# '원금**추가**지급형'(신한 ELB·DLB의 표기)이 걸리지 않는다 — 전부 나열해야 한다.
# 예전에 scrape_kofia는 '원금지급형'만, extract_ki는 아래 목록 전부를 보고 있어
# 같은 설명 원문을 두 코드가 다르게 읽었다. 그 목록을 여기 한 곳으로 모았다.
PRINCIPAL_PROTECTED_WORDS = (
    '원금지급형', '원금추가지급형', '원금지급추구형', '원금보장',
)
_RE_PROTECTED_WORD = re.compile('|'.join(PRINCIPAL_PROTECTED_WORDS))

# 상품명·설명 안의 ELB·DLB 표기. 앞뒤에 영문자가 붙으면 다른 단어이므로 제외한다
# ("신한투자-ELB-4118", "KB able ELB 제371호", "BNK…기타파생결합사채(DLB)"는 잡히고
#  "ELBOW 인덱스"는 안 잡힌다. "한화스마트ONELB164(ELB)"는 뒤쪽 "(ELB)"로 잡힌다).
_RE_BOND_TOKEN = re.compile(r'(?<![A-Za-z])(?:ELB|DLB)(?![A-Za-z])', re.I)
# 기초자산이 주가가 아닌 부류(DLS·DLB) 표기
_RE_OTHER_TOKEN = re.compile(r'(?<![A-Za-z])(?:DLS|DLB)(?![A-Za-z])', re.I)

# 법정 명칭 — 원금보장형은 '파생결합사채', 원금비보장형은 '파생결합증권'이다.
# (운영 4,769건 실측: '파생결합사채'와 ELS/DLS 토큰이 함께 든 상품명 0건.)
_BOND_WORD = '파생결합사채'
# '주가연계파생결합…'이면 ELS·ELB, '기타파생결합…'이면 DLS·DLB.
_OTHER_WORD = '기타파생결합'


def has_bond_name(name):
    """상품명이 원금보장형(ELB·DLB)임을 스스로 밝히면 True.

    상품명은 발행사가 공시하는 법정 명칭이라 설명 원문보다 신뢰도가 높다.
    다만 엑셀 수입분은 name이 회차번호뿐이라(운영 3,771건) 이것만으론 부족하다.
    """
    name = str(name or '')
    return bool(_RE_BOND_TOKEN.search(name)) or _BOND_WORD in name


def is_principal_protected(name, description=None, ki=None):
    """원금보장형(ELB·DLB)이면 True.

    ki가 안전장치다
      원금보장형에는 낙인 배리어라는 개념 자체가 없다. 반대로 ki가 박혀 있으면
      그건 낙인이 걸린 원금비보장 상품이므로, 설명에 '(80%원금지급형)'처럼
      부분보장 문구가 있어도 원금보장형으로 보지 않는다.
      이 한 줄이 교보증권K(ELS) 19/23/27/31 등 ki=40인 6건을 ELS로 남긴다.

    상품명 → 설명 순으로 본다
      이름이 회차번호뿐인 엑셀 수입분은 설명이 유일한 단서라 설명도 함께 본다.
    """
    if ki is not None:
        return False
    if has_bond_name(name):
        return True
    desc = str(description or '')
    return bool(_RE_BOND_TOKEN.search(desc)) or bool(_RE_PROTECTED_WORD.search(desc))


def is_other_linked(name, description=None):
    """기초자산이 주가가 아닌 부류(DLS·DLB)면 True.

    표기가 명시된 것만 본다 — 기초자산 문자열로 역추정하지 않는다.
    (근거 없는 추정으로 유형이 흔들리는 것보다 ELS·ELB로 남는 편이 낫다.)
    """
    for text in (str(name or ''), str(description or '')):
        if _OTHER_WORD in text or _RE_OTHER_TOKEN.search(text):
            return True
    return False


def classify_product_type(name, description=None, ki=None):
    """상품유형 4종(ELS/DLS/ELB/DLB) 판정 — 상품명과 설명, 낙인을 함께 본다.

    두 축이 독립이다.
      · 원금보장 여부: 증권(S, 원금비보장) ↔ 사채(B, 원금보장)
      · 기초자산 종류: 주가연계(E) ↔ 그 밖(D, 금리·환율 등)
    """
    protected = is_principal_protected(name, description, ki)
    other = is_other_linked(name, description)
    if protected:
        return 'DLB' if other else 'ELB'
    return 'DLS' if other else 'ELS'


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
    # ① 명시적 '낙인 없음' 선언 — 최우선 확정. (NoKI 상품은 KI가 없으므로,
    #    'NoKI 3년' 같은 표기에서 catch-all이 '3'을 KI로 오인하는 것을 막는다.)
    if re.search(r'no\s*KI|KI\s*없음|노\s*KI', text, re.IGNORECASE):
        return 'NoKI'

    # ② 명시적 KI 숫자 — 원금(부분)보장·실물상환 등 어떤 구조추론보다 우선.
    #    (예: '실물주식인도'·'80%원금지급형'이라도 KI25면 낙인 25.)
    patterns = [
        r'/(\d+)KI[\]\)]',           # /25KI]  /40KI)
        r',\s*(\d+)KI\s*\(',         # ,30KI(
        r'[/\-]{1,2}\s*KI\s*(\d+)',  # /KI 30  --KI 45
        r'KI[_\s]*(\d+)',            # KI 30  KI30  KI_30  (범용 catch-all)
        r'/\s*(\d+)\s*KI\b',         # /30 KI
        r'(\d+)\s*KI\b',             # 30KI
        r',(\d+)%-\([0-9,]+\)%',     # KOFIA형: ,30%-(85,85,80,...)%  ("KI" 텍스트 없이 %로만 표기)
        r'knock\s*in\s*(\d+)',       # KB증권형: "knock in 35" (영문 표기)
        r'/(\d+)\(종가\)\]',          # 한국투자증권형: "...50/35(종가)]" ("KI" 텍스트 없이 종가 표기)
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)

    # ③ KI 숫자가 없을 때만 '낙인 부재' 판정
    #    원금보장 표기는 PRINCIPAL_PROTECTED_WORDS 하나만 본다 — 상품유형 판정과
    #    같은 목록이라야 같은 설명 원문을 두 곳이 다르게 읽지 않는다.
    if re.search(r'하이파이브|Hi-Five|Digital형', text) or _RE_PROTECTED_WORD.search(text):
        return 'NoKI'
    # 상승참여형: 낙인 배리어 없이 만기 시 상승/하락 참여율만으로 정산하는 구조
    if re.search(r'상승참여율', text):
        return 'NoKI'
    # 실물상환형: 손실 시 현금이 아닌 주식 실물로 상환 (KI 텍스트가 전혀 없는 경우만 낙인 부재)
    if re.search(r'실물상환|실물인수|실물결제|실물주식인도', text):
        return 'NoKI'
    # Digital Call형: 배리어 도달 여부만으로 이분법 지급 — 스텝다운형 낙인(KI) 구조 자체가 없음
    if re.search(r'Digital\s*Call|디지털\s*콜', text, re.IGNORECASE):
        return 'NoKI'
    # 채권/금리 연계 DLS: 스텝다운 ELS 구조가 아니라 낙인 개념이 적용되지 않음
    if re.search(r'국고채|KTB|금리\s*연계', text):
        return 'NoKI'
    # 하나증권형: "3y/6m 90-85-85-80-75-65 ..." — 조기상환 배리어는 있으나
    # KI/knock in 관련 텍스트가 전혀 없음(위 패턴 전부 실패) → 낙인 없는 상품으로 판정
    if re.search(r'\d+y/\d+m,?\s+[\d\-]+', text):
        return 'NoKI'

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

    # 한국투자증권형: StepDown형[80-80-...-50/35(종가)] ("KI" 텍스트 없이 종가 표기)
    m = re.search(r'(?:Lizard|Safezone)?StepDown형\[([^\]]+)/\d+\(종가\)\]', text)
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

    # 범용 폴백: 접두 문맥과 무관하게 "숫자-숫자-...(선택적 (Lxx))" 가
    # 바로 KI숫자 앞에 오는 모든 경우 (KOFIA 원문 다양한 표기 대응, "KI_30" 언더스코어 포함)
    # 배리어열과 KI 사이 구분자로 콤마(유진 "...70-60, KI30")·슬래시 모두 허용
    m = re.search(r'([0-9]{2}(?:\(L\d+\))?(?:-[0-9]{2}(?:\(L\d+\))?){1,})\s*[,/]?\s*KI[_\s]*\d+', text)
    if m:
        raw = re.sub(r'\(L\d+\)', '', m.group(1))
        vals = [v for v in raw.split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # KOFIA형: ,30%-(85,85,80,80,75,75)%  (콤마 구분 배리어, "KI" 텍스트 없음)
    m = re.search(r',\d+%-\(([0-9,]+)\)%', text)
    if m:
        vals = [v.strip() for v in m.group(1).split(',') if v.strip()]
        if vals:
            return vals

    # 배리어 숫자열 바로 뒤에 NoKI/no ki (콤마·슬래시·공백으로 연결, 대소문자 무관)
    m = re.search(
        r'([0-9]{2}(?:\(L\d+\))?(?:-[0-9]{2}(?:\(L\d+\))?){1,})\s*[,/\s]\s*no\s*ki',
        text, re.IGNORECASE,
    )
    if m:
        raw = re.sub(r'\(L\d+\)', '', m.group(1))
        vals = [v for v in raw.split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # 하나증권 실물상환형: "90(3m)-90(4m)-90(5m)-75, Cpn=" — (Nm) 평가월 표기 배리어열
    m = re.search(r'([\d()a-zA-Z\-]+)\s*,\s*Cpn=', text)
    if m and re.search(r'\(\d+m\)', m.group(1)):
        raw = re.sub(r'\(\d+m\)', '', m.group(1))
        vals = [v for v in raw.split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # 하나증권 노낙인형: "3y/6m 90-85-85-80-75-65 ..." — KI 텍스트 없이 배리어만
    m = re.search(r'\d+y/\d+m,?\s+([\d\-]+)', text)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # KB증권형: Step-Down형85/85/80/80/75/70--knock in 40 (슬래시 구분, "knock in" 앞)
    # Lizard형은 90(L75)/90(L70)/... 처럼 (Lxx) 리자드 마커가 끼어들 수 있음
    m = re.search(r'Step-Down형([\d/()L]+)--\s*knock\s*in\s*\d+', text, re.IGNORECASE)
    if m:
        raw = re.sub(r'\(L\d+\)', '', m.group(1))
        vals = [v for v in raw.split('/') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # KB증권 NoKI형: 베리어 85-85-85-85-85-85--no knock in
    m = re.search(r'베리어\s*([\d\-]+)--\s*no\s*knock\s*in', text, re.IGNORECASE)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # 대괄호 배리어: [70-70-...-70] (Hi-Five형 등)
    m = re.search(r'\[([\d\-]+)\]', text)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # 삼성증권 Ultra형: (100,100,100,100,100,100)%
    m = re.search(r'\(([\d,]+)\)%', text)
    if m:
        vals = [v.strip() for v in m.group(1).split(',') if v.strip()]
        if vals:
            return vals

    # 메리츠증권 원금지급추구형: 70-70-...-70/월쿠폰배리어 65 (배리어 미달 시 쿠폰 미지급, 낙인 아님)
    m = re.search(r'([\d\-]+)/\s*월쿠폰배리어', text)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # 교보증권형: 100-100-100-만기 no ki (배리어 사이에 "만기" 토큰이 끼는 경우)
    m = re.search(r'([\d\-]+)-?만기\s*no\s*ki', text, re.IGNORECASE)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if vals:
            return vals

    # 한국투자증권 상승참여형: "조기상환: 85이상(4개월...)" — 단일 조기상환 배리어
    m = re.search(r'조기상환:\s*(\d+)\s*이상', text)
    if m:
        return [m.group(1)]

    # ── 추가 형식 보강 (아래는 모두 위 패턴이 전부 실패한 경우에만 시도) ──

    # 유안타증권: 총 N회(배리어열/CB nn/KI nn) — 괄호 안 배리어열
    m = re.search(r'\(([\d\-]+)\s*/\s*CB\s*\d+\s*/\s*KI\s*\d+\)', text)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if len(vals) >= 2:
            return vals

    # 한국투자 등: (Lizard/Power/Safezone…)StepDown형[배리어열] — 대괄호 내용 일반 추출
    m = re.search(r'StepDown형\[([^\]]+)\]', text)
    if m:
        raw = re.sub(r'\(L?\d+\)', '', m.group(1))          # (L50) 리자드 마커 제거
        vals = [v for v in re.split(r'[-/]', raw) if re.match(r'^\d+$', v.strip())]
        # 끝에 붙은 KI(예: .../30KI)는 위 split에서 'KI' 때문에 자동 탈락
        if len(vals) >= 2:
            return vals

    # KB증권 Ultra Hi-Five: "조기 상환 베리어 배리어열--no KI"
    m = re.search(r'베리어\s*([\d\-]+)\s*--\s*no\s*ki', text, re.IGNORECASE)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if len(vals) >= 2:
            return vals

    # 배리어열(2~3자리·(Lxx) 허용·대시/슬래시 구분) + NoKI 계열
    # (no ki / NO-KI / noki / 노KI / KI없음), 사이 %·콤마·슬래시 허용
    m = re.search(
        r'([0-9]{2,3}(?:\(L?\d+\))?(?:[-/][0-9]{2,3}(?:\(L?\d+\))?){2,})'
        r'\s*%?\s*[,/]?\s*(?:no[\s\-]*ki|노\s*ki|ki\s*없음)',
        text, re.IGNORECASE,
    )
    if m:
        raw = re.sub(r'\(L?\d+\)', '', m.group(1))
        vals = [v for v in re.split(r'[-/]', raw) if re.match(r'^\d+$', v.strip())]
        if len(vals) >= 2:
            return vals

    # NH 온라인 원금비보장: "원금비보장, 배리어열" (KI 텍스트 없음)
    m = re.search(r'원금비보장,\s*([0-9]{2,3}(?:-[0-9]{2,3}){2,})', text)
    if m:
        vals = [v for v in m.group(1).split('-') if re.match(r'^\d+$', v.strip())]
        if len(vals) >= 2:
            return vals

    return None


# 명시적 "주기 텍스트" 패턴 — 있으면 주기 확정(최우선)
# "6개월이후1개월단위" 처럼 1차 평가와 이후 간격이 다른 표기.
# 아래 일반 패턴보다 먼저 봐야 한다 — "3년만기 6개월이후1개월단위"에서
# r'만기\s*(\d+)개월'이 6을 물어 주기를 6개월로 잘못 읽는 사례가 있었다
# (한국투자 18932·18935: 실제는 첫 6개월 후 매월, 배리어 31단계). 2026-07-31
_STEP_FIRST_INTERVAL = re.compile(r'(\d+)\s*개월\s*이후\s*(\d+)\s*개월\s*단위')

_PERIOD_TEXT_PATTERNS = [
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
]


def extract_step_first_interval(text):
    """'N개월이후M개월단위' 표기에서 (1차평가 개월, 이후 간격) 반환. 없으면 None."""
    if not text:
        return None
    m = _STEP_FIRST_INTERVAL.search(str(text))
    return (int(m.group(1)), int(m.group(2))) if m else None


def extract_period_text(text):
    """설명에 명시적 주기 텍스트가 있으면 정수 개월 반환, 없으면 None (폴백 없음)."""
    if not text:
        return None
    text = str(text)
    step = extract_step_first_interval(text)
    if step:
        return step[1]
    for pat in _PERIOD_TEXT_PATTERNS:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


def infer_schedule(n_barriers, issue_date, expiry_date, desc):
    """조기상환 스케줄(1차까지, 이후간격, 추정여부) 판정.

    반환: (first_eval_months, interval_months, estimated) 또는 None.
    - 텍스트 주기 우선(확정) → 규칙1(균등 정수) → 규칙2(첫3개월 가정) → None.
    - estimated는 항상 False(확정된 경우만 값 반환). 판정 불가 시 None을 돌려주고,
      호출측(reparse)이 폴백값을 채우며 schedule_estimated=True로 표시한다.
    - day 아티팩트 보정: 발행/만기일의 '일(day)'이 달라 총개월이 ±1 어긋날 수 있어
      span, span+1, span-1 세 값을 시도한다.
    """
    if not n_barriers or n_barriers < 1:
        return None

    # (0-1) '6개월이후1개월단위' — 1차와 이후 간격이 다른 비균등 구조
    step = extract_step_first_interval(desc)
    if step and step[0] > 0 and step[1] > 0:
        return (step[0], step[1], False)

    # (0) 텍스트 주기 최우선 — 균등으로 확정
    tp = extract_period_text(desc)
    if tp and tp > 0:
        return (tp, tp, False)

    ym1 = _year_month(issue_date)
    ym2 = _year_month(expiry_date)
    if not ym1 or not ym2:
        return None
    span = (ym2[0] - ym1[0]) * 12 + (ym2[1] - ym1[1])
    if span <= 0:
        return None

    candidates = [span, span + 1, span - 1]  # day 아티팩트 ±1 보정

    # (1) 규칙1: 총개월 ÷ 배리어수가 정수 → 균등
    for total in candidates:
        if total > 0 and total % n_barriers == 0:
            interval = total // n_barriers
            if interval > 0:
                return (interval, interval, False)

    # (2) 규칙2: 첫 3개월 가정 — (총개월-3) ÷ (배리어수-1)이 정수
    if n_barriers >= 2:
        for total in candidates:
            rem = total - 3
            if rem > 0 and rem % (n_barriers - 1) == 0:
                interval = rem // (n_barriers - 1)
                if interval > 0:
                    return (3, interval, False)

    # (3) 판정 불가
    return None


def extract_period(text, issue_date=None, expiry_date=None, barriers=None):
    if not text:
        text = ''
    text = str(text)
    step = extract_step_first_interval(text)
    if step:
        return step[1]
    for pat in _PERIOD_TEXT_PATTERNS:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    # 폴백: 설명에 주기 텍스트가 없으면 (만기-발행) 개월수 ÷ 배리어 회차로 추정
    if issue_date and expiry_date and barriers:
        ym1 = _year_month(issue_date)
        ym2 = _year_month(expiry_date)
        if ym1 and ym2:
            total = (ym2[0] - ym1[0]) * 12 + (ym2[1] - ym1[1])
            n = len(barriers)
            if total > 0 and n > 0:
                # 대개 3/4/6개월 등 정수 주기 → 가장 가까운 통상값으로 스냅
                raw = total / n
                for cand in (1, 3, 4, 6, 12):
                    if abs(raw - cand) <= 0.75:
                        return cand
                return round(raw)
    return None


def _year_month(d):
    """date 객체 / 정수(20260723) / 문자열 어느 형태든 (year, month) 반환."""
    from datetime import date as _date
    if isinstance(d, _date):
        return (d.year, d.month)
    s = str(d).replace("-", "").replace(".", "").strip()
    if len(s) >= 6 and s[:8].isdigit():
        return (int(s[:4]), int(s[4:6]))
    return None
