"""
프리셋 매칭 / 상환 평가일 / 관심 마감 알림.

채널: 가족 공용 텔레그램(기존) + 사용자별 웹 푸시(core.push).
호출처: notify_preset_matches — import_els·scrape_kofia(새벽 배치),
        notify_redemptions·notify_watchlist_deadline — send_redemption_alert·
        send_deadline_alert(아침 8시 크론, 새벽 푸시 방지).
"""

from datetime import date, timedelta

from django.conf import settings

from core import push, telegram
from core.models import (
    Investment, NotifiedMatch, Preset, Product, RedemptionAlert, WatchItem,
)


def _alert_scope_holdings():
    """텔레그램에 실을 보유 투자 — 지정 계정 것만(설정이 비면 전 계정).

    개별 알림(조기상환 판정·평가일 D-1·낙인 근접)과 같은 기준을 주간 요약에도
    쓴다. 요약 안에 여러 계정 투자가 섞여 나열되던 것을 2026-08-10 조 팀장 지시로
    맞췄다. 배치 보고·헬스체크는 서비스 전체 상태라 이 제한을 걸지 않는다.
    """
    qs = Investment.objects.filter(status="보유중").select_related("product")
    names = telegram.alert_usernames()
    return qs.filter(user__username__in=names) if names else qs


def notify_preset_matches(stdout=None):
    """신규 프리셋 매칭 상품 알림. NotifiedMatch로 중복 발송 방지."""
    today = date.today()
    for preset in Preset.objects.filter(notify=True, user__isnull=True):  # 가족 공용만 (개인 프리셋은 텔레그램 미발송)
        # 원금지급형(ELB·DLB) 제외는 match_queryset 안에서 걸린다 — 화면의
        # 프리셋 필터와 같은 함수를 타므로 목록과 알림이 어긋날 수 없다.
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
                f"낙인{p.ki_display} {p.assets_raw[:20]} ~{p.sub_end:%m.%d}"
            )
        if len(new_matches) > 10:
            lines.append(f"... 외 {len(new_matches)-10}건")
        lines.append(f"대시보드: {settings.SITE_URL}")
        # 텔레그램 발송은 2026-08-11 조 팀장 지시로 비활성화(설정 기본값 꺼짐).
        # NotifiedMatch 기록은 껐을 때도 남긴다 — 안 남기면 매 배치가 같은 매칭을
        # 신규로 다시 세고, 나중에 다시 켰을 때 밀린 건이 한꺼번에 쏟아진다.
        enabled = settings.TELEGRAM_PRESET_MATCH_ALERT_ENABLED
        recorded = telegram.send_message("\n".join(lines)) if enabled else True
        if recorded:
            NotifiedMatch.objects.bulk_create(
                [NotifiedMatch(preset=preset, product=p) for p in new_matches],
                ignore_conflicts=True,
            )
            if stdout:
                state = "발송" if enabled else "기록만 (텔레그램 발송 꺼짐)"
                stdout.write(f"[알림] {preset.name}: {len(new_matches)}건 {state}")


def notify_redemptions(stdout=None):
    """보유 투자 평가일 D-1 알림. RedemptionAlert로 중복 발송 방지.

    D-7은 2026-08-11 조 팀장 지시로 없앴다 — 너무 일러서 실효 알림이 아니었다.
    """
    today = date.today()
    for inv in Investment.objects.filter(status="보유중").select_related("product", "user"):
        nxt = inv.next_evaluation
        if not nxt:
            continue
        days_left = (nxt["date"] - today).days
        if days_left != 1:
            continue
        alert_type = "D-1"
        _, created = RedemptionAlert.objects.get_or_create(
            investment=inv, round_no=nxt["n"], alert_type=alert_type
        )
        if not created:
            continue
        expected = f"{nxt['expected']:,}원" if nxt["expected"] else "-"
        # 텔레그램은 공용 채널이라 지정 계정 것만 보낸다. 웹 푸시는 계정별로 가므로
        # 아래 send_to_user는 제한하지 않는다 — 다른 계정도 본인 알림은 계속 받는다.
        # 텔레그램 발송 자체는 2026-08-11 조 팀장 지시로 비활성화(설정 기본값 꺼짐).
        # 위 RedemptionAlert 기록과 아래 웹 푸시는 그대로 돈다.
        if settings.TELEGRAM_REDEMPTION_ALERT_ENABLED and telegram.is_alert_target(inv.user):
            account = f" · {inv.broker_account}" if inv.broker_account else ""
            telegram.send_message(
                f"[상환 평가 {alert_type}] {inv.product.issuer} {inv.product.product_no}\n"
                f"투자금액 {inv.amount:,}원{account}\n"
                f"{nxt['n']}회차 평가일: {nxt['date']:%Y-%m-%d}\n"
                f"배리어: {nxt['barrier'] or '-'}% / 예상상환금: {expected}"
            )
        n_push = push.send_to_user(
            inv.user,
            f"[상환 평가 {alert_type}] {inv.product.issuer} {inv.product.product_no}",
            f"{nxt['n']}회차 평가일 {nxt['date']:%Y-%m-%d} · 배리어 {nxt['barrier'] or '-'}% "
            f"· 예상상환 {expected}",
            url="/portfolio/",
            tag=f"redeem-{inv.id}-{nxt['n']}-{alert_type}",
            stdout=stdout,
        )
        if stdout:
            stdout.write(f"[상환알림] {inv} {alert_type} (푸시 {n_push}건)")


