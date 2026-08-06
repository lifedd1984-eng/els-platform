"""포트폴리오 사실 계산 — 화면(/portfolio/)과 AI 분석 질문이 **같은 함수**를 쓴다.

왜 뷰에서 뜯어냈나
  화면과 답변이 다른 숫자를 내면 그 순간 둘 다 못 믿게 된다. 집중도·스트레스
  테스트는 원래 views.portfolio 안에 인라인으로 있었고, /ask/ 도구가 같은 계산을
  다시 구현하면 두 벌이 조용히 갈라진다. 계산은 여기 한 곳에만 둔다.

⚠ analyze_risk / stress_test 는 뷰에서 **한 글자도 바꾸지 않고** 옮긴 코드다.
  화면 출력이 바뀌면 안 되므로, 고칠 일이 생기면 /portfolio/ 렌더 결과를
  추출 전후로 대조한 뒤에 고칠 것.
"""

import logging
import re
from collections import defaultdict
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# 스트레스 테스트 상수 — 값은 뷰에 있던 그대로.
SHOCKS = (40, 50, 60, 70)
KI_LOSS_ASSUMED = 55   # 낙인 확정 시 원금 손실률 가정(%)

# 자산/발행사/주차 집중도 표에 실을 최대 행 수 (뷰의 슬라이스와 동일)
TOP_ASSETS = 6
TOP_ISSUERS = 5
TOP_WEEKS = 6

# 스트레스 테스트에 자산이 올라오는 최소 노출 비중(%)
STRESS_MIN_PCT = 3


def _split_assets(assets_raw: str):
    """'KOSPI200 , SK하이닉스' → ['KOSPI200', 'SK하이닉스'].

    ⚠ market.split_assets 와 다르다 — 이쪽은 회사명 안의 쉼표를 재결합하지
      않는다. 집중도 표의 기존 출력이 이 동작에 기대고 있어 그대로 둔다.
    """
    return [a.strip() for a in re.split(r"[,/]+", assets_raw or "") if a.strip()]


