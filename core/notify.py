"""
프리셋 매칭 / 상환 평가일 텔레그램 알림 (import_els, scrape_kofia 공용).
"""

from datetime import date

from django.conf import settings

from core import telegram
from core.models import Investment, NotifiedMatch, Preset, Product, RedemptionAlert


def notify_preset_matches(stdout=None):
    """신규 프리셋 매칭 상품 알림. NotifiedMatch로 중복 발송 방지."""
    today = date.today()
    for preset in Preset.objects.filter(notify=True):
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
