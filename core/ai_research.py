"""상품 조건 필터 실행 — 필터(JSON)를 받아 기존 쿼리로 돌린다.

설계 원칙 (2026-07-28 확정, 그대로 유효):
- AI는 답을 만들지 않고 "필터"만 만든다. 결과는 항상 DB의 실제 데이터.
  → 환각 원천 차단, 숫자를 지어낼 수 없음.
- 자유 문답("이 상품 어때?")은 지원하지 않는다. 조회·정렬·필터 전용.
  → 투자권유 소지 차단. 레이더와 같은 "시스템 분류"의 법적 성격 유지.

⚠ 2026-08-07: 자연어 → 필터 변환(ask())은 여기서 폐기했다. /search/의 AI
  검색 입구를 없애고 /ask/ 한 곳으로 모았기 때문이다. 같은 일을 하는 입구가
  둘이면 어느 쪽이 최신인지 아무도 모르게 된다.
  변환은 이제 core/ask_agent.py의 해석턴이 하고, 이 모듈은 그 결과를
  **실행**하는 쪽만 맡는다. run_filter / describe 는 /ask/의 product_filter
  도구가 그대로 쓴다 — 검색 결과가 화면과 답변에서 갈라지지 않게 하는 지점이다.
"""
import logging

log = logging.getLogger(__name__)

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
        # 검색 화면의 AI 검색도 목록이므로 같은 규칙을 태운다.
        # 위 my_portfolio 분기는 본인이 등록한 보유 상품이라 거르지 않는다.
        qs = Product.objects.listed()
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
