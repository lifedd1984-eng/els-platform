"""단일 기초자산 쏠림 상한 시뮬레이션 (전체 후보 풀 재현판).

verify_historical과 동일하게 AdaptiveGate로 주차별 컷을 만들고
hist_radar.reproduce_radar의 게이트를 그대로 통과시킨 뒤,
"한 기초자산이 그 주 배지의 X%를 넘지 못한다"는 상한을 얹어 다시 뽑는다.
"""
import os
import sys
from collections import Counter, defaultdict
from datetime import timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "els_platform.settings")
django.setup()

from core import hist_radar  # noqa: E402
from core.models import (HistoricalIssue, RADAR_EARLY_MIN, RADAR_LOSS_MAX,  # noqa: E402
                         RADAR_SCORE_SHIFT, RADAR_TOP_STRONG, RADAR_TOP_WEAK,
                         RADAR_YIELD_TOP_PCT)

YEARS = [int(y) for y in sys.argv[1].split(",")] if len(sys.argv) > 1 else [2023, 2024, 2025]
CAPS = [None, 0.5, 0.4, 0.3, 0.2]
TOP_N = RADAR_TOP_WEAK

# ── 게이트 구성 (verify_historical._build_gate와 동일) ──
rows = []
for d, sort, ki, bars in HistoricalIssue.objects.filter(
        product_type="ELS", recu_whcd="공모", detail_fetched=True, parse_error="",
        issue_date__isnull=False, ki__isnull=False).values_list(
        "issue_date", "basset_sort", "ki", "stepdown_barriers").iterator(chunk_size=2000):
    at = "지수형" if (sort or "").strip() == "지수" else "종목형"
    last = bars[-1] if bars and isinstance(bars[-1], (int, float)) else None
    rows.append((d, at, ki, last))
gate = hist_radar.AdaptiveGate(rows, anchor_year=None)
print(f"게이트 앵커: {gate.anchor_year}년 / 분포표본 {len(rows):,}건")

# ── 주차×유형 그룹 적재 ──
groups = defaultdict(list)
for h in HistoricalIssue.objects.filter(
        product_type="ELS", recu_whcd="공모", detail_fetched=True, parse_error="",
        issue_date__year__in=YEARS, verdict_met__isnull=False):
    monday = h.issue_date - timedelta(days=h.issue_date.weekday())
    at = "지수형" if h.basset_sort == "지수" else "종목형"
    groups[(monday, at)].append(h)
print(f"그룹 {len(groups):,}개 / 판정확정 상품 {sum(len(v) for v in groups.values()):,}건")


def keys(h):
    return [((a.get("name") if isinstance(a, dict) else str(a)) or "").strip()
            for a in (h.assets or []) if a]


def survivors_of(members, at, monday):
    """reproduce_radar와 같은 게이트 + 점수순 정렬된 후보 리스트."""
    cut = gate.cuts(monday, at)
    if not cut:
        return []
    ki_cut, last_cut = cut["cut_ki"], cut["cut_last"]
    out = []
    for p in members:
        bars = p.stepdown_barriers or []
        if p.ki is None or p.ki >= ki_cut:
            continue
        if p.sim_early_1y is None or p.sim_loss_prob is None:
            continue
        if p.sim_early_1y < RADAR_EARLY_MIN or p.sim_loss_prob >= RADAR_LOSS_MAX:
            continue
        if not bars or bars[-1] is None or bars[-1] > last_cut:
            continue
        out.append(p)
    if not out:
        return []
    ys = sorted((p.yield_rate or 0) for p in out)
    thr = ys[int(len(ys) * (1 - RADAR_YIELD_TOP_PCT))] if ys else 0
    out = [p for p in out if (p.yield_rate or 0) >= thr]
    out.sort(key=lambda p: ((p.yield_rate or 0) - (p.ki or 0) + RADAR_SCORE_SHIFT),
             reverse=True)
    return out


pools = {}
for (monday, at), members in groups.items():
    s = survivors_of(members, at, monday)
    if s:
        pools[(monday, at)] = s
print(f"게이트 통과 후보: {sum(len(v) for v in pools.values()):,}건 "
      f"(그룹당 평균 {sum(len(v) for v in pools.values()) / max(1, len(pools)):.1f}건)")
print()


def run(cap):
    picked = []
    for (monday, at), pool in pools.items():
        if cap is None:
            picked.extend(pool[:TOP_N])
            continue
        limit = max(1, round(TOP_N * cap))
        used, chosen = Counter(), []
        for p in pool:
            if len(chosen) >= TOP_N:
                break
            ks = keys(p)
            if ks and any(used[k] + 1 > limit for k in ks):
                continue
            for k in ks:
                used[k] += 1
            chosen.append(p)
        picked.extend(chosen)
    return picked


hdr = (f"{'상한':>10} {'배지수':>7} {'성공률':>8} {'수익률':>8} "
       f"{'최다자산':>9} {'LG화학':>8} {'HSCEI':>8}")
print(hdr)
print("-" * len(hdr))
results = {}
for cap in CAPS:
    picked = run(cap)
    results[cap] = picked
    n = len(picked)
    if not n:
        continue
    hits = sum(1 for p in picked if p.verdict_met)
    ys = [p.yield_rate for p in picked if p.yield_rate is not None]
    cnt = Counter()
    for p in picked:
        for k in keys(p):
            cnt[k] += 1
    top = cnt.most_common(1)[0][1] / n * 100 if cnt else 0
    lg = sum(1 for p in picked if any("LG화학" in k for k in keys(p))) / n * 100
    hs = sum(1 for p in picked if any("Hang Seng China" in k for k in keys(p))) / n * 100
    lab = "현행(없음)" if cap is None else f"{int(cap * 100)}%"
    print(f"{lab:>10} {n:7,} {hits / n * 100:7.1f}% {sum(ys) / len(ys):7.1f}% "
          f"{top:8.1f}% {lg:7.1f}% {hs:7.1f}%")

print()
print("=== 연도별 성공률 ===")
print(f"{'상한':>10} " + " ".join(f"{y:>9}" for y in YEARS))
print("-" * (11 + 10 * len(YEARS)))
for cap, picked in results.items():
    lab = "현행(없음)" if cap is None else f"{int(cap * 100)}%"
    row = f"{lab:>10} "
    for y in YEARS:
        sel = [p for p in picked if p.issue_date.year == y]
        row += (f"{sum(1 for p in sel if p.verdict_met) / len(sel) * 100:8.1f}% "
                if sel else "        - ")
    print(row)