def analyze_risk(holding, total_invested):
    """보유 포트폴리오의 집중도/분산 리스크 분석.

    ELS는 워스트오브 구조 → 각 기초자산에 투자금 전액이 노출된다.
    같은 자산에 여러 건 몰리면 그 자산 하나로 전체가 위험해진다.
    """
    if not holding or not total_invested:
        return None

    def _bucket():
        return {"amount": 0, "count": 0, "items": []}

    asset_exposure = defaultdict(_bucket)
    issuer_exposure = defaultdict(_bucket)
    week_exposure = defaultdict(_bucket)     # 청약 주차(빈티지)
    ki_exposure = defaultdict(_bucket)       # 종목형 낙인별
    maturity_buckets = defaultdict(int)  # 'YYYY-MM' → 건수

    for inv in holding:
        p = inv.product
        item = {"pid": p.id, "label": f"{p.issuer} {p.product_no}", "amount": inv.amount,
                "ki": "없음" if p.is_no_ki else (f"{p.ki:g}" if p.ki is not None else "-"),
                "issued": p.issued_on}
        for asset in _split_assets(p.assets_raw):
            b = asset_exposure[asset]
            b["amount"] += inv.amount
            b["count"] += 1
            b["items"].append(item)
        b = issuer_exposure[p.issuer]
        b["amount"] += inv.amount
        b["count"] += 1
        b["items"].append(item)
        # 청약 주차 = 마감일이 속한 주의 월요일. 같은 주 발행분은 같은 빈티지로,
        # 시장 급변 시 함께 무너질 수 있다 (2021 HSCEI·2023 LG화학 사례).
        wd = p.sub_end or p.issue_date
        if wd:
            monday = wd - timedelta(days=wd.weekday())
            b = week_exposure[monday]
            b["amount"] += inv.amount
            b["count"] += 1
            b["items"].append(item)
        # 종목형 낙인별 — 낙인이 얕을수록(숫자 클수록) 방어선이 약하다
        if p.asset_type == "종목형":
            kkey = "노낙인" if p.is_no_ki else (f"낙인 {p.ki:g}" if p.ki is not None else "낙인 미상")
            b = ki_exposure[kkey]
            b["amount"] += inv.amount
            b["count"] += 1
            b["items"].append(item)
        nxt = inv.next_evaluation
        if nxt:
            maturity_buckets[nxt["date"].strftime("%Y-%m")] += 1

    def _row(name, v):
        return {"name": name, "amount": v["amount"], "count": v["count"],
                "pct": round(v["amount"] / total_invested * 100),
                "items": sorted(v["items"], key=lambda i: -i["amount"])}

    def _top(exposure):
        return sorted((_row(k, v) for k, v in exposure.items()),
                      key=lambda r: -r["amount"])

    assets = _top(asset_exposure)
    issuers = _top(issuer_exposure)
    weeks = [_row(f"{k.month}.{k.day}~{(k + timedelta(days=6)).month}.{(k + timedelta(days=6)).day}", v)
             for k, v in sorted(week_exposure.items(), key=lambda kv: -kv[1]["amount"])]

    # 낙인 낮은(깊은) 순 정렬, 노낙인·미상은 뒤로
    def _ki_order(name):
        if name.startswith("낙인 ") and name != "낙인 미상":
            return (0, float(name.split()[1]))
        return (1, 999)
    ki_types = sorted((_row(k, v) for k, v in ki_exposure.items()),
                      key=lambda r: _ki_order(r["name"]))

    # 경고: 단일 자산/발행사 노출이 전체의 50% 초과
    warnings = []
    if assets and assets[0]["pct"] > 50:
        warnings.append(
            f"기초자산 '{assets[0]['name']}'에 전체의 {assets[0]['pct']}%가 집중되어 있습니다."
        )
    if issuers and issuers[0]["pct"] > 60:
        warnings.append(
            f"발행사 '{issuers[0]['name']}'에 전체의 {issuers[0]['pct']}%가 집중되어 있습니다."
        )
    # 청약 주차 집중: 같은 주 상품은 같은 빈티지 — 한 주에 50% 초과면 경고
    if weeks and weeks[0]["pct"] > 50 and len(holding) >= 3:
        warnings.append(
            f"청약 주차 {weeks[0]['name']}에 전체의 {weeks[0]['pct']}%가 집중되어 있습니다. "
            f"같은 주 상품은 시장 급변 시 함께 흔들립니다."
        )
    # 만기 집중: 한 달에 60% 초과 평가 몰림
    if maturity_buckets:
        top_month, top_cnt = max(maturity_buckets.items(), key=lambda x: x[1])
        if top_cnt / len(holding) > 0.6 and len(holding) >= 3:
            warnings.append(f"{top_month} 평가일에 상환이 몰려 있습니다 ({top_cnt}건).")

    return {
        "assets": assets[:TOP_ASSETS],
        "issuers": issuers[:TOP_ISSUERS],
        "weeks": weeks[:TOP_WEEKS],
        "week_total": len(weeks),
        "ki_types": ki_types,
        "maturity": sorted(maturity_buckets.items()),
        "warnings": warnings,
    }


