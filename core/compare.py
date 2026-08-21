"""유사상품 비교 — 같은 주·같은 유형·같은 낙인 상품끼리 조건을 줄 세운다.

상품 하나만 보면 "이 조건이 좋은 건지" 알 수가 없다. 같은 주에 나온 200건대를
일일이 훑어야 비교가 되던 자리를, 세 축(제시 수익률·1차 배리어·마지막 배리어)의
백분위와 조건이 가장 가까운 5건으로 대신한다.

⚠ 세 축을 **반드시 함께** 보여준다. 수익률이 높은 상품은 1차 배리어도 높은
(불리한) 경향이 있어서, 한 축만 떼어 보여주면 좋은 조건으로 왜곡돼 읽힌다.

⚠ 저장하지 않는다. 비교 모수가 '그 주 청약분'이라 매주 바뀌므로 화면을 열 때
계산해야 맞다. 대신 모수 조회는 쿼리 한 번으로 끝낸다 (같은 주 200건대).
"""

from __future__ import annotations

import statistics
from datetime import timedelta

from core.market import resolve_ticker, split_assets

# ── 대상 건수 규칙 (2026-08 실측 분포 근거) ──────────────────
# 낙인 분포는 고르지 않다. 같은 주라도 낙인 25·30은 각 35건인데 낙인 15는 3건,
# 40은 3건까지 내려간다. 모수가 너무 작으면 '혼자 1등'이라 순위에 뜻이 없다.
MIN_PEERS = 3     # 이 미만이면 비교 버튼 자체를 숨긴다
LOW_SAMPLE = 10   # 이 미만이면 패널은 열되 참고용 문구를 띄운다
NEAREST_N = 5     # 결과 표에 싣는 '조건이 가장 가까운' 건수 (기준 상품 포함)


def _fmt(v):
    """게이지 눈금·표 표기용 숫자 문자열. 37.02 → '37.02', 21.5 → '21.5', 80 → '80'."""
    if v is None:
        return "-"
    return f"{float(v):g}"


def peer_key(product):
    """비교 모수를 가르는 키. 비교가 성립하지 않으면 None.

    같은 자산유형 + 낙인 **동일값**. ±범위가 아니다 — 낙인이 다르면 애초에
    다른 상품이라 수익률을 나란히 놓는 것 자체가 사과와 배 비교가 된다.
    노낙인끼리는 '낙인 조건이 없다'는 점이 같으므로 한 무리로 묶는다.
    낙인값도 노낙인 표시도 없는 상품(원금지급형 등)은 가를 기준이 없어 제외.
    """
    if not product.asset_type:
        return None
    if product.is_no_ki:
        return (product.asset_type, "noki")
    if product.ki is None:
        return None
    return (product.asset_type, int(product.ki))


def _row_key(asset_type, ki, is_no_ki):
    """peer_key와 같은 규칙을 values_list 행에 적용한 것 (모델 인스턴스 없이)."""
    if not asset_type:
        return None
    if is_no_ki:
        return (asset_type, "noki")
    if ki is None:
        return None
    return (asset_type, int(ki))


