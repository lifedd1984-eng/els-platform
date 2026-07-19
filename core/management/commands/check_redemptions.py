"""
지난 평가일 조기상환 판정: 평가일 종가로 워스트 레벨을 계산해
배리어 충족 여부를 RedemptionVerdict에 기록하고, 충족 시 텔레그램 알림.

가장 최근에 지난 회차만 판정한다(그 이전 회차에서 상환됐다면
사용자가 이미 상환 처리를 했을 것이므로).
"""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand

from core import market, telegram
from core.models import Investment, RedemptionVerdict


class Command(BaseCommand):
    help = "지난 평가일 조기상환 충족 여부 판정 + 알림"

    def add_arguments(self, parser):
        parser.add_argument("--no-notify", action="store_true")
        parser.add_argument("--recheck", action="store_true",
                            help="시세 미확보로 판정불가였던 회차 재시도")

    def handle(self, *args, **opts):
        notify = not opts["no_notify"]
        today = date.today()
        checked = met_cnt = 0
        price_cache = {}

        def price_on(ticker, d):
            key = (ticker, d)
            if key not in price_cache:
                price_cache[key] = market.fetch_price_on(ticker, d)
            return price_cache[key]

        for inv in Investment.objects.filter(status="보유중").select_related("product"):
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
            base = p.issue_date or inv.invested_at
            worst_level = None
            worst_asset = ""
            judgeable = base is not None and row["barrier"] is not None
            if judgeable:
                for asset in market.split_assets(p.assets_raw):
                    ticker = market.resolve_ticker(asset)
                    ref = price_on(ticker, base) if ticker else None
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
                if notify and created:
                    expected = row.get("expected")
                    exp_txt = f"{expected:,}원" if expected else "-"
                    telegram.send_message(
                        f"[조기상환 예정] {p.issuer} {p.product_no}\n"
                        f"{row['n']}회차({row['date']:%m.%d}) 배리어 {row['barrier']}% 충족 "
                        f"(워스트 {worst_level}%)\n"
                        f"예상상환금: {exp_txt}\n"
                        f"증권사 확인 후 포트폴리오에서 상환 처리하세요.\n"
                        f"{settings.SITE_URL}/portfolio/"
                    )

        self.stdout.write(f"판정 {checked}건 / 상환예정 {met_cnt}건")