def stress_test(holding, total_invested):
    """자산별 추가 하락 시나리오 → 낙인 진입액·예상 손실.

    위험 단위는 '노출 %'가 아니라 '낙인 발동가 분포'다. 같은 자산이라도
    발행 시기(기준가)가 다르면 발동가가 달라 함께 무너지지 않는다 — 총노출만
    보면 과대평가라는 태훈님 지적(2026-08-01)을 반영한 지표.
    """
    if not (holding and total_invested):
        return None

    from collections import defaultdict as _dd

    from core import market as _mkt_s
    from core.models import KnockInStatus

    # 자산 키는 티커로 정규화 — 'Micron'과 'Micron Technology'가 표기만 다른
    # 같은 자산으로 분리되면 3% 게이트에 걸려 통째로 누락된다 (레드팀 B [상3])
    def _akey(name):
        n = (name or "").strip()
        return _mkt_s.resolve_ticker(n) or n

    _sa = _dd(lambda: {"name": None, "cur": None, "cur_at": None, "trigs": [], "amt": 0})
    _inv_assets = _dd(list)   # inv_id → [(asset_key, trigger|None)]
    _inv_amt = {}
    _covered = set()
    for _s in KnockInStatus.objects.filter(
            investment__in=holding).select_related("investment__product"):
        _inv = _s.investment
        if _s.ref_price is None or _s.current_price is None:
            continue
        _p = _inv.product
        _k = _akey(_s.asset_name)
        _a = _sa[_k]
        if _a["name"] is None:
            _a["name"] = _mkt_s.shorten_asset_display(_s.asset_name.strip())
        # 티커로 합친 버킷에는 표기가 다른 행이 섞인다('Micron'/'Micron Technology').
        # 그냥 덮어쓰면 마지막에 읽힌 행의 현재가가 자산 전체의 기준이 돼,
        # 배치가 갱신하다 만 옛 값이 여유·손실 계산을 통째로 흔들 수 있다.
        # 갱신 시각이 가장 최신인 행의 값을 채택한다.
        if _a["cur"] is not None and _a["cur"] != _s.current_price:
            logger.info("스트레스 테스트 현재가 불일치 자산=%s: %s(%s) vs %s(%s) — 최신값 채택",
                        _k, _a["cur"], _a["cur_at"], _s.current_price, _s.updated_at)
        if _a["cur"] is None or (_s.updated_at is not None
                                 and (_a["cur_at"] is None or _s.updated_at > _a["cur_at"])):
            _a["cur"] = _s.current_price
            _a["cur_at"] = _s.updated_at
        _covered.add(_inv.id)
        _inv_amt[_inv.id] = _inv.amount
        trig = None
        if not (_p.is_no_ki or _p.ki is None):
            trig = _s.ref_price * _p.ki / 100.0
            _a["trigs"].append((trig, _inv.amount))
        _inv_assets[_inv.id].append((_k, trig))
    # 노출은 투자 단위로 (같은 투자가 한 자산에 두 번 잡히지 않게)
    for _iid, _al in _inv_assets.items():
        for _k in {k for k, _ in _al}:
            _sa[_k]["amt"] += _inv_amt[_iid]

    rows = []
    _shown = set()   # 3% 게이트를 통과해 실제로 표에 그려지는 자산 키
    for _k, _a in _sa.items():
        pct = _a["amt"] / total_invested * 100
        if pct < STRESS_MIN_PCT or not _a["cur"] or not _a["trigs"]:
            continue
        _shown.add(_k)
        needs = [(1 - trig / _a["cur"]) * 100 for trig, _ in _a["trigs"]]
        amts = [amt for _, amt in _a["trigs"]]
        # 금액가중 평균 여유 — 대표값 (min은 가장 취약한 1건이라 대표성이 없다)
        wavg = sum(n * m for n, m in zip(needs, amts)) / sum(amts)
        losses = {}
        for d in SHOCKS:
            px = _a["cur"] * (1 - d / 100)
            hit = sum(amt for trig, amt in _a["trigs"] if px < trig)
            losses[d] = round(hit / total_invested * 100 * KI_LOSS_ASSUMED / 100, 2)
        rows.append({"name": _a["name"] or _k, "pct": round(pct, 1),
                     "wavg": round(wavg, 1), "nearest": round(min(needs), 1),
                     "losses": losses})
    if not rows:
        return None

    # 합계는 투자 단위 union — 워스트오브 상품은 어느 자산이든 발동가를
    # 깨지면 낙인 1번이다. 자산 행 합산은 다중자산 투자를 중복 계상해
    # 이론상 상한(55%)을 넘는 값까지 만들었다 (레드팀 B [상1·상2])
    # union 범위는 표에 그려진 자산(_shown)으로 제한한다. 3% 게이트에 걸려
    # 행이 없는 자산까지 세면 <tfoot> '합계'가 바로 위 행들과 안 맞아,
    # 같은 표 안에서 셈이 어긋난 것처럼 보인다.
    _cur = {k: _sa[k]["cur"] for k in _sa}
    tot = {}
    for d in SHOCKS:
        knocked = 0
        for _iid, _al in _inv_assets.items():
            if any(k in _shown and t is not None and _cur.get(k)
                   and _cur[k] * (1 - d / 100) < t for k, t in _al):
                knocked += _inv_amt[_iid]
        tot[d] = round(knocked / total_invested * 100 * KI_LOSS_ASSUMED / 100, 2)
    # '몰아 샀다면'(-60%): 전액을 각 자산의 가장 취약한 기준가에 샀다고 가정.
    # 자산 단위 _worst_trig만 보면 안 되고 투자별 발동가 t도 함께 봐야 한다 —
    # t가 None인 노낙인 투자는 같은 기초자산의 낙인 상품이 아무리 위험해도
    # 절대 낙인되지 않는데, 이를 빼먹으면 그 투자금까지 conc60에 얹혀
    # '시기를 나눠 산 덕분에 N%p를 막았습니다'가 부풀려진다.
    _worst_trig = {k: max((t for t, _ in _sa[k]["trigs"]), default=None)
                   for k in _shown}
    conc_knocked = 0
    for _iid, _al in _inv_assets.items():
        if any(t is not None and _worst_trig.get(k) is not None and _cur.get(k)
               and _cur[k] * 0.40 < _worst_trig[k] for k, t in _al):
            conc_knocked += _inv_amt[_iid]
    conc = round(conc_knocked / total_invested * 100 * KI_LOSS_ASSUMED / 100, 2)
    rows.sort(key=lambda r: -r["losses"][60])
    _miss = [i for i in holding if i.id not in _covered]
    return {
        "rows": rows,
        "shocks": list(SHOCKS),
        "loss_assumed": KI_LOSS_ASSUMED,
        "total": tot,
        "conc60": conc,
        "saved60": round(conc - tot[60], 2),
        "worst": rows[0],
        "missing_n": len(_miss),
        "missing_amt": sum(i.amount for i in _miss),
    }


