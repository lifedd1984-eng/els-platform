"""단일 기초자산 쏠림 상한 시뮬레이션.

현재 레이더는 (주차 × 유형) 그룹에서 점수 상위 15개에 배지를 준다.
여기에 "한 기초자산이 그 주 배지의 X%를 넘지 못한다"는 상한을 얹으면
결과가 어떻게 달라지는지 본다.

적용 방식: 점수 높은 순으로 훑되, 그 자산을 담으면 상한을 넘는 상품은 건너뛰고
다음 후보로 넘어간다(정원은 그대로 채운다).
"""
import os
import sys
from collections import Counter, defaultdict

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "els_platform.settings")
django.setup()

from core.models import HistoricalIssue  # noqa: E402

YEARS = [int(y) for y in sys.argv[1].split(",")] if len(sys.argv) > 1 else [2023, 2024, 2025]
CAPS = [None, 0.5, 0.4, 0.3]      # None = 현행(상한 없음)
TOP_N = 15                         # 그룹당 배지 정원 (아주 강한 5 + 강한 10)
STRONG_N = 5


def asset_keys(h):
    out = []
    for a in (h.assets or []):
        n = (a.get("name") if isinstance(a, dict) else str(a)) or ""
        if n:
            out.append(n.strip())
    return out


# ── 데이터 적재: 실제 배지(radar_rank)가 있는 상품만이 후보였다 ──
groups = defaultdict(list)
for h in HistoricalIssue.objects.filter(
        issue_date__year__in=YEARS, verdict_met__isnull=False,
        radar_rank__isnull=False).only(
        "issue_date", "basset_sort", "radar_rank", "radar_tier",
        "verdict_met", "assets", "yield_rate", "ki", "isin"):
    monday = h.issue_date - __import__("datetime").timedelta(days=h.issue_date.weekday())
    t = "지수형" if h.basset_sort == "지수" else "종목형"
    groups[(monday, t)].append(h)

print(f"대상 연도: {YEARS}")
print(f"후보 그룹: {len(groups):,}개 / 후보 상품: {sum(len(v) for v in groups.values()):,}건")
print()


def run(cap):
    """상한 cap을 적용해 배지를 다시 뽑는다. cap=None이면 현행."""
    picked = []
    for key, members in groups.items():
        ranked = sorted(members, key=lambda h: h.radar_rank)
        if cap is None:
            picked.extend((h, i + 1) for i, h in enumerate(ranked[:TOP_N]))
            continue
        limit = max(1, int(TOP_N * cap))     # 한 자산의 최대 등장 횟수
        used = Counter()
        chosen = []
        for h in ranked:
            if len(chosen) >= TOP_N:
                break
            ks = asset_keys(h)
            if ks and any(used[k] + 1 > limit for k in ks):
                continue                      # 상한 초과 → 건너뛰고 다음 후보
            for k in ks:
                used[k] += 1
            chosen.append(h)
        picked.extend((h, i + 1) for i, h in enumerate(chosen))
    return picked


print(f"{'상한':>8} {'배지수':>7} {'성공률':>8} {'평균수익률':>10} "
      f"{'최다자산 비중':>12} {'LG화학 포함':>11} {'HSCEI 포함':>11}")
print("-" * 76)

for cap in CAPS:
    picked = run(cap)
    n = len(picked)
    if not n:
        continue
    hits = sum(1 for h, _ in picked if h.verdict_met)
    ys = [h.yield_rate for h, _ in picked if h.yield_rate is not None]
    cnt = Counter()
    for h, _ in picked:
        for k in asset_keys(h):
            cnt[k] += 1
    top_share = cnt.most_common(1)[0][1] / n * 100 if cnt else 0
    lg = sum(1 for h, _ in picked if any("LG화학" in k for k in asset_keys(h))) / n * 100
    hs = sum(1 for h, _ in picked if any("Hang Seng China" in k for k in asset_keys(h))) / n * 100
    label = "현행(없음)" if cap is None else f"{int(cap * 100)}%"
    print(f"{label:>8} {n:7,} {hits / n * 100:7.1f}% {sum(ys) / len(ys):9.1f}% "
          f"{top_share:11.1f}% {lg:10.1f}% {hs:10.1f}%")

print()
print("=== 연도별 성공률 ===")
print(f"{'상한':>8} " + " ".join(f"{y:>9}" for y in YEARS))
print("-" * (9 + 10 * len(YEARS)))
for cap in CAPS:
    picked = run(cap)
    label = "현행(없음)" if cap is None else f"{int(cap * 100)}%"
    row = f"{label:>8} "
    for y in YEARS:
        sel = [h for h, _ in picked if h.issue_date.year == y]
        row += f"{(sum(1 for h in sel if h.verdict_met) / len(sel) * 100):8.1f}% " if sel else "        - "
    print(row)
