"""코어/위성 규칙 세트 백테스트 — 1년 성공률·낙인 터치율·실제 원금손실률·공급량.

한 번의 가격 패스로 상품별 특성을 전부 계산한 뒤 규칙 세트별로 집계한다.
손실 판정: 만기(마지막 평가일) 도래 + 낙인 보유 상품에 한해,
  손실 = 만기 워스트 < 막차 배리어 AND 낙인 터치했음. (낙인 미터치면 원금 보전)
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

YEARS = tuple(int(a) for a in sys.argv[1:]) or (2023, 2024, 2025)
TODAY = date.today()
store = hist_radar.PriceStore(throttle=0.0)

# 실제 상환기록: 조기상환된 상품은 만기 손실 판정 대상이 아니다
RED = dict(HistoricalRedemption.objects.exclude(exercise_type="").values_list(
    "isin", "exercise_type"))


def has_hscei(assets):
    for a in assets or []:
        n = ((a.get("name") or "") if isinstance(a, dict) else str(a)).upper()
        if "HANG SENG CHINA" in n or "HSCEI" in n:
            return True
    return False


P = []
for h in HistoricalIssue.objects.filter(
        issue_date__year__in=YEARS, verdict_met__isnull=False).only(
        "verdict_met", "ki", "stepdown_barriers", "basset_sort", "yield_rate",
        "assets", "eval_dates", "issue_date", "radar_tier", "isin"):
    bars = h.stepdown_barriers or []
    b0 = bars[0] if bars and isinstance(bars[0], (int, float)) else None
    blast = bars[-1] if bars and isinstance(bars[-1], (int, float)) else None
    lim = h.issue_date + timedelta(days=365)
    n1y = sum(1 for d in (h.eval_dates or []) if str(d)[:10] <= lim.isoformat())
    evs = [str(d)[:10] for d in (h.eval_dates or [])]
    fin_date = date.fromisoformat(max(evs)) if evs else None

    peak = None
    touch = False
    fin_worst = None
    price_ok = True
    for a in (h.assets or []):
        if not isinstance(a, dict):
            price_ok = False
            break
        tk = hist_radar.resolve_asset_ticker(a)
        s = store.get(tk) if tk else None
        if s is None:
            price_ok = False
            break
        ic = store.close_on(tk, h.issue_date)
        if ic is None:
            price_ok = False
            break
        ref = ic if market.is_normalized_std_price(a.get("std_price"), ic) else float(a["std_price"])
        if ref <= 0:
            price_ok = False
            break
        past = s[(s.index >= str(h.issue_date - timedelta(days=365))) &
                 (s.index < str(h.issue_date))]
        if not past.empty:
            r = ref / float(past.max()) * 100
            peak = r if peak is None else max(peak, r)
        if h.ki is not None:
            seg = s[s.index >= str(h.issue_date)]
            if not seg.empty and float(seg.min()) < ref * h.ki / 100.0:
                touch = True
        if fin_date and fin_date <= TODAY - timedelta(days=3):
            c = store.close_on(tk, fin_date)
            if c is None:
                fin_worst = None
                price_ok = price_ok  # 해당 상품 손실판정만 포기
                fin_fail = True
            else:
                lv = c / ref * 100
                fin_worst = lv if fin_worst is None else min(fin_worst, lv)

    early = RED.get(h.isin) == "조기상환"      # 실제 조기상환 완료 → 원금+쿠폰 회수
    matured = bool(fin_date and fin_date <= TODAY - timedelta(days=3)
                   and fin_worst is not None and blast is not None and h.ki is not None
                   and price_ok)
    settled = early or matured                  # 결말이 확정된 상품
    loss = bool(matured and not early and fin_worst < blast and touch)

    P.append({
        "y": h.issue_date.year,
        "week": h.issue_date - timedelta(days=h.issue_date.weekday()),
        "t": "지수형" if h.basset_sort == "지수" else "종목형",
        "hit": bool(h.verdict_met), "ki": h.ki, "b0": b0, "n1y": n1y,
        "na": len(h.assets or []), "yield": h.yield_rate,
        "peak": peak if price_ok else None,
        "touch": touch if (price_ok and h.ki is not None) else None,
        "matured": settled, "loss": loss,
        "hscei": has_hscei(h.assets),
        "badge": bool(h.radar_tier),
    })

print(f"적재 {len(P):,}건\n")

SETS = [
    ("전체", lambda r: True),
    ("지수형 전체", lambda r: r["t"] == "지수형"),
    ("종목형 전체", lambda r: r["t"] == "종목형"),
    # 사전(ex-ante) 규칙만 — 특정 자산 이름을 아는 조건 없음
    ("A: 지수.1차<=90.고점<95", lambda r: r["t"] == "지수형" and r["b0"] is not None
        and r["b0"] <= 90 and r["peak"] is not None and r["peak"] < 95),
    ("B: 지수.1차<=85.고점<95", lambda r: r["t"] == "지수형" and r["b0"] is not None
        and r["b0"] <= 85 and r["peak"] is not None and r["peak"] < 95),
    ("C: 지수.1차<=90.고점<90", lambda r: r["t"] == "지수형" and r["b0"] is not None
        and r["b0"] <= 90 and r["peak"] is not None and r["peak"] < 90),
    ("D: A + 깊은낙인 ki<=45", lambda r: r["t"] == "지수형" and r["b0"] is not None
        and r["b0"] <= 90 and r["peak"] is not None and r["peak"] < 95
        and r["ki"] is not None and r["ki"] <= 45),
    ("E: 1차<=85.고점<90.ki<=45", lambda r: r["t"] == "지수형" and r["b0"] is not None
        and r["b0"] <= 85 and r["peak"] is not None and r["peak"] < 90
        and r["ki"] is not None and r["ki"] <= 45),
]
hdr = (f"{'세트':32} {'건수':>6} {'성공률':>7} {'터치율':>7} "
       f"{'만기판정':>8} {'손실':>5} {'손실률':>7} {'수익률':>7} {'주당공급':>8}")
print(hdr)
print("-" * len(hdr))

all_weeks = defaultdict(set)
for r in P:
    all_weeks[r["y"]].add(r["week"])

for name, pred in SETS:
    sel = [r for r in P if pred(r)]
    n = len(sel)
    if not n:
        print(f"{name:32} {'0':>6}")
        continue
    succ = sum(1 for r in sel if r["hit"]) / n * 100
    tden = [r for r in sel if r["touch"] is not None]
    trate = sum(1 for r in tden if r["touch"]) / len(tden) * 100 if tden else 0
    mat = [r for r in sel if r["matured"]]
    losses = sum(1 for r in mat if r["loss"])
    lrate = losses / len(mat) * 100 if mat else None
    ys = [r["yield"] for r in sel if r["yield"] is not None]
    wk = defaultdict(int)
    for r in sel:
        wk[r["week"]] += 1
    tot_weeks = sum(len(v) for v in all_weeks.values())
    supply = n / tot_weeks
    lr = f"{lrate:6.1f}%" if lrate is not None else "     -"
    print(f"{name:32} {n:6,} {succ:6.1f}% {trate:6.1f}% "
          f"{len(mat):8,} {losses:5,} {lr} {sum(ys)/len(ys):6.1f}% {supply:7.1f}개")

print()
print("=== 연도별 (성공률 / 손실률[만기판정 n]) ===")
for name, pred in SETS:
    row = f"{name:32}"
    for y in YEARS:
        sel = [r for r in P if pred(r) and r["y"] == y]
        if not sel:
            row += "        -        "
            continue
        succ = sum(1 for r in sel if r["hit"]) / len(sel) * 100
        mat = [r for r in sel if r["matured"]]
        losses = sum(1 for r in mat if r["loss"])
        lr = f"{losses/len(mat)*100:4.1f}%[{len(mat)}]" if mat else "  -  "
        row += f"  {succ:5.1f}%/{lr:>10}"
    print(row)
