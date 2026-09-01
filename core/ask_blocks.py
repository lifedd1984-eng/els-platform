"""도구 결과 → 화면 블록. 템플릿은 이 구조만 보고 그린다.

블록은 전부 {"type": ...} 로 판별한다. 템플릿이 새 타입을 모르면 그냥
건너뛰면 되므로, 블록을 추가해도 화면이 깨지지 않는다.

숫자는 여기서 새로 계산하지 않는다 — ask_tools가 만든 display 문자열을
옮겨 담기만 한다. 그래야 답변 문장·표·근거의 숫자가 서로 어긋나지 않는다.
"""

import math

from . import ask_tools


def _nice(v):
    """축 라벨용 '보기 좋은 수' — 상위 2자리만 남기고 내림."""
    if v <= 0:
        return v
    e = math.floor(math.log10(v)) - 1
    step = 10.0 ** e
    return round(round(v / step) * step, max(0, -e))


# 차트 좌표계 (목업과 동일)
VIEWBOX = "0 0 720 244"
X0, X1 = 46, 706
Y_TOP, Y_BOTTOM = 20, 212


def _d(v):
    """{"value","display"} → display. None 안전."""
    return v["display"] if isinstance(v, dict) and "display" in v else (v if v else None)


def _dkey(iso):
    """'2024-06-18' → 정렬용 정수. 날짜 파싱 없이 가장 가까운 포인트를 찾는 용도."""
    return int(iso.replace("-", ""))


def _cell(text, tone=None, bold=False):
    return {"text": "" if text is None else str(text), "tone": tone, "bold": bold}


def _mobile(rows, label_i, value_i, sub_fn=None):
    """표의 모바일 대체 리스트. 800px 아래에서 표 대신 그린다.

    가로 스크롤로도 동작은 하지만, 좁은 화면에서 7열짜리 표는 사실상 못 읽는다.
    """
    out = []
    for r in rows:
        if len(r) <= max(label_i, value_i):
            continue
        out.append({"label": r[label_i]["text"], "value": r[value_i]["text"],
                    "tone": r[value_i]["tone"], "sub": sub_fn(r) if sub_fn else None})
    return out


def _tone_by_sign(v):
    if not isinstance(v, dict) or v.get("value") is None:
        return None
    return "blue" if v["value"] > 0 else ("red" if v["value"] < 0 else None)


def _section(title, icon):
    return {"type": "section", "title": title, "icon": icon}


def _note(lines, tone="info"):
    return {"type": "note", "tone": tone, "lines": [ln for ln in lines if ln]}


def _external_blocks(res):
    sources = res.get("sources") or []
    if not sources:
        return []
    return [{"type": "sources", "title": "외부 검색 출처", "items": sources}]


# ══════════════════════════════════════════════════════════════════
# 차트 — 로그 눈금 라인차트. SVG 좌표를 서버가 계산해 내려준다.
# ══════════════════════════════════════════════════════════════════

