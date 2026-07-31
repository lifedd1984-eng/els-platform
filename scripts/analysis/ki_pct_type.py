"""유형별 낙인 퍼센타일 컷 검증 — 지수형/종목형 각자 분포로 하위30% 미만.

컷 = 발행 직전 연도의 '같은 유형' 낙인 분포 하위 30% 값, 조건은 ki < 컷 (미만).
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

# 유형×연도 낙인 분포 → 직전 연도 하위30% 컷
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

print("연도별 하위30% 컷 (ki < 컷 적용):")
print("연도    지수형   종목형")
for y in YEARS:
    print("%d   %5s   %5s" % (y, cuts[(y, "지수형")] or "-", cuts[(y, "종목형")] or "-"))
print()

P = []
for h in HistoricalIssue.objects.filter(
        issue_date__year__in=YEARS, verdict_met__isnull=False).only(
        "verdict_met", "ki", "stepdown_barriers", "basset_sort", "yield_rate",
        "assets", "eval_dates", "issue_date", "isin"):
    bars = h.stepdown_barriers or []
    b0 = bars[0] if bars and isinstance(bars[0], (int, float)) else None
    blast = bars[-1] if bars and isinstance(bars[-1], (int, float)) else None
    if b0 is None or h.ki is None:
        continue
    t = "지수형" if (h.basset_sort or "").strip() == "지수" else "종목형"
    cut = cuts.get((h.issue_date.year, t))
    if cut is None or h.ki >= cut:
        continue                    # 낙인 조건 선적용 (가격 조회량 절감)
    lim = h.issue_date + timedelta(days=365)
    n1y = sum(1 for d in (h.eval_dates or []) if str(d)[:10] <= lim.isoformat())
    evs = [str(d)[:10] for d in (h.eval_dates or [])]
    fin_date = date.fromisoformat(max(evs)) if evs else None

    peak = None
    touch = False
    fin_worst = None
    ok = True
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
        past = s[(s.index >= str(h.issue_date - timedelta(days=365))) &
                 (s.index < str(h.issue_date))]
        if not past.empty:
            r = ref / float(past.max()) * 100
            peak = r if peak is None else max(peak, r)
        seg = s[s.index >= str(h.issue_date)]
        if not seg.empty and float(seg.min()) < ref * h.ki / 100.0:
            touch = True
        if fin_date and fin_date <= TODAY - timedelta(days=3):
            c = store.close_on(tk, fin_date)
            if c is None:
                fin_worst = None
            else:
                lv = c / ref * 100
                fin_worst = lv if fin_worst is None else min(fin_worst, lv)
    if not ok or peak is None:
        continue

    early = RED.get(h.isin) == "조기상환"
    matured = bool(fin_date and fin_date <= TODAY - timedelta(days=3)
                   and fin_worst is not None and blast is not None)
    P.append({"y": h.issue_date.year, "t": t, "b0": b0, "peak": peak,
              "na": len(h.assets or []), "n1y": n1y,
              "hit": bool(h.verdict_met), "settled": early or matured,
              "loss": bool(matured and not early and fin_worst < blast and touch),
              "yield": h.yield_rate})

n_i = sum(1 for r in P if r["t"] == "지수형")
n_s = sum(1 for r in P if r["t"] == "종목형")
print("낙인 퍼센타일 통과 표본: %s건 (지수형 %s / 종목형 %s)" % (
    format(len(P), ","), format(n_i, ","), format(n_s, ",")))
print()

VARIANTS = [
    ("지수형: 1차<=90.고점<95", lambda r: r["t"] == "지수형" and r["b0"] <= 90 and r["peak"] < 95),
    ("종목형: 1차<=90.고점<95", lambda r: r["t"] == "종목형" and r["b0"] <= 90 and r["peak"] < 95),
    ("종목형: 1차<=85.고점<95", lambda r: r["t"] == "종목형" and r["b0"] <= 85 and r["peak"] < 95),
    ("종목형: 1차<=85.고점<90", lambda r: r["t"] == "종목형" and r["b0"] <= 85 and r["peak"] < 90),
    ("종목형: 1차<=85.고점<95.자산<=2", lambda r: r["t"] == "종목형" and r["b0"] <= 85
        and r["peak"] < 95 and r["na"] <= 2),
    ("종목형: (참고) 낙인만", lambda r: r["t"] == "종목형"),
]

n_weeks = len(YEARS) * 52
hdr = "%-34s %6s %7s %6s %4s %7s %6s %5s" % ("변형 (공통: ki<유형별 하위30%)", "건수", "성공률", "결말", "손실", "손실률", "수익률", "주당")
print(hdr)
print("-" * len(hdr))
for name, pred in VARIANTS:
    sel = [r for r in P if pred(r)]
    if not sel:
        print("%-34s      0" % name)
        continue
    st = [r for r in sel if r["settled"]]
    losses = sum(1 for r in st if r["loss"])
    ys = [r["yield"] for r in sel if r["yield"] is not None]
    print("%-34s %6s %6.1f%% %6s %4d %6.2f%% %5.1f%% %4.1f개" % (
        name, format(len(sel), ","),
        sum(1 for r in sel if r["hit"]) / len(sel) * 100,
        format(len(st), ","), losses,
        losses / len(st) * 100 if st else 0,
        (sum(ys) / len(ys)) if ys else 0, len(sel) / n_weeks))

print()
print("=== 연도별 손실/결말확정 ===")
for name, pred in VARIANTS[:5]:
    by = defaultdict(lambda: [0, 0])
    for r in P:
        if pred(r) and r["settled"]:
            by[r["y"]][0] += 1
            by[r["y"]][1] += r["loss"]
    row = "%-34s" % name
    for y in YEARS:
        n, l = by.get(y, [0, 0])
        row += " %02d:%d/%d" % (y % 100, l, n) if n else " %02d:-" % (y % 100)
    print(row)
