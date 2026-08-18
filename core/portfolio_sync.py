"""구글 시트 동기화 피드 — 조 팀장 'ELS투자 리스트' 시트가 읽어 가는 JSON.

방향이 거꾸로다. 서버가 시트에 쓰지 않고 **시트가 이 주소를 읽어 간다**.
그래야 구글 자격증명(서비스 계정 키)이 서버에 들어오지 않는다 — 키가 새면
시트 하나가 아니라 그 계정이 닿는 문서 전부가 열린다.

값은 전부 core.portfolio_export가 만든다. 이 모듈은 열을 고르고 이름을 시트
머리글 표기로 바꿀 뿐, 단위(원→만원)·날짜 형식·예상수익 계산을 다시 구현하지
않는다. 다시 만들면 엑셀 다운로드(/portfolio/export/)와 시트가 서로 다른 숫자를
내게 되고, 어느 쪽이 맞는지 아무도 모르게 된다.

내보내지 않는 열이 둘 있다 — 시트에서 사람이 채우는 칸이다.
    회사 : DB에 아예 없다. Investment.broker_account가 전 건 비어 있고,
           시트의 '모두벤처스/택스턴' 구분은 조 팀장 머릿속에만 있다.
    비고 : portfolio_export.note()가 비슷한 값을 만들긴 한다(월지급·L50). 하지만
           시트 쪽 비고에는 '실물인도', 'L 50/2배'처럼 손으로만 넣은 값이 섞여
           있어 덮어쓰면 복구가 안 된다. 피드에 아예 싣지 않는 것이 가장 확실한
           보호다 — 스크립트가 실수로라도 쓸 값이 없어야 한다.
"""

from django.utils import timezone

from core import portfolio_export as pfx

# 시트 4행 머리글 표기 → portfolio_export.COLUMNS 인덱스.
# 이름이 하나 다르다: 시트는 '상환', 모듈은 '상환여부'. 시트 표기를 따른다 —
# Apps Script가 머리글 '글자'로 열 위치를 찾기 때문이다(열 문자를 박아 두면
# 조 팀장이 열을 하나 끼워 넣는 순간 엉뚱한 칸에 쓴다).
# 인덱스 15(비고)가 빠져 있는 것은 실수가 아니다. 위 docstring 참조.
SHEET_COLUMNS = [
    ("증권사", 0),
    ("상품번호", 1),
    ("기초자산", 2),
    ("발행일", 3),
    ("만기일", 4),
    ("금리", 5),
    ("투자금액", 6),
    ("상환", 7),
    ("낙인", 8),
    ("1회차", 9),
    ("마지막", 10),
    ("주기", 11),
    ("구분", 12),
    ("투자월", 13),
    ("첫 조기", 14),
    ("예상수익", 16),
]

# 시트 행과 DB 투자를 맞추는 키. 이 둘은 피드에서 절대 빠지면 안 된다.
KEY_COLUMNS = ("증권사", "상품번호")

# 시트에 있지만 이 피드가 건드리지 않는 열 (사람이 채우는 칸)
UNTOUCHED_COLUMNS = ("회사", "비고")

COLUMN_NAMES = [name for name, _ in SHEET_COLUMNS]


def build_rows(investments):
    """투자 목록 → 시트 머리글 이름을 키로 하는 dict 리스트.

    정렬·값 변환은 portfolio_export.build_rows가 한 그대로다.
    """
    return [
        {name: row[idx] for name, idx in SHEET_COLUMNS}
        for row in pfx.build_rows(investments)
    ]


def build_payload(investments, username=""):
    """시트가 받아 갈 응답 한 덩어리.

    count를 따로 싣는 이유: Apps Script가 rows 길이와 대조해 응답이 잘렸는지
    본다. 잘린 JSON을 성공으로 오인하면 시트 일부만 갱신된 채 조용히 끝난다.
    """
    rows = build_rows(investments)
    return {
        "generated_at": timezone.localtime().isoformat(timespec="seconds"),
        "username": username,
        "count": len(rows),
        "columns": COLUMN_NAMES,
        "key_columns": list(KEY_COLUMNS),
        "untouched_columns": list(UNTOUCHED_COLUMNS),
        "rows": rows,
    }
