"""AI 리서치 — 자연어 질문을 상품 필터(JSON)로 변환해 기존 쿼리로 실행.

설계 원칙 (2026-07-28 확정):
- AI는 답을 만들지 않고 "필터"만 만든다. 결과는 항상 DB의 실제 데이터.
  → 환각 원천 차단, 숫자를 지어낼 수 없음.
- 자유 문답("이 상품 어때?")은 지원하지 않는다. 조회·정렬·필터 전용.
  → 투자권유 소지 차단. 레이더와 같은 "시스템 분류"의 법적 성격 유지.
- 상품 데이터를 모델에 보내지 않는다. 질문 + 스키마만 전송 (요청당 약 0.4원).
"""
import json
import logging

import requests
from django.conf import settings

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5"
TIMEOUT = 20

# Haiku가 채울 필터 스키마. strict tool use라 형식이 보장된다.
FILTER_TOOL = {
    "name": "product_filter",
    "description": "사용자 질문을 ELS 상품 검색 필터로 변환한다. 질문에 없는 조건은 넣지 않는다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string", "enum": ["products", "my_portfolio"],
                "description": "내 보유/포트폴리오 관련 질문이면 my_portfolio, 그 외 products",
            },
            "subscribing_only": {
                "type": "boolean",
                "description": "청약 가능한(마감 전) 상품만 원하면 true. '지금 살 수 있는' 등",
            },
            "asset_type": {"type": "string", "enum": ["종목형", "지수형"]},
            "ki_max": {"type": "number", "description": "낙인 상한 (이하). '낙인 40 이하' → 40"},
            "ki_min": {"type": "number", "description": "낙인 하한 (이상)"},
            "no_ki": {"type": "boolean", "description": "노낙인 상품만 원하면 true"},
            "yield_min": {"type": "number", "description": "연수익률 하한 (%)"},
            "loss_prob_max": {"type": "number", "description": "손실확률 상한 (%). '안전한' → 1 정도"},
            "early_1y_min": {"type": "number", "description": "1년내 조기상환 확률 하한 (%)"},
            "issuer": {"type": "string", "description": "발행사명 일부. 예: 키움, 삼성"},
            "asset_contains": {"type": "string", "description": "기초자산명 일부. 예: 테슬라, S&P"},
            "badge_only": {"type": "boolean", "description": "레이더 배지(아주 강한/강한 신호) 상품만"},
            "eval_within_days": {
                "type": "integer",
                "description": "다음 평가일이 N일 이내 (my_portfolio 전용). '다음 달 평가' → 31",
            },
            "sort": {
                "type": "string",
                "enum": ["yield", "ki", "loss_prob", "sub_end", "early_1y", "next_eval"],
                "description": "정렬 기준. 미지정 시 yield",
            },
            "sort_dir": {"type": "string", "enum": ["asc", "desc"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "unanswerable": {
                "type": "boolean",
                "description": "조회·필터로 답할 수 없는 질문(의견·추천·예측 요구 등)이면 true",
            },
        },
        "required": [],
    },
}

SYSTEM = (
    "너는 한국 ELS 상품 데이터베이스의 검색 필터 생성기다. "
    "사용자 질문을 product_filter 도구 호출 하나로 변환한다. "
    "질문에 명시되지 않은 조건은 절대 추가하지 않는다. "
    "'추천해줘', '어때?', '살까?' 같은 의견·판단 요구는 unanswerable=true로 표시한다. "
    "'안전한'은 loss_prob_max=1, '고수익'은 sort=yield/desc 로 해석한다."
)