def notify_watchlist_deadline(stdout=None):
    """관심상품 중 내일 청약마감 상품 알림.

    ① 웹 푸시 — 계정별 (본인 관심상품 기준, 본인이 이미 보유중인 상품 제외)
    ② 텔레그램 — 가족 공용 (전 계정 합집합, 누군가 보유중인 상품 제외)
       2026-08-11 조 팀장 지시로 비활성화. ①은 그대로 나간다.
    숙려대상자(65세+)는 마감 2영업일 전까지 청약해야 하므로 D-1에 안내.
    """
    tomorrow = date.today() + timedelta(days=1)
    watches = list(
        WatchItem.objects.filter(product__sub_end=tomorrow).select_related("product", "user")
    )

    # ── ① 사용자별 웹 푸시 ──
    by_user = {}
    for w in watches:
        if w.user_id:
            by_user.setdefault(w.user, []).append(w.product)
    for user, prods in by_user.items():
        held_u = set(
            Investment.objects.filter(user=user, status="보유중").values_list("product_id", flat=True)
        )
        prods = [p for p in prods if p.id not in held_u]
        if not prods:
            continue
        prods.sort(key=lambda p: -(p.yield_rate or 0))
        top = prods[0]
        y = f"{top.yield_rate:g}" if top.yield_rate is not None else "-"
        body = f"{top.issuer} {top.product_no} 연 {y}%"
        if len(prods) > 1:
            body += f" 외 {len(prods) - 1}건"
        body += f" — 내일({tomorrow.month}/{tomorrow.day}) 청약 마감"
        n_push = push.send_to_user(
            user, f"[청약 마감 D-1] 관심상품 {len(prods)}건", body,
            url="/watchlist/", tag=f"deadline-{tomorrow:%Y%m%d}", stdout=stdout,
        )
        if n_push and stdout:
            stdout.write(f"[마감푸시] {user.username}: {len(prods)}건 → 기기 {n_push}대")

    # ── ② 가족 공용 텔레그램 ──
    # 2026-08-11 조 팀장 지시로 비활성화(설정 기본값 꺼짐). 위 ① 웹 푸시는
    # 계정별 채널이라 그대로 나간다. 여기서 바로 빠지는 이유는 아래 집계가
    # 텔레그램 메시지를 만들기 위한 것뿐이라 계산할 필요조차 없어서다.
    if not settings.TELEGRAM_WATCHLIST_DEADLINE_ALERT_ENABLED:
        return
    held = set(
        Investment.objects.filter(status="보유중").values_list("product_id", flat=True)
    )
    seen = {}
    for w in watches:
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
        # v7은 배지명이 "타겟 신호" 하나 — 구 배지명(아주 강한/강한) 비교는
        # 항상 거짓이라 타겟 상품이 강조되지 않고 있었다 (2026-08-03)
        badge = " · \U0001F535타겟" if p.radar else ""
        y = f"{p.yield_rate:g}" if p.yield_rate is not None else "-"
        lines.append(f"⏰ {p.issuer} {p.product_no} — 연 {y}% · 낙인{p.ki_display}{badge}")
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

    # ① 이번주 레이더 TOP5 — v7 트랙별 (사이트 추천과 동일 기준, radar_tracks 공용)
    from core.models import radar_tracks
    tracks = radar_tracks()
    lines.append("\n📡 이번주 타겟 신호 TOP5 (10년 검증 규칙)")
    for tier, head in (("지수형", "🛡 지수형"), ("종목형", "🚀 종목형")):
        items = tracks.get(tier, [])
        lines.append(head)
        if not items:
            lines.append("  이번 주 조건 통과 상품 없음")
            continue
        for i, p in enumerate(items, 1):
            lines.append(
                f"  {i}. {p.issuer} {p.product_no} 연 {p.yield_rate:g}% "
                f"· 낙인 {p.ki} · ~{p.sub_end.month}/{p.sub_end.day}"
            )

    # ② 향후 7일 보유상품 평가예정 (알림 대상 계정 보유분만)
    upcoming = []
    for inv in _alert_scope_holdings():
        nxt = inv.next_evaluation
        if nxt and today <= nxt["date"] <= week_end:
            upcoming.append((nxt["date"], inv, nxt))
    lines.append(f"\n▶ 7일내 조기상환 평가 {len(upcoming)}건")
    for d, inv, nxt in sorted(upcoming, key=lambda x: x[0])[:5]:
        lines.append(f"- {d:%m.%d} {inv.product.issuer} {inv.product.product_no} "
                     f"{nxt['n']}회차 배리어 {nxt['barrier'] or '-'}%")
    if len(upcoming) > 5:
        lines.append(f"... 외 {len(upcoming)-5}건")

    # ③ 낙인 현황 (update_prices가 채운 KnockInStatus 기반, 알림 대상 계정 보유분만)
    danger = warn = safe = 0
    for inv in _alert_scope_holdings():
        buf = inv.ki_buffer
        if buf is None:
            continue
        if buf <= 5:
            danger += 1
        elif buf <= 20:
            warn += 1
        else:
            safe += 1
    lines.append(f"\n▶ 낙인 현황: 위험 {danger} / 경고 {warn} / 안전 {safe}")
    lines.append(f"\n대시보드: {settings.SITE_URL}")

    if telegram.send_message("\n".join(lines)) and stdout:
        stdout.write("[주간요약] 발송 완료")
