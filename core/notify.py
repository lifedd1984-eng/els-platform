"""
프리셋 매칭 / 상환 평가일 텔레그램 알림 (import_els, scrape_kofia 공용).
"""

from datetime import date, timedelta

from django.conf import settings

from core import telegram
from core.models import (
    Investment, NotifiedMatch, Preset, Product, RedemptionAlert, WatchItem,
)


def notify_preset_matches(stdout=None):
    """신규 프리셋 매칭 상품 알림. NotifiedMatch로 중복 발송 방지."""
    today = date.today()
    for preset in Preset.objects.filter(notify=True, user__isnull=True):  # 가족 공용만 (개인 프리셋은 텔레그램 미발송)
        matches = preset.match_queryset(Product.objects.filter(sub_end__gte=today))
        already = set(
            NotifiedMatch.objects.filter(preset=preset).values_list("product_id", flat=True)
        )
        new_matches = [p for p in matches if p.id not in already]
        if not new_matches:
            continue
        lines = [f"[프리셋 매칭] {preset.name} — 신규 {len(new_matches)}건"]
        for p in new_matches[:10]:
            lines.append(
                f"- {p.issuer} {p.product_no} ({p.yield_rate}%) "
                f"KI{p.ki_display} {p.assets_raw[:20]} ~{p.sub_end:%m.%d}"
            )
        if len(new_matches) > 10:
            lines.append(f"... 외 {len(new_matches)-10}건")
        lines.append(f"대시보드: {settings.SITE_URL}")
        if telegram.send_message("\n".join(lines)):
            NotifiedMatch.objects.bulk_create(
                [NotifiedMatch(preset=preset, product=p) for p in new_matches],
                ignore_conflicts=True,
            )
            if stdout:
                stdout.write(f"[알림] {preset.name}: {len(new_matches)}건 발송")


def notify_redemptions(stdout=None):
    """보유 투자 평가일 D-7/D-1 알림. RedemptionAlert로 중복 발송 방지."""
    today = date.today()
    for inv in Investment.objects.filter(status="보유중").select_related("product"):
        nxt = inv.next_evaluation
        if not nxt:
            continue
        days_left = (nxt["date"] - today).days
        alert_type = None
        if days_left == 7:
            alert_type = "D-7"
        elif days_left == 1:
            alert_type = "D-1"
        if not alert_type:
            continue
        _, created = RedemptionAlert.objects.get_or_create(
            investment=inv, round_no=nxt["n"], alert_type=alert_type
        )
        if not created:
            continue
        expected = f"{nxt['expected']:,}원" if nxt["expected"] else "-"
        telegram.send_message(
            f"[상환 평가 {alert_type}] {inv.product.issuer} {inv.product.product_no}\n"
            f"{nxt['n']}회차 평가일: {nxt['date']:%Y-%m-%d}\n"
            f"배리어: {nxt['barrier'] or '-'}% / 예상상환금: {expected}"
        )
        if stdout:
            stdout.write(f"[상환알림] {inv} {alert_type}")


def notify_watchlist_deadline(stdout=None):
    """관심상품 중 내일 청약마감 상품 알림 (전 계정 합집합, 상품 중복 제거).

    이미 보유중(Investment status='보유중')인 상품은 제외. 0건이면 발송하지 않는다.
    숙려대상자(65세+)는 마감 2영업일 전까지 청약해야 하므로 D-1에 안내.
    """
    tomorrow = date.today() + timedelta(days=1)
    held = set(
        Investment.objects.filter(status="보유중").values_list("product_id", flat=True)
    )
    seen = {}
    for w in WatchItem.objects.filter(product__sub_end=tomorrow).select_related("product"):
        p = w.product
        if p.id in held or p.id in seen:
            continue
        seen[p.id] = p
    items = sorted(seen.values(), key=lambda p: -(p.yield_rate or 0))
    if not items:
        if stdout:
            stdout.write("[마감알림] 대상 없음 - 발송 생략")
        return

    lines = [
        f"[청약 마감 임박] 관심 상품 {len(items)}건이 내일"
        f"({tomorrow.month}/{tomorrow.day}) 마감됩니다",
        "",
    ]
    for p in items:
        badge = ""
        r = p.radar
        if r and r["tier"] == "아주 강한 신호":
            badge = " · \U0001F535아주 강한"
        elif r and r["tier"] == "강한 신호":
            badge = " · \U0001F535강한"
        y = f"{p.yield_rate:g}" if p.yield_rate is not None else "-"
        lines.append(f"⏰ {p.issuer} {p.product_no} — 연 {y}% · KI{p.ki_display}{badge}")
    lines.append("")
    lines.append("숙려대상자(65세+)는 오늘까지 청약해야 합니다.")
    site = settings.SITE_URL.split("://")[-1]
    lines.append(f"\U0001F449 {site}/watchlist")

    if telegram.send_message("\n".join(lines)) and stdout:
        stdout.write(f"[마감알림] {len(items)}건 발송")


def notify_weekly_digest(stdout=None):
    """주간 요약: 이번주 마감 고수익 TOP5 + 향후 7일 평가예정 + 낙인 현황."""
    today = date.today()
    week_end = today + timedelta(days=6)

    weekday = "월화수목금토일"[today.weekday()]
    lines = [f"[주간 요약] {today:%m.%d}({weekday})"]

    # ① 이번주 레이더 TOP5 (사이트 추천과 동일 기준 — radar_top5 공용)
    from core.models import radar_top5, _radar_early
    top = radar_top5()
    lines.append("\n📡 이번주 레이더 TOP5")
    lines.append("(아주 강한 신호 · 손실확률 0% · 1년내 상환 90%↑)")
    if not top:
        lines.append("이번주 TOP5 기준 통과 상품 없음")
    else:
        for i, p in enumerate(top, 1):
            early = round(_radar_early(p))
            lines.append(
                f"{i}. {p.issuer} {p.product_no} [{p.asset_type}] "
                f"연 {p.yield_rate:g}% · 1년내 {early}% · ~{p.sub_end.month}/{p.sub_end.day}"
            )

    # ② 향후 7일 보유상품 평가예정
    upcoming = []
    for inv in Investment.objects.filter(status="보유중").select_related("product"):
        nxt = inv.next_evaluation
        if nxt and today <= nxt["date"] <= week_end:
            upcoming.append((nxt["date"], inv, nxt))
    lines.append(f"\n▶ 7일내 조기상환 평가 {len(upcoming)}건")
    for d, inv, nxt in sorted(upcoming, key=lambda x: x[0])[:5]:
        lines.append(f"- {d:%m.%d} {inv.product.issuer} {inv.product.product_no} "
                     f"{nxt['n']}회차 배리어 {nxt['barrier'] or '-'}%")
    if len(upcoming) > 5:
        lines.append(f"... 외 {len(upcoming)-5}건")

    # ③ 낙인 현황 (update_prices가 채운 KnockInStatus 기반)
    danger = warn = safe = 0
    for inv in Investment.objects.filter(status="보유중").select_related("product"):
        buf = inv.ki_buffer
        if buf is None:
            continue
        if buf <= 5:
            danger += 1
        elif buf <= 15:
            warn += 1
        else:
            safe += 1
    lines.append(f"\n▶ 낙인 현황: 위험 {danger} / 경고 {warn} / 안전 {safe}")
    lines.append(f"\n대시보드: {settings.SITE_URL}")

    if telegram.send_message("\n".join(lines)) and stdout:
        stdout.write("[주간요약] 발송 완료")
