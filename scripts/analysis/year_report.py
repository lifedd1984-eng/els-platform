"""연도별 레이더 검증 리포트 — 배지 vs 나머지 전부, 구성 보정, 조건별 비교.

사용: venv/bin/python year_report.py 2026
지표는 "1년 이내 조기상환 성공률". 대조군은 배지 없는 나머지 전부.
"""
import os
import sys
from collections import defaultdict

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "els_platform.settings")
django.setup()

from core.models import HistoricalIssue  # noqa: E402

YEAR = int(sys.argv[1])
MIN_N = 300   # 판정확정이 이보다 적은 연도는 표본 부족으로 제외


def has_hscei(assets):
    """HSCEI(홍콩H지수) 편입 여부. SEIBro 표기는 'Hang Seng China Enterprises Index'."""
    for a in assets or []:
        n = ((a.get("name") or "") if isinstance(a, dict) else str(a)).upper()
        if "HANG SENG CHINA" in n or "HSCEI" in n or "홍콩" in n or "H지수" in n:
            return True
    return False


rows = []
for h in HistoricalIssue.objects.filter(
        issue_date__year=YEAR, verdict_met__isnull=False).only(
        "radar_tier", "verdict_met", "basset_sort", "ki",
        "stepdown_barriers", "assets", "sim_early_1y"):
    t = "지수형" if h.basset_sort == "지수" else "종목형"
    bars = h.stepdown_barriers or []
    rows.append({
        "tier": h.radar_tier or "",
        "hit": bool(h.verdict_met),
        "type": t,
        "b0": bars[0] if bars and isinstance(bars[0], (int, float)) else None,
        "hscei": has_hscei(h.assets),
            })

if len(rows) < MIN_N:
    print(f"{YEAR}년: 판정확정 {len(rows):,}건 — 최소 표본 {MIN_N}건 미달로 제외")
    sys.exit()


def rate(sel):
    n = len(sel)
    k = sum(1 for r in sel if r["hit"])
    return n, k, (k / n * 100 if n else None)


badge = [r for r in rows if r["tier"]]
ctrl = [r for r in rows if not r["tier"]]

print(f"===== {YEAR}년 레이더 검증 =====")
print(f"판정확정 {len(rows):,}건 / 배지 {len(badge):,} / 대조군(나머지 전부) {len(ctrl):,}")
print()

print("[1] 등급별")
for t in ("아주 강한 신호", "강한 신호"):
    n, k, p = rate([r for r in badge if r["tier"] == t])
    if n:
        print(f"  {t:12} {n:5,}건  성공 {k:5,}  {p:5.1f}%")
n, k, p = rate(badge)
print(f"  {'배지 합계':12} {n:5,}건  성공 {k:5,}  {p:5.1f}%")
n2, k2, p2 = rate(ctrl)
print(f"  {'대조군':12} {n2:5,}건  성공 {k2:5,}  {p2:5.1f}%   격차 {p - p2:+.1f}%p")
print()

print("[2] 유형별 (구성 차이 확인)")
print(f"  {'구분':10} {'배지':>18}  {'대조군':>20}   격차")
for t in ("종목형", "지수형"):
    bn, bk, bp = rate([r for r in badge if r["type"] == t])
    cn, ck, cp = rate([r for r in ctrl if r["type"] == t])
    if bn and cn:
        print(f"  {t:10} {bn:5,}건 {bp:5.1f}%   {cn:6,}건 {cp:5.1f}%   {bp - cp:+6.1f}%p")
bt = sum(1 for r in badge if r["type"] == "종목형")
ct = sum(1 for r in ctrl if r["type"] == "종목형")
if badge and ctrl:
    print(f"  종목형 비중 — 배지 {bt / len(badge) * 100:.1f}%  vs  대조군 {ct / len(ctrl) * 100:.1f}%")
print()

print("[3] HSCEI 조건별 비교 (지수형만)")
for flag, label in ((True, "HSCEI 포함"), (False, "HSCEI 없음")):
    bn, bk, bp = rate([r for r in badge if r["type"] == "지수형" and r["hscei"] is flag])
    cn, ck, cp = rate([r for r in ctrl if r["type"] == "지수형" and r["hscei"] is flag])
    if bn and cn:
        print(f"  {label:11} 배지 {bn:4,}건 {bp:5.1f}%   대조 {cn:5,}건 {cp:5.1f}%   {bp - cp:+6.1f}%p")
    elif bn or cn:
        print(f"  {label:11} 배지 {bn:4,}건   대조 {cn:5,}건  (한쪽 표본 없음)")
bh = sum(1 for r in badge if r["type"] == "지수형" and r["hscei"])
bi = sum(1 for r in badge if r["type"] == "지수형")
ch = sum(1 for r in ctrl if r["type"] == "지수형" and r["hscei"])
ci = sum(1 for r in ctrl if r["type"] == "지수형")
if bi and ci:
    print(f"  HSCEI 비중 — 배지 지수형 {bh / bi * 100:.1f}%  vs  대조 지수형 {ch / ci * 100:.1f}%")
print()

print("[4] 구성 보정 (대조군을 배지군과 같은 유형·배리어 구성으로 재계산)")


def band(b):
    return None if b is None else int(b // 5 * 5)


strata = defaultdict(list)
for r in ctrl:
    strata[(r["type"], band(r["b0"]))].append(r)
num = den = 0
miss = 0
for r in badge:
    key = (r["type"], band(r["b0"]))
    pool = strata.get(key)
    if not pool:
        miss += 1
        continue
    num += sum(1 for x in pool if x["hit"]) / len(pool)
    den += 1
if den:
    std = num / den * 100
    _, _, bp = rate(badge)
    print(f"  배지 {bp:.1f}%  vs  보정 대조군 {std:.1f}%   보정 격차 {bp - std:+.1f}%p")
    print(f"  (층 매칭 실패 {miss}건 제외, 커버리지 {den / len(badge) * 100:.0f}%)")
else:
    print("  층 매칭 불가")
