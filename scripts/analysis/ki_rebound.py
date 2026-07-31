"""낙인 터치 자산의 이후 1년 수익률.

2023~2025 판정확정 상품에서, 각 기초자산이 그 상품의 낙인선(기준가 × 낙인%)
아래로 처음 내려간 날을 '낙인 이벤트'로 잡고, 그 시점 종가 대비 1년 뒤 종가의
수익률을 자산별로 집계한다. (상품 단위 이벤트라 같은 자산이 여러 번 세어지며,
이는 '상품 투자자 관점의 평균 경험'에 해당한다.)
"""
import os
from collections import defaultdict
from datetime import date, timedelta

import django
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "els_platform.settings")
django.setup()

from core import hist_radar, market  # noqa: E402
from core.models import HistoricalIssue  # noqa: E402

TODAY = date.today()
store = hist_radar.PriceStore(throttle=0.0)

events = defaultdict(list)      # 자산명 → [(터치일, 1년수익률 or None)]
seen_products = 0

for h in HistoricalIssue.objects.filter(
        issue_date__year__in=(2023, 2024, 2025), verdict_met__isnull=False,
        ki__isnull=False).only("ki", "assets", "issue_date"):
    seen_products += 1
    for a in (h.assets or []):
        if not isinstance(a, dict):
            continue
        tk = hist_radar.resolve_asset_ticker(a)
        s = store.get(tk) if tk else None
        if s is None:
            continue
        ic = store.close_on(tk, h.issue_date)
        if ic is None:
            continue
        ref = ic if market.is_normalized_std_price(a.get("std_price"), ic) else float(a["std_price"])
        if ref <= 0:
            continue
        ki_line = ref * h.ki / 100.0
        seg = s[s.index >= str(h.issue_date)]
        below = seg[seg < ki_line]
        if below.empty:
            continue
        d0 = pd.Timestamp(below.index[0]).date()
        p0 = float(below.iloc[0])
        after = s[s.index >= str(d0 + timedelta(days=365))]
        ret = (float(after.iloc[0]) / p0 - 1) * 100 if not after.empty else None
        name = (a.get("name") or "").strip()
        events[name].append((d0, ret))

print(f"검사 상품 {seen_products:,}건 / 낙인 이벤트 {sum(len(v) for v in events.values()):,}건 "
      f"(자산 {len(events)}종)\n")

print(f"{'자산':30} {'이벤트':>6} {'1년경과':>7} {'평균':>8} {'중앙값':>8} "
      f"{'상승비율':>8}  터치 시기")
print("-" * 96)
rows = []
for name, evs in sorted(events.items(), key=lambda kv: -len(kv[1])):
    rets = [r for _, r in evs if r is not None]
    dates = [d for d, _ in evs]
    if len(evs) < 5:
        continue
    if rets:
        rets_s = sorted(rets)
        med = rets_s[len(rets_s) // 2]
        avg = sum(rets) / len(rets)
        pos = sum(1 for r in rets if r > 0) / len(rets) * 100
        rows.append((name, len(evs), len(rets), avg, med, pos, min(dates), max(dates)))
    else:
        rows.append((name, len(evs), 0, None, None, None, min(dates), max(dates)))

for name, n, nr, avg, med, pos, d0, d1 in rows:
    period = f"{d0.strftime('%y.%m')}~{d1.strftime('%y.%m')}"
    if nr:
        print(f"{name[:30]:30} {n:6,} {nr:7,} {avg:+7.1f}% {med:+7.1f}% {pos:7.0f}%  {period}")
    else:
        print(f"{name[:30]:30} {n:6,} {'0':>7} {'(1년 미경과)':>18}  {period}")

# 전체 요약
all_rets = [r for evs in events.values() for _, r in evs if r is not None]
if all_rets:
    s = sorted(all_rets)
    print("-" * 96)
    print(f"{'전체':30} {sum(len(v) for v in events.values()):6,} {len(all_rets):7,} "
          f"{sum(all_rets)/len(all_rets):+7.1f}% {s[len(s)//2]:+7.1f}% "
          f"{sum(1 for r in all_rets if r>0)/len(all_rets)*100:7.0f}%")