def holding_by_type(holding):
    """유형별(종목형/지수형/기타) 보유 분해 — 건수·투자금액."""
    out = {
        "종목형": {"count": 0, "amount": 0},
        "지수형": {"count": 0, "amount": 0},
        "기타": {"count": 0, "amount": 0},
    }
    for i in holding:
        t = i.product.asset_type if i.product.asset_type in ("종목형", "지수형") else "기타"
        out[t]["count"] += 1
        out[t]["amount"] += i.amount
    return out


# ══════════════════════════════════════════════════════════════════
# AI 분석 질문(/ask/)용 — 위 함수를 그대로 불러 표시용 문자열만 덧붙인다
# ══════════════════════════════════════════════════════════════════

VIEWS = ("summary", "by_asset_type", "concentration_asset", "concentration_issuer",
         "concentration_week", "ki_buffer", "next_eval", "stress_test",
         "maturity_schedule")


def won(n):
    """금액 → 사람이 읽는 문자열. 1,000만원 / 20억 1,774만원."""
    if n is None:
        return None
    neg = "-" if n < 0 else ""
    n = abs(int(n))
    if n >= 100_000_000:
        eok, rest = divmod(n, 100_000_000)
        man = rest // 10_000
        return f"{neg}{eok:,}억 {man:,}만원" if man else f"{neg}{eok:,}억원"
    if n >= 10_000:
        return f"{neg}{n // 10_000:,}만원"
    return f"{neg}{n:,}원"


def num(value, display):
    """도구 반환값의 표준 모양 — 원본값과 표시 문자열을 함께 준다.

    답변 사후검사가 '모델이 쓴 수치가 도구가 준 display 집합 안에 있는가'를
    대조하므로, 표시 문자열은 반드시 도구가 만들어야 한다.
    """
    return None if value is None else {"value": value, "display": display}


def _pct(v, digits=1):
    return num(v, None if v is None else f"{v:.{digits}f}%")


def _pp(v, digits=1):
    return num(v, None if v is None else f"{v:.{digits}f}%p")


def _amt(v):
    return num(v, won(v))


