"""낙인 퍼센타일 컷 검증 — 고정 45 vs 직전 연도 분포 하위 20/25/30%.

컷은 발행 '직전 연도'의 지수형 낙인 분포에서 산출 (완전 사전 정보).
기본 규칙 A(지수형·1차<=90·고점<95)에 각 낙인 조건을 얹어 비교한다.
마지막에 2021년 A세트 손실 상품의 낙인 값을 그대로 나열해
어느 컷이 재앙을 막는지 보인다.
"""
import os
import sys
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

# ── 직전 연도 지수형 낙인 분포 → 퍼센타일 컷 (전 발행상품 기준, 판정 여부 무관) ──
dist = defaultdict(list)
for d, sort, ki in HistoricalIssue.objects.filter(
        detail_fetched=True, ki__isnull=False, issue_date__isnull=False
        ).values_list("issue_date", "basset_sort", "ki").iterator(chunk_size=4000):
    if (sort or "").strip() == "지수":
        dist[d.year].append(ki)

cuts = {}
for y in YEARS:
    prev = sorted(dist.get(y - 1, []))
    if len(prev) < 100:
        cuts[y] = None
        continue
    cuts[y] = {p: prev[int(len(prev) * p / 100)] for p in (20, 25, 30)}

print("연도별 컷 (직전 연도 지수형 분포 기준):")
print("연도   하위20%  하위25%  하위30%")
for y in YEARS:
    c = cuts[y]
    print("%d   %s" % (y, "  ".join("%5s" % (c[p] if c else "-") for p in (20, 25, 30)) if c else "   (분포 부족)"))
print()

P = []
for h in HistoricalIssue.objects.filter(
        issue_date__year__in=YEARS, verdict_met__isnull=False).only(
        "verdict_met", "ki", "stepdown_barriers", "basset_sort", "yield_rate",
        "assets", "eval_dates", "issue_date", "isin"):
    if (h.basset_sort or "").strip() != "지수":
        continue
    bars = h.stepdown_barriers or []
    b0 = bars[0] if bars and isinstance(bars[0], (int, float)) else None
    blast = bars[-1] if bars and isinstance(bars[-1], (int, float)) else None
    if b0 is None or b0 > 90 or h.ki is None:
        continue                      # 기본 규칙 A의 배리어 조건 선적용
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
    if not ok or peak is None or peak >= 95:
        continue                      # 기본 규칙 A의 고점 조건

    early = RED.get(h.isin) == "조기상환"
    matured = bool(fin_date and fin_date <= TODAY - timedelta(days=3)
                   and fin_worst is not None and blast is not None)
    settled = early or matured
    loss = bool(matured and not early and fin_worst < blast and touch)
    P.append({"y": h.issue_date.year, "ki": h.ki, "hit": bool(h.verdict_met),
              "settled": settled, "loss": loss, "yield": h.yield_rate,
              "assets": " / ".join((a.get("name") or "")[:16] for a in (h.assets or [])
                                    if isinstance(a, dict))})

print(f"기본 규칙 A 통과 (지수형·1차<=90·고점<95): {len(P):,}건\n")

VARIANTS = [("고정 ki<=45", lambda r: r["ki"] <= 45)]
for p in (30, 25, 20):
    def make(pp):
        return lambda r: cuts.get(r["y"]) is not None and r["ki"] <= cuts[r["y"]][pp]
    VARIANTS.append((f"하위{p}% (직전연도)", make(p)))

print(f"{'변형':20} {'건수':>6} {'성공률':>7} {'결말확정':>7} {'손실':>4} {'손실률':>7} {'수익률':>6} {'주당':>5}")
print("-" * 72)
n_weeks = len(YEARS) * 52
for name, pred in VARIANTS:
    sel = [r for r in P if pred(r)]
    if not sel:
        print(f"{name:20} 0")
        continue
    st = [r for r in sel if r["settled"]]
    losses = sum(1 for r in st if r["loss"])
    ys = [r["yield"] for r in sel if r["yield"] is not None]
    print(f"{name:20} {len(sel):6,} {sum(1 for r in sel if r['hit'])/len(sel)*100:6.1f}% "
          f"{len(st):7,} {losses:4} {losses/len(st)*100 if st else 0:6.2f}% "
          f"{sum(ys)/len(ys):5.1f}% {len(sel)/n_weeks:4.1f}개")

print()
print("=== 변형별 연도 손실 분포 ===")
for name, pred in VARIANTS:
    by = defaultdict(lambda: [0, 0])
    for r in P:
        if pred(r) and r["settled"]:
            by[r["y"]][0] += 1
            by[r["y"]][1] += r["loss"]
    row = f"{name:20}"
    for y in YEARS:
        n, l = by.get(y, [0, 0])
        row += f" {y%100:02d}:{l}/{n}" if n else f" {y%100:02d}:-"
    print(row)

print()
print("=== 2021년 A세트 손실 상품의 낙인 값 (어느 컷이 막는가) ===")
for r in P:
    if r["y"] == 2021 and r["loss"]:
        print(f"  낙인 {r['ki']:>3} | {r['assets'][:60]}")