def _chart(res):
    pts = res.get("points") or []
    if len(pts) < 2:
        return None
    vals = [p["v"] for p in pts if p["v"] and p["v"] > 0]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    llo, lhi = math.log10(lo), math.log10(hi)
    if lhi - llo < 0.05:          # 거의 평평하면 눈금을 조금 벌린다
        llo, lhi = llo - 0.05, lhi + 0.05
    pad = (lhi - llo) * 0.08
    llo, lhi = llo - pad, lhi + pad

    def y_of(v):
        return round(Y_BOTTOM - (math.log10(v) - llo) / (lhi - llo) * (Y_BOTTOM - Y_TOP), 1)

    n = len(pts)
    step = (X1 - X0) / (n - 1)
    xy = [(round(X0 + step * i, 1), y_of(p["v"])) for i, p in enumerate(pts) if p["v"] > 0]

    ticker = (res.get("meta") or {}).get("ticker", "")

    def _label(v):
        d = ask_tools.price(ticker, v)
        return d["display"] if d else f"{v:,.0f}"

    # y축: 10의 거듭제곱 눈금 중 구간 안에 드는 것만.
    # ⚠ 한 데케이드를 못 채우는 계열(1년치 국내 종목 등)은 거듭제곱 눈금이
    #   하나도 안 걸려 축 라벨이 통째로 사라진다. 그럴 땐 구간을 3등분한
    #   '보기 좋은 수'로 대체한다.
    ticks = []
    for e in range(math.floor(llo), math.ceil(lhi) + 1):
        v = 10.0 ** e
        y = y_of(v)
        if Y_TOP <= y <= Y_BOTTOM:
            ticks.append({"y": y, "label": _label(v),
                          "solid": abs(y - Y_BOTTOM) < 1.5})
    if len(ticks) < 2:
        ticks = []
        for frac in (0.05, 0.5, 0.95):
            v = _nice(10.0 ** (llo + (lhi - llo) * frac))
            y = y_of(v)
            if Y_TOP <= y <= Y_BOTTOM and all(abs(t["y"] - y) > 8 for t in ticks):
                ticks.append({"y": y, "label": _label(v),
                              "solid": abs(y - Y_BOTTOM) < 12})
        ticks.sort(key=lambda t: -t["y"])
    # x축: 연도 라벨 (2년 간격, 최대 6개)
    years = sorted({p["d"][:4] for p in pts})
    stride = max(1, len(years) // 6 + (1 if len(years) % 6 else 0))
    x_ticks = []
    for i, p in enumerate(pts):
        y4 = p["d"][:4]
        if y4 in years[::stride] and all(t["label"] != y4 for t in x_ticks):
            x_ticks.append({"x": round(X0 + step * i, 1), "label": y4})

    # 최대낙폭 구간 음영 + 고점/저점 마커.
    # 좌표는 일별 기준이라 월말 폴리라인 위에 정확히 얹히지 않는다 — 의도한 것이다.
    shade, markers = None, []
    mm = res.get("mdd_marks") or {}
    if mm.get("peak") and mm.get("trough"):
        dates = [p["d"] for p in pts]

        def x_of_date(ds):
            i = min(range(len(dates)), key=lambda k: abs(_dkey(dates[k]) - _dkey(ds)))
            return round(X0 + step * i, 1)

        px_, tx = x_of_date(mm["peak"]["d"]), x_of_date(mm["trough"]["d"])
        # 고점·저점이 같은 축약 구간에 들어가면 폭이 0이 된다. 낙폭은 실재하므로
        # 최소 폭을 줘서 음영이 사라지지 않게 한다 (짧고 깊은 하락이 그렇다).
        w = max(round(tx - px_, 1), 4.0)
        shade = {"x": px_, "y": Y_TOP, "w": min(w, X1 - px_), "h": Y_BOTTOM - Y_TOP}
        for kind, key, anchor in (("peak", "고점", "middle"), ("trough", "저점", "start")):
            v = mm[kind]["v"]
            if v and v > 0:
                markers.append({
                    "kind": kind, "cx": x_of_date(mm[kind]["d"]), "cy": y_of(v),
                    "label": (f"{key} {_d(mm[kind]['px'])}"
                              + (f" ({_d(mm['mdd'])})" if kind == "trough" else "")),
                    "anchor": anchor, "filled": kind == "trough",
                })

    return {
        "type": "chart", "viewbox": VIEWBOX,
        "points": " ".join(f"{x},{y}" for x, y in xy),
        "y_ticks": ticks, "x_ticks": x_ticks,
        "shade": shade, "markers": markers,
        "y_bottom": Y_BOTTOM, "y_top": Y_TOP, "x0": X0, "x1": X1,
        "legend": [
            {"color": "var(--blue)",
             "label": "{} {}".format(res.get("asset"),
                                     "미조정 종가" if res["meta"]["price_kind"] == "raw"
                                     else "조정종가")},
            {"color": None,
             "label": "로그 눈금 — 상승폭이 커서 선형 눈금이면 초반 구간이 보이지 않습니다"},
        ],
        "last_label": _d(res.get("last_px")),
        "last_x": xy[-1][0], "last_y": xy[-1][1],
        "freq_note": {"daily": "일별 종가", "weekly": "주말 종가",
                      "monthly": "월말 종가"}.get(res.get("freq"), "종가"),
        "n_points": len(xy),
    }


# ══════════════════════════════════════════════════════════════════
# metric_calc
# ══════════════════════════════════════════════════════════════════

_STAT_LABEL = {
    "cumulative_return": "누적수익률", "period_return": "구간수익률",
    "cagr": "연평균 (CAGR)", "mdd": "최대낙폭 (MDD)", "volatility": "연율 변동성",
}


def _metric_blocks(res):
    out = []
    m = res.get("metrics") or {}
    cards = []
    for key, label in _STAT_LABEL.items():
        v = m.get(key)
        if not v:
            continue
        tone = "red" if key == "mdd" else ("blue" if key != "volatility" else None)
        if key in ("cumulative_return", "period_return", "cagr"):
            tone = _tone_by_sign(v)
        cards.append({"label": label, "display": _d(v), "tone": tone})
    if cards:
        out.append({"type": "stats", "cards": cards[:4]})

    det = m.get("mdd_detail")
    if det:
        lines = [f"고점 {det['peak']} {_d(det['peak_px'])} → 저점 {det['trough']} "
                 f"{_d(det['trough_px'])} ({_d(m.get('mdd'))})."]
        lines.append(f"고점 회복 {det['recovered']}." if det.get("recovered")
                     else det.get("recovered_note"))
        if res["meta"]["price_kind"] != "raw":
            lines.append("낙폭은 조정종가 기준이라 배당·분할이 반영된 값이며, "
                         "ELS 낙인 판정에 쓰는 미조정 종가와는 계열이 다릅니다.")
        out.append(_section("최대낙폭 상세", "fa-arrow-trend-down"))
        out.append(_note(lines, tone="warn"))

    for key, title, icon in (("worst_day", "하루 최대 하락", "fa-arrow-down"),
                             ("best_day", "하루 최대 상승", "fa-arrow-up")):
        if m.get(key):
            out.append(_note([f"{title}: {m[key]['date']} {_d(m[key]['ret'])}"]))
    if m.get("level_vs_high"):
        lv = m["level_vs_high"]
        out.append(_note([f"구간 고점 {lv['high_date']} {_d(lv['high'])} 대비 현재 "
                          f"{_d(lv['level'])} ({_d(lv['gap'])})."]))
    if m.get("days_below"):
        db = m["days_below"]
        out.append(_note([
            f"기준가 {db['base_date']} {_d(db['base_px'])}의 {_d(db['threshold'])} 아래로 "
            f"내려간 거래일 {_d(db['days'])}"
            + (f", 최초 {db['first_hit']}." if db.get("first_hit") else "은 없습니다."),
            f"구간 최저 레벨 {_d(db['lowest_level'])}." if db.get("lowest_level") else None]))

    by = res.get("by_bucket")
    if by:
        label = {"year": "연도", "quarter": "분기", "month": "월"}.get(res.get("group_by"), "구간")
        out.append(_section(f"{label}별 수익률과 구간 최대낙폭", "fa-table-list"))
        rows = [[_cell(r["bucket"]),
                 _cell(_d(r["ret"]), _tone_by_sign(r["ret"]), bold=True),
                 _cell(_d(r["mdd"]), "red"),
                 _cell(_d(r["close"])),
                 _cell("부분 구간" if r.get("partial") else "")]
                for r in by]
        out.append({
            "type": "table",
            "columns": [{"key": "b", "label": label, "num": False},
                        {"key": "ret", "label": f"{label} 수익률", "num": True},
                        {"key": "mdd", "label": f"{label}중 최대낙폭", "num": True},
                        {"key": "close", "label": "구간말 종가", "num": True},
                        {"key": "note", "label": "비고", "num": False}],
            "rows": rows,
            "mobile": _mobile(rows, 0, 1,
                              lambda r: f"{label}중 {r[2]['text']}"),
        })
    return out


# ══════════════════════════════════════════════════════════════════
# portfolio_facts
# ══════════════════════════════════════════════════════════════════

def _portfolio_blocks(res):
    out = []
    s = res.get("summary")
    if s:
        cards = [{"label": "보유 건수", "display": f"{s['count']:,}건", "tone": None},
                 {"label": "투자원금 합계", "display": _d(s["total"]), "tone": None}]
        for t, v in (s.get("by_type") or {}).items():
            cards.append({"label": f"{t} {v['n']}건", "display": _d(v["amount"]), "tone": None})
        out.append({"type": "stats", "cards": cards[:4]})

    for key, title, icon in (("concentration_asset", "기초자산별 비중", "fa-layer-group"),
                             ("concentration_issuer", "발행사별 비중", "fa-building-columns"),
                             ("concentration_week", "청약 주차별 비중", "fa-calendar-week")):
        rows = res.get(key)
        if not rows:
            continue
        out.append(_section(title, icon))
        trows = [[_cell(r["name"]), _cell(_d(r["pct"]), bold=True),
                  _cell(f"{r['n']:,}건"), _cell(_d(r["amount"]))] for r in rows]
        out.append({
            "type": "table",
            "columns": [{"key": "n", "label": "구분", "num": False},
                        {"key": "pct", "label": "비중", "num": True},
                        {"key": "cnt", "label": "건수", "num": True},
                        {"key": "amt", "label": "투자금", "num": True}],
            "rows": trows,
            "mobile": _mobile(trows, 0, 1,
                              lambda r: f"{r[2]['text']} · {r[3]['text']}"),
        })
        if key == "concentration_asset" and res.get("concentration_note"):
            out.append(_note([res["concentration_note"]]))

    kb = res.get("ki_buffer")
    if kb:
        out.append(_section("낙인까지 여유 (적은 순)", "fa-shield-halved"))
        krows = [[_cell(r["product"]), _cell(r["asset"]), _cell(_d(r["level"])),
                  _cell(r["ki"]),
                  _cell(_d(r["buffer"]), "red" if _alert(r) else None, bold=True),
                  _cell(_d(r["drop_needed"]), "red" if _alert(r) else None),
                  _cell(_d(r["amount"]))] for r in kb]
        out.append({
            "type": "table",
            "columns": [{"key": "p", "label": "상품", "num": False},
                        {"key": "a", "label": "기초자산", "num": False},
                        {"key": "lv", "label": "현재 레벨", "num": True},
                        {"key": "ki", "label": "낙인", "num": True},
                        {"key": "bf", "label": "여유", "num": True},
                        {"key": "dr", "label": "추가 하락 시 낙인", "num": True},
                        {"key": "amt", "label": "투자금", "num": True}],
            # 여유 20%p 이하는 포트폴리오 화면의 낙인 경보와 같은 기준으로 강조한다
            "rows": krows,
            "mobile": _mobile(krows, 0, 4,
                              lambda r: f"{r[1]['text']} · 레벨 {r[2]['text']}"),
        })
    elif res.get("ki_buffer_reason"):
        out.append(_note(["낙인 여유를 계산할 시세가 아직 없습니다."]))

    ne = res.get("next_eval")
    if ne:
        out.append(_section("다가오는 평가일", "fa-calendar-day"))
        out.append({
            "type": "table",
            "columns": [{"key": "d", "label": "평가일", "num": False},
                        {"key": "left", "label": "남은 일수", "num": True},
                        {"key": "p", "label": "상품", "num": False},
                        {"key": "n", "label": "회차", "num": True},
                        {"key": "b", "label": "배리어", "num": True},
                        {"key": "amt", "label": "투자금", "num": True}],
            "rows": [[_cell(r["date"]), _cell(f"{r['days_left']:,}일"), _cell(r["product"]),
                      _cell(f"{r['n']}차"), _cell(_d(r["barrier"])), _cell(_d(r["amount"]))]
                     for r in ne],
        })

    st = res.get("stress_test")
    if st:
        shocks = st["shocks"]
        out.append(_section("스트레스 테스트 — 기초자산이 여기서 더 내려간다면",
                            "fa-arrow-trend-down"))
        cols = [{"key": "a", "label": "기초자산", "num": False},
                {"key": "e", "label": "노출", "num": True},
                {"key": "b", "label": "평균 여유", "num": True}]
        cols += [{"key": f"s{d}", "label": f"-{d}%", "num": True} for d in shocks]
        rows = []
        for r in st["rows"]:
            cells = [_cell(r["asset"]), _cell(_d(r["exposure"])), _cell(_d(r["avg_buffer"]))]
            for d in shocks:
                v = r["loss"][str(d)]
                cells.append(_cell(_d(v), "red" if (v and v["value"] > 3) else None))
            rows.append(cells)
        foot = [_cell("합계 (투자 단위)", bold=True), _cell("—"), _cell("—")]
        for d in shocks:
            v = st["total"][str(d)]
            foot.append(_cell(_d(v), "red" if (v and v["value"] > 3) else None, bold=True))
        out.append({"type": "table", "columns": cols, "rows": rows, "foot": foot,
                    "mobile": _mobile(rows, 0, len(cols) - 2,
                                      lambda r: f"노출 {r[1]['text']}")})
        out.append(_note([
            f"원금 대비 예상 손실률입니다. 낙인 확정 시 손실률 {_d(st['loss_assumed'])} 가정 "
            f"· 노출 3% 미만 자산은 제외.",
            "합계는 자산 행의 단순 합이 아니라 투자 단위 union입니다 — 워스트오브 상품은 "
            "어느 자산이든 발동가를 깨면 낙인 1번이라 다중자산 상품을 중복으로 세지 않습니다. "
            "포트폴리오 화면의 스트레스 테스트와 같은 계산입니다.",
            (f"시세를 확보하지 못해 계산에서 빠진 보유 {st['missing_n']}건이 있습니다."
             if st.get("missing_n") else None)]))
    elif res.get("stress_test_reason"):
        out.append(_note(["스트레스 테스트를 돌릴 시세가 아직 없습니다."]))

    ms = res.get("maturity_schedule")
    if ms:
        out.append(_section("평가일 분포", "fa-calendar"))
        out.append({"type": "table",
                    "columns": [{"key": "m", "label": "월", "num": False},
                                {"key": "n", "label": "건수", "num": True}],
                    "rows": [[_cell(r["month"]), _cell(f"{r['n']:,}건")] for r in ms]})
    return out


def _alert(row):
    b = row.get("buffer")
    return bool(b and b.get("value") is not None and b["value"] <= 20)


# ══════════════════════════════════════════════════════════════════
# product_filter / product_stats
# ══════════════════════════════════════════════════════════════════

def _filter_blocks(res):
    out = [_section(f"조건 검색 결과 {res['n']}건", "fa-filter")]
    if res.get("chips"):
        out.append({"type": "chips", "items": res["chips"]})
    prows = [[_cell(f"{p['issuer']} {p['no']}"), _cell(p["no"]), _cell(p["assets"]),
              _cell(_d(p["yield"]), "blue", bold=True), _cell(p["ki"]),
              _cell(_d(p["loss_prob"])), _cell(p["sub_end"])]
             for p in res["products"]]
    out.append({
        "type": "table",
        "columns": [{"key": "i", "label": "발행사", "num": False},
                    {"key": "no", "label": "상품번호", "num": False},
                    {"key": "a", "label": "기초자산", "num": False},
                    {"key": "y", "label": "수익률", "num": True},
                    {"key": "k", "label": "낙인", "num": True},
                    {"key": "l", "label": "손실확률", "num": True},
                    {"key": "s", "label": "마감", "num": True}],
        "rows": prows,
        "mobile": _mobile(prows, 0, 3,
                          lambda r: f"{r[2]['text']} · 낙인 {r[4]['text']}"),
    })
    out.append(_note([f"목록에서 {res.get('excluded', 'ELB·DLB 제외')}."]))
    return out


def _stats_blocks(res):
    rows = res.get("rows") or []
    if not rows:
        return []
    out = [_section("집계 결과", "fa-chart-simple")]
    keys = [k for k in rows[0] if k != "bands"]
    label = {"bucket": "구간", "count": "건수", "share": "비중", "issuer": "발행사",
             "asset": "기초자산", "band": "구간", "loss_count": "손실 건수",
             "loss_rate": "손실 비율", "realized_yield": "실현수익률"}
    out.append({
        "type": "table",
        "columns": [{"key": k, "label": label.get(k, k), "num": k != "bucket"} for k in keys],
        "rows": [[_cell(_d(r.get(k)),
                        _tone_by_sign(r[k]) if k == "realized_yield" else None,
                        bold=(k == "realized_yield")) for k in keys] for r in rows],
    })
    notes = [res.get("note"), res.get("source") and f"출처: {res['source']}"]
    out.append(_note(notes))
    return out


# ══════════════════════════════════════════════════════════════════
# 계산 근거
# ══════════════════════════════════════════════════════════════════

_FORMULA = {
    "cumulative_return": "누적 = 마지막/처음 − 1",
    "period_return": "구간 = 마지막/처음 − 1",
    "cagr": "CAGR = (마지막/처음)^(1/연수) − 1",
    "mdd": "최대낙폭 = 직전 최고가 대비 최대 하락률",
    "mdd_detail": "최대낙폭 = 직전 최고가 대비 최대 하락률",
    "volatility": "변동성 = 일간 수익률 표준편차 × √252",
    "days_below": "기준가 = 구간 첫 종가, 그 대비 기준선 미만인 거래일 수",
    "level_vs_high": "고점대비 = 현재/구간 최고 − 1",
    "recovery_days": "회복일수 = 저점일 ~ 직전 고점 회복일",
}


def _basis(calls, results):
    """근거 dl. 시세 계열이 쓰인 경우에만 만든다 (집계·검색은 출처 주석으로 충분)."""
    rows, tools, formulas = [], [], []
    meta = None
    for c, r in zip(calls, results):
        tools.append(c["name"])
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        if c["name"] == "external_search":
            rows.append(("외부 검색", r.get("query") or "최신 공개 사실"))
            rows.append(("검색 출처", f"{len(r.get('sources') or [])}개"))
        if r.get("meta") and meta is None:
            meta = r["meta"]
            rows.append(("대상", f"{r.get('asset')} — 티커 {meta['ticker']}"))
            rows.append(("기간", f"{meta['first']} ~ {meta['last']}"
                                 + (f" ({_d(r['span']['years'])})" if r.get("span") else "")))
            rows.append(("표본", f"{meta['rows']:,} 거래일"))
            rows.append(("가격 계열", meta["series"]
                         + (" — 배당·분할 소급 반영" if meta["price_kind"] != "raw"
                            else " — 증권사 고시 종가와 같은 계열")))
            rows.append(("보완 구간", meta["filled_note"]))
        if c["name"] == "price_series" and r.get("points"):
            rows.append(("차트", f"{len(r['points'])}개 포인트로 축약 (계산은 일별 전수)"))
        if c["name"] == "metric_calc":
            for m in (c.get("input") or {}).get("metrics") or []:
                if m in _FORMULA and _FORMULA[m] not in formulas:
                    formulas.append(_FORMULA[m])
    if not rows:
        return None
    if formulas:
        rows.append(("계산식", " · ".join(formulas)))
    rows.append(("실행 도구", " → ".join(tools) + " (자산명 해석은 서버가 처리)"))
    return {
        "rows": [{"label": k, "value": v} for k, v in rows],
        "tools": tools,
        "note": "답변에 쓰인 숫자는 전부 위 도구가 돌려준 값을 그대로 옮긴 것입니다. "
                "AI는 숫자를 직접 만들거나 다시 계산하지 않습니다.",
    }


BUILDERS = {
    "metric_calc": _metric_blocks,
    "portfolio_facts": _portfolio_blocks,
    "product_filter": _filter_blocks,
    "product_stats": _stats_blocks,
    "external_search": _external_blocks,
}


def build(calls, results):
    """(blocks, basis). 실패한 도구는 사유 주석 한 줄로만 남긴다."""
    blocks = []
    has_portfolio = any(c["name"] == "portfolio_facts" for c in calls)
    for c, r in zip(calls, results):
        if not isinstance(r, dict):
            continue
        if not r.get("ok"):
            blocks.append(_note([f"[{c['name']}] {r.get('detail') or '조회 결과가 없습니다.'} "
                                 f"(사유 {r.get('reason', 'NO_DATA')})"], tone="warn"))
            continue
        if c["name"] == "price_series":
            ch = _chart(r)
            if ch:
                blocks.append(_section(f"{r.get('asset')} 추이 ({ch['freq_note']} · 로그 눈금)",
                                       "fa-chart-area"))
                blocks.append(ch)
            continue
        fn = BUILDERS.get(c["name"])
        if fn:
            blocks.extend(fn(r))
    return blocks, _basis(calls, results),