def facts(user, views, sort=None, within_days=None, limit=None):
    """/ask/ portfolio_facts 도구 본체. 화면과 같은 함수를 불러 쓴다.

    반환은 요청한 view 키만 담는다 — 안 물어본 걸 얹으면 토큰만 먹는다.
    """
    from .models import Investment

    limit = max(1, min(int(limit or 10), 30))
    wanted = [v for v in (views or []) if v in VIEWS] or ["summary"]

    invs = (Investment.objects.filter(user=user, status="보유중")
            .select_related("product").prefetch_related("ki_status"))
    holding = list(invs)
    total = sum(i.amount for i in holding)
    out = {"as_of": date.today().isoformat()}

    if not holding:
        out["reason"] = "NO_PORTFOLIO"
        out["note"] = "보유 중으로 등록된 투자가 없습니다."
        return out

    risk = None

    def _risk():
        nonlocal risk
        if risk is None:
            risk = analyze_risk(holding, total) or {}
        return risk

    def _conc(rows):
        return [{"name": r["name"], "n": r["count"],
                 "pct": num(r["pct"], f"{r['pct']}%"), "amount": _amt(r["amount"])}
                for r in rows[:limit]]

    if "summary" in wanted:
        by = holding_by_type(holding)
        out["summary"] = {
            "count": len(holding),
            "total": _amt(total),
            "by_type": {k: {"n": v["count"], "amount": _amt(v["amount"])}
                        for k, v in by.items() if v["count"]},
        }
    if "by_asset_type" in wanted:
        by = holding_by_type(holding)
        out["by_asset_type"] = [
            {"type": k, "n": v["count"], "amount": _amt(v["amount"]),
             "pct": num(round(v["amount"] / total * 100, 1),
                        f"{v['amount'] / total * 100:.1f}%")}
            for k, v in by.items() if v["count"]
        ]
    if "concentration_asset" in wanted:
        out["concentration_asset"] = _conc(_risk().get("assets", []))
        out["concentration_note"] = "워스트오브 구조라 자산별 노출 합은 100%를 넘습니다."
    if "concentration_issuer" in wanted:
        out["concentration_issuer"] = _conc(_risk().get("issuers", []))
    if "concentration_week" in wanted:
        out["concentration_week"] = _conc(_risk().get("weeks", []))
        out["week_total"] = _risk().get("week_total")
    if "maturity_schedule" in wanted:
        out["maturity_schedule"] = [{"month": m, "n": n}
                                    for m, n in _risk().get("maturity", [])][:limit]

    if "ki_buffer" in wanted:
        rows = []
        for inv in holding:
            w = inv.worst_ki_status
            if w is None or w.level_pct is None:
                continue
            p = inv.product
            ki = None if p.is_no_ki else p.ki
            buf = inv.ki_buffer
            drop = None if not ki else round((ki / w.level_pct - 1) * 100, 1)
            rows.append({
                "asset": w.asset_name,
                "product": f"{p.issuer} {p.product_no}",
                "level": _pct(round(w.level_pct, 1)),
                "ki": "노낙인" if p.is_no_ki else (f"{ki:g}" if ki is not None else "미상"),
                "buffer": _pp(buf) if buf is not None else None,
                "drop_needed": _pct(drop) if drop is not None else None,
                "amount": _amt(inv.amount),
                "_sort_buffer": buf if buf is not None else 9999,
            })
        rows.sort(key=lambda r: r["_sort_buffer"])
        for r in rows:
            r.pop("_sort_buffer")
        out["ki_buffer"] = rows[:limit]
        if not rows:
            out["ki_buffer_reason"] = "NO_PRICE_DATA"

    if "next_eval" in wanted:
        edge = date.today() + timedelta(days=int(within_days)) if within_days else None
        rows = []
        for inv in holding:
            nxt = inv.next_evaluation
            if not nxt:
                continue
            if edge and nxt["date"] > edge:
                continue
            p = inv.product
            rows.append({
                "date": nxt["date"].isoformat(),
                "product": f"{p.issuer} {p.product_no}",
                "n": nxt["n"],
                "barrier": num(nxt["barrier"], f"{nxt['barrier']}%"),
                "amount": _amt(inv.amount),
                "days_left": (nxt["date"] - date.today()).days,
            })
        rows.sort(key=lambda r: r["date"])
        out["next_eval"] = rows[:limit]

    if "stress_test" in wanted:
        st = stress_test(holding, total)
        if st is None:
            out["stress_test_reason"] = "NO_PRICE_DATA"
        else:
            out["stress_test"] = {
                "shocks": st["shocks"],
                "loss_assumed": num(st["loss_assumed"], f"{st['loss_assumed']}%"),
                "rows": [{"asset": r["name"], "exposure": _pct(r["pct"]),
                          "avg_buffer": _pct(r["wavg"]), "nearest": _pct(r["nearest"]),
                          "loss": {str(d): _pct(r["losses"][d], 2) for d in st["shocks"]}}
                         for r in st["rows"][:limit]],
                "total": {str(d): _pct(st["total"][d], 2) for d in st["shocks"]},
                "missing_n": st["missing_n"],
            }

    # 정렬 힌트는 ki_buffer/next_eval 에만 의미가 있다 (그 외는 이미 금액순)
    if sort == "eval_date" and out.get("next_eval"):
        out["next_eval"].sort(key=lambda r: r["date"])
    elif sort == "amount":
        for key in ("ki_buffer", "next_eval"):
            if out.get(key):
                out[key].sort(key=lambda r: -(r["amount"]["value"] if r.get("amount") else 0))
    return out