def ask(question):
    """질문 → 필터 dict. 실패 시 (None, 오류메시지)."""
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, "AI 검색이 아직 설정되지 않았습니다."
    try:
        r = requests.post(
            API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 300,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": question[:300]}],
                "tools": [FILTER_TOOL],
                "tool_choice": {"type": "tool", "name": "product_filter"},
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return block["input"], None
        return None, "필터 생성에 실패했습니다. 질문을 바꿔 보세요."
    except requests.Timeout:
        return None, "응답이 늦어지고 있습니다. 잠시 후 다시 시도해 주세요."
    except Exception:
        log.exception("AI 리서치 호출 실패")
        return None, "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."


# 필터 → 사람이 읽는 조건 칩 (적용된 조건을 투명하게 보여준다)
_CHIP = {
    "subscribing_only": lambda v: "청약 가능" if v else None,
    "asset_type": lambda v: v,
    "ki_max": lambda v: f"낙인 {v:g} 이하",
    "ki_min": lambda v: f"낙인 {v:g} 이상",
    "no_ki": lambda v: "노낙인" if v else None,
    "yield_min": lambda v: f"수익률 {v:g}% 이상",
    "loss_prob_max": lambda v: f"손실확률 {v:g}% 이하",
    "early_1y_min": lambda v: f"1년내 상환 {v:g}% 이상",
    "issuer": lambda v: f"발행사 {v}",
    "asset_contains": lambda v: f"기초자산 {v}",
    "badge_only": lambda v: "레이더 배지" if v else None,
    "eval_within_days": lambda v: f"평가일 {v}일 이내",
}

_SORT_LABEL = {
    "yield": "수익률", "ki": "낙인", "loss_prob": "손실확률",
    "sub_end": "마감일", "early_1y": "1년내 상환", "next_eval": "다음 평가일",
}


def describe(f):
    """적용된 조건을 칩 문자열 리스트로."""
    chips = []
    for key, fmt in _CHIP.items():
        if f.get(key) is not None:
            c = fmt(f[key])
            if c:
                chips.append(c)
    sort = f.get("sort")
    if sort == "next_eval":
        chips.append("평가일 " + ("늦은순" if f.get("sort_dir") == "desc" else "빠른순"))
    elif sort in _SORT_LABEL:
        chips.append(f"{_SORT_LABEL[sort]} {'낮은순' if f.get('sort_dir') == 'asc' else '높은순'}")
    if f.get("scope") == "my_portfolio":
        chips.insert(0, "내 포트폴리오")
    return chips


def run_filter(f, user):
    """필터 실행 → (Product 리스트, 오류메시지). 데이터는 전부 DB에서."""
    from datetime import date, timedelta
    from .models import Product, Investment

    if f.get("unanswerable"):
        return None, ("의견이나 추천은 드릴 수 없습니다. "
                      "조건 검색만 가능합니다 — 예: \"낙인 40 이하 지수형 중 수익률 높은 3개\"")

    scope = f.get("scope", "products")
    limit = min(int(f.get("limit") or 10), 50)

    if scope == "my_portfolio":
        if not user.is_authenticated:
            return None, "내 포트폴리오 질문은 로그인 후 이용할 수 있습니다."
        invs = list(Investment.objects.filter(user=user, status="보유중")
                    .select_related("product"))
        if f.get("eval_within_days"):
            edge = date.today() + timedelta(days=int(f["eval_within_days"]))
            invs = [i for i in invs
                    if i.next_evaluation and i.next_evaluation["date"] <= edge]
        if f.get("sort") == "next_eval":  # 평가일 임박순 (DB 컬럼이 아니라 여기서 정렬)
            invs.sort(key=lambda i: i.next_evaluation["date"] if i.next_evaluation else date.max,
                      reverse=f.get("sort_dir") == "desc")
            ids = [i.product_id for i in invs][:limit]
            by_id = Product.objects.in_bulk(ids)
            return [by_id[i] for i in ids if i in by_id], None
        qs = Product.objects.filter(id__in=[i.product_id for i in invs])
    else:
        qs = Product.objects.all()
        if f.get("subscribing_only"):
            qs = qs.filter(sub_end__gte=date.today())

    if f.get("asset_type"):
        qs = qs.filter(asset_type=f["asset_type"])
    if f.get("no_ki"):
        qs = qs.filter(is_no_ki=True)
    if f.get("ki_max") is not None:
        qs = qs.filter(is_no_ki=False, ki__lte=f["ki_max"])
    if f.get("ki_min") is not None:
        qs = qs.filter(is_no_ki=False, ki__gte=f["ki_min"])
    if f.get("yield_min") is not None:
        qs = qs.filter(yield_rate__gte=f["yield_min"])
    if f.get("loss_prob_max") is not None:
        qs = qs.filter(loss_prob__isnull=False, loss_prob__lte=f["loss_prob_max"])
    if f.get("issuer"):
        qs = qs.filter(issuer__icontains=f["issuer"])
    if f.get("asset_contains"):
        qs = qs.filter(assets_raw__icontains=f["asset_contains"])

    sort = f.get("sort") or "yield"
    desc = f.get("sort_dir", "desc") != "asc"
    ORDER = {"yield": "yield_rate", "ki": "ki", "loss_prob": "loss_prob", "sub_end": "sub_end"}

    def _early_1y(p):
        s = p.sim_result or {}
        return s.get("early_1y_pct") if s.get("early_1y_pct") is not None else s.get("early_redemp_pct")

    if sort in ORDER:
        field = ORDER[sort]
        qs = qs.exclude(**{f"{field}__isnull": True}).order_by(("-" if desc else "") + field)

    # 1년내 상환·배지는 DB 컬럼이 아니라(JSON/파이썬 속성) 파이썬에서 거른다
    need_py = f.get("early_1y_min") is not None or f.get("badge_only") or sort == "early_1y"
    results = list(qs[: limit * 5 if need_py else limit])

    if f.get("early_1y_min") is not None:
        results = [p for p in results
                   if _early_1y(p) is not None and _early_1y(p) >= f["early_1y_min"]]
    if f.get("badge_only"):
        results = [p for p in results if getattr(p, "radar", None) and p.radar.get("tier")]
    if sort == "early_1y":
        results = [p for p in results if _early_1y(p) is not None]
        results.sort(key=_early_1y, reverse=desc)

    return results[:limit], None
