"""
지난 평가일 조기상환 판정: 평가일 종가로 워스트 레벨을 계산해
배리어 충족 여부를 RedemptionVerdict에 기록하고, 충족 시 텔레그램 알림.

가장 최근에 지난 회차만 판정한다(그 이전 회차에서 상환됐다면
사용자가 이미 상환 처리를 했을 것이므로).
"""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core import market, telegram
from core.models import Investment, RedemptionVerdict

# 확정되지 않은 상환예정 건을 다시 알리기까지의 영업일수.
# 최초 1회만 알리던 탓에 2026-08-03 판정된 키움 1863(inv694)이 확정되지 않은 채
# 일주일간 방치됐다. 그 사고의 직접 원인이라 재알림을 기본 동작으로 둔다.
REMIND_BUSINESS_DAYS = 5


class Command(BaseCommand):
    help = "지난 평가일 조기상환 충족 여부 판정 + 알림(미확정 건 재알림 포함)"

    def add_arguments(self, parser):
        parser.add_argument("--no-notify", action="store_true")
        parser.add_argument("--recheck", action="store_true",
                            help="시세 미확보로 판정불가였던 회차 재시도")
        parser.add_argument("--remind-days", type=int, default=REMIND_BUSINESS_DAYS,
                            help="미확정 상환예정 건 재알림 주기(영업일)")

    def handle(self, *args, **opts):
        notify = not opts["no_notify"]
        today = date.today()
        checked = met_cnt = 0
        price_cache = {}

        def price_on(ticker, d, back=0):
            """d 시점 종가. back은 기준가 조회에만 쓴다(평가일 시세는 항상 back=0)."""
            key = (ticker, d, back)
            if key not in price_cache:
                price_cache[key] = market.fetch_price_on(ticker, d, back=back)
            return price_cache[key]

        for inv in Investment.objects.filter(status="보유중").select_related("product", "user"):
            past = [r for r in inv.schedule if r["date"] < today]
            if not past:
                continue
            row = past[-1]  # 가장 최근 지난 회차

            existing = RedemptionVerdict.objects.filter(
                investment=inv, round_no=row["n"]
            ).first()
            if existing and not (opts["recheck"] and existing.met is None):
                continue

            p = inv.product
            # 기준가 산정일 + 거래일 오프셋 (설명서 평가일 우선, 없으면 발행사 규칙)
            base, base_back = market.base_price_date(p)
            if not base:
                base, base_back = inv.invested_at, 0
            worst_level = None
            worst_asset = ""
            judgeable = base is not None and row["barrier"] is not None
            if judgeable:
                for asset in market.split_assets(p.assets_raw):
                    ticker = market.resolve_ticker(asset)
                    ref = price_on(ticker, base, back=base_back) if ticker else None
                    # 평가일 시세는 오프셋 없이 당일 종가 그대로
                    ev = price_on(ticker, row["date"]) if ticker else None
                    if not (ref and ev):
                        worst_level = None
                        break
                    level = round(ev / ref * 100, 1)
                    if worst_level is None or level < worst_level:
                        worst_level, worst_asset = level, asset

            met = None
            if worst_level is not None:
                met = worst_level >= row["barrier"]

            verdict, created = RedemptionVerdict.objects.update_or_create(
                investment=inv, round_no=row["n"],
                defaults=dict(eval_date=row["date"], barrier=row["barrier"],
                              worst_level=worst_level, worst_asset=worst_asset,
                              met=met),
            )
            checked += 1
            label = {True: "충족(상환예정)", False: "미충족", None: "판정불가"}[met]
            self.stdout.write(
                f"[{p.issuer} {p.product_no}] {row['n']}회차 {row['date']} "
                f"배리어 {row['barrier']}% / 워스트 {worst_level}% -> {label}"
            )

            if met:
                met_cnt += 1
                if notify and created and telegram.is_alert_target(inv.user):
                    sent = telegram.send_message(_alert_text(
                        inv, row["n"], row["date"], row["barrier"],
                        worst_level, row.get("expected"),
                    ))
                    if sent:
                        now = timezone.now()
                        verdict.notified_at = now
                        verdict.last_notified_at = now
                        verdict.save(update_fields=["notified_at", "last_notified_at"])

        self.stdout.write(f"판정 {checked}건 / 상환예정 {met_cnt}건")

        if notify:
            self._remind_pending(today, opts["remind_days"])

    def _remind_pending(self, today, remind_days):
        """확정되지 않은 상환예정 건 재알림.

        판정 루프는 이미 판정한 회차를 건너뛰므로(중복 판정 방지) 알림도 최초 1회로
        끝났다 — 조용히 잊히는 경로가 여기였다. 확정(=상태가 보유중에서 바뀜)될
        때까지 remind_days 영업일마다 다시 알린다.
        """
        sent_cnt = 0
        for v in (RedemptionVerdict.objects
                  .filter(met=True, investment__status="보유중")
                  .select_related("investment__product", "investment__user")):
            inv = v.investment
            if not telegram.is_alert_target(inv.user):
                continue
            last = v.last_notified_at or v.notified_at
            if last is None:
                # 최초 알림이 나간 적 없는 건(이 기능 이전에 쌓인 행). 재알림 기준을
                # 판정 시각으로 잡아 방치분이 영영 안 알려지는 일이 없게 한다.
                last = v.checked_at
            if last is None:
                continue
            elapsed = market.business_days_between(timezone.localtime(last).date(), today)
            if elapsed < remind_days:
                continue
            sched = inv.schedule
            expected = (sched[v.round_no - 1]["expected"]
                        if 1 <= v.round_no <= len(sched) else None)
            if telegram.send_message(_alert_text(
                inv, v.round_no, v.eval_date, v.barrier, v.worst_level, expected,
                remind_days=elapsed,
            )):
                v.last_notified_at = timezone.now()
                if v.notified_at is None:
                    v.notified_at = v.last_notified_at
                v.save(update_fields=["notified_at", "last_notified_at"])
                sent_cnt += 1
        if sent_cnt:
            self.stdout.write(f"미확정 재알림 {sent_cnt}건")


def _alert_text(inv, round_no, eval_date, barrier, worst_level, expected,
                remind_days=None):
    """조기상환 판정 알림 문구. 최초·재알림이 같은 함수를 쓴다.

    발행사+상품번호만 적던 옛 문구는 같은 상품을 두 건 보유하면 구분이 안 됐다 —
    2026-08-03 키움 1863 두 건 중 한 건만 처리되고 나머지가 방치된 원인이다.
    계좌 메모와 투자금액을 넣어 건을 특정할 수 있게 한다.
    """
    p = inv.product
    exp_txt = f"{expected:,}원" if expected else "-"
    who = f" · {inv.broker_account}" if inv.broker_account else ""
    head = ("[조기상환 예정]" if remind_days is None
            else f"[조기상환 미확정 {remind_days}영업일]")
    return (
        f"{head} {p.issuer} {p.product_no}\n"
        f"투자금액 {inv.amount:,}원{who}\n"
        f"{round_no}회차({eval_date:%m.%d}) 조기상환 기준 {barrier}% 충족\n"
        f"가장 부진한 자산 {worst_level}% (기준가 대비)\n"
        f"예상상환금: {exp_txt}\n"
        f"증권사 확인 후 포트폴리오에서 상환 처리하세요.\n"
        f"{settings.SITE_URL}/portfolio/"
    )
