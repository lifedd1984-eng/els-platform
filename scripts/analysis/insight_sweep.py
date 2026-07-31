"""추가 인사이트 스윕 — 아직 안 본 각도 5개.

A. 기초자산 개수별 성공률/수익률 (워스트오브 구조의 자산 수 효과)
B. 1년 내 평가 회차 수별 성공률 (기회가 많을수록 유리한가)
C. 스텝다운 기울기(1차−막차)별 성공률
D. 동일 조건(유형×1차배리어대×낙인대) 내 쿠폰 상위/하위 비교 — 쿠폰=위험신호 가설
E. 발행 시점 가격 위치(기준가/직전52주 최고)별 성공률 — 고점 발행 위험 (종목형)
대상: 2023~2025 판정확정.
"""
import os
from collections import defaultdict
from datetime import timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "els_platform.settings")
django.setup()

from core import hist_radar, market  # noqa: E402
from core.models import HistoricalIssue  # noqa: E402

rows = []
for h in HistoricalIssue.objects.filter(
        issue_date__year__in=(2023, 2024, 2025), verdict_met__isnull=False).only(
        "verdict_met", "ki", "stepdown_barriers", "basset_sort", "yield_rate",
        "assets", "eval_dates", "issue_date"):
    b = h.stepdown_barriers or []
    if not b or not isinstance(b[0], (int, float)):
        continue
    lim = h.issue_date + timedelta(days=365)
    n1y = sum(1 for d in (h.eval_dates or []) if str(d)[:10] <= lim.isoformat())
    rows.append({
        "h": h, "hit": bool(h.verdict_met),
        "t": "지수형" if h.basset_sort == "지수" else "종목형",
        "ki": h.ki, "b0": b[0],
        "blast": b[-1] if isinstance(b[-1], (int, float)) else None,
        "y": h.yield_rate, "na": len(h.assets or []), "n1y": n1y,
    })
print(f"표본 {len(rows):,}건 (2023~2025 판정확정)\n")


def line(sel, label):
    n = len(sel)
    if n < 80:
        return
    p = sum(1 for r in sel if r["hit"]) / n * 100
    ys = [r["y"] for r in sel if r["y"] is not None]
    print(f"   {label:16} {n:6,}건  성공 {p:5.1f}%  수익률 {sum(ys)/len(ys):5.1f}%")


print("[A] 기초자산 개수")
for t in ("종목형", "지수형"):
    print(f"  {t}:")
    for na in (1, 2, 3, 4):
        line([r for r in rows if r["t"] == t and r["na"] == na], f"{na}개")

print()
print("[B] 1년 내 평가 회차 수")
for t in ("종목형", "지수형"):
    print(f"  {t}:")
    for k in (1, 2, 3, 4):
        lab = f"{k}회" if k < 4 else "4회+"
        sel = [r for r in rows if r["t"] == t and
               (r["n1y"] == k if k < 4 else r["n1y"] >= 4)]
        line(sel, lab)

print()
print("[C] 스텝다운 기울기 (1차 − 막차 배리어)")
for t in ("종목형", "지수형"):
    print(f"  {t}:")
    for lo, hi, lab in ((0, 5, "완만(0~5)"), (5, 15, "보통(5~15)"),
                        (15, 25, "가파름(15~25)"), (25, 99, "매우 가파름(25+)")):
        sel = [r for r in rows if r["t"] == t and r["blast"] is not None
               and lo <= r["b0"] - r["blast"] < hi]
        line(sel, lab)

print()
print("[D] 같은 조건 안에서 쿠폰 상위 vs 하위 (유형×배리어5%대×낙인10대 셀 고정)")
cells = defaultdict(list)
for r in rows:
    if r["ki"] is None or r["y"] is None:
        continue
    cells[(r["t"], int(r["b0"] // 5 * 5), int(r["ki"] // 10 * 10))].append(r)
agg = {"상위": [0, 0], "하위": [0, 0]}
for sel in cells.values():
    if len(sel) < 40:
        continue
    sel = sorted(sel, key=lambda r: r["y"])
    half = len(sel) // 2
    for grp, part in (("하위", sel[:half]), ("상위", sel[-half:])):
        agg[grp][0] += len(part)
        agg[grp][1] += sum(1 for r in part if r["hit"])
for grp, (n, k) in agg.items():
    print(f"   쿠폰 {grp} 절반   {n:6,}건  성공 {k/n*100:5.1f}%")

print()
print("[E] 발행 시점 가격 위치 — 기준가 / 직전 52주 최고 (종목형, 워스트 자산 기준)")
store = hist_radar.PriceStore(throttle=0.0)
buckets = defaultdict(lambda: [0, 0, []])
skip = 0
for r in rows:
    if r["t"] != "종목형":
        continue
    h = r["h"]
    peak_ratio = None       # 자산 중 가장 고점에 가까운 것 (위험 프록시)
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
        past = s[(s.index >= str(h.issue_date - timedelta(days=365))) &
                 (s.index < str(h.issue_date))]
        if past.empty or ref <= 0:
            ok = False
            break
        ratio = ref / float(past.max()) * 100
        peak_ratio = ratio if peak_ratio is None else max(peak_ratio, ratio)
    if not ok or peak_ratio is None:
        skip += 1
        continue
    if peak_ratio >= 95:
        key = "고점권(95%+)"
    elif peak_ratio >= 85:
        key = "고점 근처(85~95)"
    elif peak_ratio >= 70:
        key = "중간(70~85)"
    else:
        key = "저점권(<70)"
    buckets[key][0] += 1
    buckets[key][1] += r["hit"]
    if r["y"] is not None:
        buckets[key][2].append(r["y"])
for key in ("고점권(95%+)", "고점 근처(85~95)", "중간(70~85)", "저점권(<70)"):
    n, k, ys = buckets[key]
    if n:
        print(f"   {key:14} {n:6,}건  성공 {k/n*100:5.1f}%  수익률 {sum(ys)/len(ys):5.1f}%")
print(f"   (판정불가 제외 {skip}건)")
