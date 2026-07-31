"""v7 확정 규칙 최종 검증표 — 연도별 × 유형별 (건수 / 성공률 / 손실률).

코어: 지수형 · 1차<=90 · 고점<95 · ki < 지수형 p30(직전연도)
위성: 종목형 · 1차<=85 · 고점<95 · ki < 종목형 p30(직전연도)
대조: 같은 유형 시장 전체 (규칙 무관, 판정확정 전체)
손실률 분모 = 결말 확정(실제 조기상환 완료 or 만기 판정 가능) 건.
"""
import os
from collections import defaultdict
from datetime import date, timedelta

import django
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "els_platform.settings")
django.setup()

from core import hist_radar, market  # noqa: E402
from core.models import HistoricalIssue, HistoricalRedemption  # noqa: E402

YEARS = tuple(range(2016, 2026))
TODAY = date.today()
store = hist_radar.PriceStore(throttle=0.0)
RED = dict(HistoricalRedemption.objects.exclude(exercise_type="").values_list(
    "isin", "exercise_type"))

dist = defaultdict(list)
for d, sort, ki in HistoricalIssue.objects.filter(
        detail_fetched=True, ki__isnull=False, issue_date__isnull=False
        ).values_list("issue_date", "basset_sort", "ki").iterator(chunk_size=4000):
    t = "지수형" if (sort or "").strip() == "지수" else "종목형"
    dist[(d.year, t)].append(ki)
cuts = {}
for y in YEARS:
    for t in ("지수형", "종목형"):
        prev = sorted(dist.get((y - 1, t), []))
        cuts[(y, t)] = prev[int(len(prev) * 0.30)] if len(prev) >= 100 else None

B0_MAX = {"지수형": 90, "종목형": 85}

# 집계 버킷: (연도, 유형, "rule"|"mkt") → [건수, 성공, 결말, 손실]
agg = defaultdict(lambda: [0, 0, 0, 0])

for h in HistoricalIssue.objects.filter(
        issue_date__year__in=YEARS, verdict_met__isnull=False).only(
        "verdict_met", "ki", "stepdown_barriers", "basset_sort",
        "assets", "eval_dates", "issue_date", "isin"):
    t = "지수형" if (h.basset_sort or "").strip() == "지수" else "종목형"
    y = h.issue_date.year
    bars = h.stepdown_barriers or []
    b0 = bars[0] if bars and isinstance(bars[0], (int, float)) else None
    blast = bars[-1] if bars and isinstance(bars[-1], (int, float)) else None
    evs = [str(d)[:10] for d in (h.eval_dates or [])]
    fin_date = date.fromisoformat(max(evs)) if evs else None
    early = RED.get(h.isin) == "조기상환"

    # ── 시장 대조군: 손실 판정은 가격 필요 — 조기상환이면 가격 없이 확정 ──
    need_price = not early and fin_date and fin_date <= TODAY - timedelta(days=3) \
        and blast is not None and h.ki is not None
    cut = cuts.get((y, t))
    rule_candidate = (cut is not None and h.ki is not None and h.ki < cut
                      and b0 is not None and b0 <= B0_MAX[t])

    peak = None
    touch = False
    fin_worst = None
    ok = True
    if need_price or rule_candidate:
        for a in (h.assets or []):
            if not isinstance(a, dict):
                ok = False
                break
            tk = hist_radar.resolve_asset_ticker(a)
            s = store.get(tk) if tk else None
            if s is None:
                ok = False
                break
            ic = store.close_on(tk, h.issue_date)
            if ic is None:
                ok = False
                break
            ref = ic if market.is_normalized_std_price(a.get("std_price"), ic) else float(a["std_price"])
            if ref <= 0:
                ok = False
                break
            if rule_candidate:
                past = s[(s.index >= str(h.issue_date - timedelta(days=365))) &
                         (s.index < str(h.issue_date))]
                if not past.empty:
                    r = ref / float(past.max()) * 100
                    peak = r if peak is None else max(peak, r)
            if h.ki is not None:
                seg = s[s.index >= str(h.issue_date)]
                if not seg.empty and float(seg.min()) < ref * h.ki / 100.0:
                    touch = True
            if need_price:
                c = store.close_on(tk, fin_date)
                if c is None:
                    fin_worst = None
                    ok = ok  # 해당 상품 만기판정 불가로 처리
                else:
                    lv = c / ref * 100
                    fin_worst = lv if fin_worst is None else min(fin_worst, lv)

    matured = bool(need_price and ok and fin_worst is not None)
    settled = early or matured
    loss = bool(matured and not early and fin_worst < blast and touch)

    # 시장 전체 (유형별)
    a1 = agg[(y, t, "mkt")]
    a1[0] += 1
    a1[1] += bool(h.verdict_met)
    if settled:
        a1[2] += 1
        a1[3] += loss

    # 규칙 통과 (코어/위성)
    if rule_candidate and ok and peak is not None and peak < 95:
        a2 = agg[(y, t, "rule")]
        a2[0] += 1
        a2[1] += bool(h.verdict_met)
        if settled:
            a2[2] += 1
            a2[3] += loss

def fmt(b):
    n, hit, st, lo = b
    if not n:
        return "     -            "
    succ = hit / n * 100
    lr = ("%5.2f%%" % (lo / st * 100)) if st else "   - "
    return "%5d %6.1f%% %s" % (n, succ, lr)

print("=" * 100)
print("  v7 확정 규칙 최종 검증 — 연도별 × 유형별 (건수 / 1년 성공률 / 실손실률)")
print("  코어=지수형(1차<=90·고점<95·낙인<p30)  위성=종목형(1차<=85·고점<95·낙인<p30)")
print("=" * 100)
print("연도  │ 코어(규칙)                │ 지수형 시장 전체         │ 위성(규칙)                │ 종목형 시장 전체")
print("─" * 100)
for y in YEARS:
    print("%d  │ %s │ %s │ %s │ %s" % (
        y,
        fmt(agg[(y, "지수형", "rule")]), fmt(agg[(y, "지수형", "mkt")]),
        fmt(agg[(y, "종목형", "rule")]), fmt(agg[(y, "종목형", "mkt")])))
print("─" * 100)
tot = {}
for t in ("지수형", "종목형"):
    for k in ("rule", "mkt"):
        b = [0, 0, 0, 0]
        for y in YEARS:
            for i in range(4):
                b[i] += agg[(y, t, k)][i]
        tot[(t, k)] = b
print("합계  │ %s │ %s │ %s │ %s" % (
    fmt(tot[("지수형", "rule")]), fmt(tot[("지수형", "mkt")]),
    fmt(tot[("종목형", "rule")]), fmt(tot[("종목형", "mkt")])))
print()
for t in ("지수형", "종목형"):
    b = tot[(t, "rule")]
    print("%s 규칙 합계: %s건 / 성공 %.1f%% / 결말확정 %s건 중 손실 %d건 (%.2f%%)" % (
        t, format(b[0], ","), b[1] / b[0] * 100 if b[0] else 0,
        format(b[2], ","), b[3], b[3] / b[2] * 100 if b[2] else 0))