def week_of(d):
    """그 날짜가 속한 주의 (월요일, 일요일). 주간 청약 화면과 같은 주 경계."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def week_peer_counts(monday, sunday):
    """{peer_key: 건수} — 목록의 '비교 N' 버튼 라벨용.

    상품마다 세면 200번 넘게 왕복한다. 한 주 전체를 값만 한 번 긁어서 센다.
    """
    from core.models import Product

    rows = (Product.objects.listed()
            .filter(sub_end__gte=monday, sub_end__lte=sunday)
            .values_list("asset_type", "ki", "is_no_ki"))
    counts = {}
    for asset_type, ki, is_no_ki in rows:
        key = _row_key(asset_type, ki, is_no_ki)
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def asset_keys(assets_raw):
    """기초자산 문자열 → 비교용 키 집합.

    분해는 market.split_assets를 그대로 쓴다(회사명 안의 쉼표 처리가 이미 들어 있음).
    티커로 정규화해 '삼성전자'와 'Samsung Electronics'가 갈라지지 않게 한다.
    """
    return {resolve_ticker(a) or a for a in split_assets(assets_raw or "")}


# ── 지표 3종 ─────────────────────────────────────────────
# better: 어느 쪽이 유리한가. 수익률은 높을수록, 배리어는 둘 다 낮을수록 유리하다.
# 1차 배리어가 낮으면 첫 조기상환 문턱이 낮고, 마지막 배리어가 낮으면 만기에
# 넘어야 하는 선이 낮다.
METRICS = (
    {"field": "yield_rate", "label": "제시 수익률", "better": "high", "unit": "%"},
    {"field": "barrier_first", "label": "1차 조기상환 배리어", "better": "low", "unit": ""},
    {"field": "barrier_last", "label": "마지막 배리어", "better": "low", "unit": ""},
)


def _gauge(product, peers, spec, yield_side=None):
    """지표 하나의 게이지 데이터. 값이 없거나 모수가 1건뿐이면 None.

    ⚠ 막대 위치는 **실제 값의 위치**다 — (값 − 최저) / (최고 − 최저).
    라벨("낮은 쪽 상위 N%")에 끌려 막대를 옮기면 안 된다. 값이 중앙값보다 크면
    라벨이 무엇이든 막대는 오른쪽에 있어야 한다.
    """
    field = spec["field"]
    value = getattr(product, field)
    vals = [v for v in (getattr(p, field) for p in peers) if v is not None]
    if value is None or len(vals) < 2:
        return None

    n = len(vals)
    lo, hi = min(vals), max(vals)
    mid = statistics.median(vals)
    n_higher = sum(1 for v in vals if v > value)
    n_lower = sum(1 for v in vals if v < value)

    # 막대 위치 — 라벨과 완전히 분리해서 값 그대로 환산한다.
    pos = 50.0 if hi == lo else (value - lo) / (hi - lo) * 100
    pos = round(max(0.0, min(100.0, pos)), 1)

    # 어느 끝에 붙어 있는가. 위쪽이 더 가까우면 'high'(동수면 high).
    side = "low" if n_lower < n_higher else "high"
    rank = (n_higher if side == "high" else n_lower) + 1
    pct = round(rank / n * 100)

    if spec["better"] == "high":
        # 수익률은 높을수록 유리하다는 것이 자명해 방향어 없이 상위/하위로 쓴다.
        rank_label = f"상위 {pct}%" if side == "high" else f"하위 {pct}%"
    else:
        rank_label = f"높은 쪽 상위 {pct}%" if side == "high" else f"낮은 쪽 상위 {pct}%"

    # 색: 유리한 쪽이면 초록, 불리한 쪽이면 주황, 한가운데면 회색.
    fav_side = "high" if spec["better"] == "high" else "low"
    if n_higher == n_lower:
        tone = "flat"
    else:
        tone = "good" if side == fav_side else "warn"

    note = _note(spec, side, n, n_lower, n_higher, yield_side)

    return {
        "field": field, "label": spec["label"], "unit": spec["unit"],
        "value": value, "value_text": _fmt(value) + spec["unit"],
        "pos": pos, "pct": pct, "side": side, "tone": tone,
        "rank_label": rank_label, "note": note,
        "n": n,
        "lo_text": _fmt(lo) + spec["unit"],
        "mid_text": _fmt(mid) + spec["unit"],
        "hi_text": _fmt(hi) + spec["unit"],
    }


def _note(spec, side, n, n_lower, n_higher, yield_side):
    """게이지 아래 해석 문구. 지표의 방향(유리/불리)을 말로 한 번 더 못 박는다."""
    field = spec["field"]
    if field == "yield_rate":
        if side == "high":
            return f"{n}건 중 {n_lower}건보다 높습니다."
        return f"{n}건 중 {n_higher}건보다 낮습니다."

    if field == "barrier_first":
        if side == "high":
            head = "높을수록 첫 상환 문턱이 높습니다."
            # 수익률도 높은 쪽이면 맞바꾼 관계를 그대로 짚는다 — 한 축만 읽고
            # '조건이 좋다'로 넘어가는 것을 막는 자리다.
            if yield_side == "high":
                return f"{head} 수익률이 높은 대신 이쪽이 불리합니다."
            return f"{head} {n}건 중 {n_lower}건보다 높습니다."
        return f"낮을수록 첫 상환 문턱이 낮아 유리합니다 — {n}건 중 {n_higher}건보다 낮습니다."

    head = "만기까지 갔을 때 넘어야 하는 선입니다."
    if side == "high":
        return f"{head} 낮을수록 유리한데 이 상품은 높은 편입니다 — {n}건 중 {n_lower}건보다 높습니다."
    return f"{head} 낮을수록 유리한데 이 상품은 낮은 편입니다 — {n}건 중 {n_higher}건보다 낮습니다."


def _nearest(product, peers):
    """조건이 가장 가까운 NEAREST_N건 (기준 상품 포함, 거리 오름차순).

    거리 = 게이지에 띄운 세 축(수익률·1차 배리어·마지막 배리어)의 차이를
    **그 모수 안의 폭(최고−최저)으로 나눠** 더한 값. 폭으로 나누는 이유는
    단위가 달라서다 — 수익률은 20~40%대, 배리어는 50~90대라 그냥 더하면
    배리어 차이가 수익률 차이를 덮어버린다. 폭으로 나누면 세 축이 0~1로
    같은 자에 올라간다.

    값이 없는 축은 1.0(그 축에서 가장 먼 거리)으로 친다 — 결측을 0으로 두면
    정보가 없는 상품이 오히려 '가깝다'고 올라온다.
    모수의 폭이 0인 축(전원이 같은 값)은 변별력이 없으므로 0으로 둔다.
    """
    fields = [m["field"] for m in METRICS]
    spreads = {}
    for f in fields:
        vals = [v for v in (getattr(p, f) for p in peers) if v is not None]
        spreads[f] = (max(vals) - min(vals)) if vals else 0

    def distance(p):
        total = 0.0
        for f in fields:
            a, b = getattr(product, f), getattr(p, f)
            spread = spreads[f]
            if a is None or b is None:
                total += 1.0
            elif spread:
                total += abs(a - b) / spread
        return total

    def sort_key(p):
        dy = abs((product.yield_rate or 0) - (p.yield_rate or 0))
        return (distance(p), dy, p.id)

    picked = sorted(peers, key=sort_key)[:NEAREST_N]
    rows = []
    for p in picked:
        rows.append({
            "p": p,
            "is_target": p.id == product.id,
            "yield_text": _fmt(p.yield_rate) + "%" if p.yield_rate is not None else "-",
            "barrier_seq": "-".join(str(b) for b in (p.barriers_raw or [])) or "-",
            "period_text": p.period_display,
        })
    return rows


def compare_context(product, same_assets=False):
    """비교 패널 컨텍스트. 비교가 성립하지 않으면 None.

    성립 조건: 낙인·자산유형으로 모수를 가를 수 있고, 청약 마감일이 있고,
    같은 조건 상품이 MIN_PEERS건 이상일 것.
    """
    if peer_key(product) is None or not product.sub_end:
        return None

    from core.models import Product

    monday, sunday = week_of(product.sub_end)
    qs = (Product.objects.listed()
          .filter(sub_end__gte=monday, sub_end__lte=sunday,
                  asset_type=product.asset_type))
    if product.is_no_ki:
        qs = qs.filter(is_no_ki=True)
    else:
        qs = qs.filter(is_no_ki=False, ki=product.ki)
    peers = list(qs)                     # 쿼리 한 번 — 이후는 전부 메모리 계산
    total = len(peers)
    if total < MIN_PEERS:
        return None

    too_few = False
    if same_assets:
        base = asset_keys(product.assets_raw)
        peers = [p for p in peers if asset_keys(p.assets_raw) & base]
        # 토글을 켜서 3건 미만이 되면 줄 세우지 않는다 — 건수는 그대로 보여 주고
        # 토글을 끄면 몇 건과 비교되는지 알려 준다. 2건짜리 백분위는 50%/100%뿐이라
        # 순위라고 부를 수 없다.
        too_few = len(peers) < MIN_PEERS

    count = len(peers)
    if too_few:
        gauges, nearest = [], []
    else:
        yg = _gauge(product, peers, METRICS[0])
        yield_side = yg["side"] if yg else None
        gauges = [g for g in (
            yg,
            _gauge(product, peers, METRICS[1], yield_side),
            _gauge(product, peers, METRICS[2], yield_side),
        ) if g]
        nearest = _nearest(product, peers)

    return {
        "product": product,
        "count": count,
        "min_peers": MIN_PEERS,
        "total": total,               # 토글 끈 상태의 모수 (버튼 라벨과 같은 수)
        "same_assets": same_assets,
        "too_few_same_assets": too_few,
        "low_sample": not too_few and count < LOW_SAMPLE,
        "gauges": gauges,
        "nearest": nearest,
        "ki_label": "노낙인" if product.is_no_ki else f"낙인 {product.ki}",
        "monday": monday, "sunday": sunday,
    }
