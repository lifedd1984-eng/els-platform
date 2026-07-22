"""
레이더 신호 성과 검증: 과거 주차의 배지 상품(및 대조군)의 실제 1차 조기상환
결과를 시세로 판정해 RadarVerdict에 기록한다.

판정 로직은 check_redemptions와 동일 — 발행일 기준가 대비 1차 평가일 종가 비율(%)의
최소값(워스트) vs 1차 배리어. 시세는 커맨드 내 캐시로 중복 조회를 막는다.
평가일이 아직 안 왔거나 시세 미확보면 met=None으로 저장하고 다음 실행 때 재시도한다.
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand

from core import market
from core.models import (
    RADAR_KI_EXCL, Product, RadarVerdict, _add_months, _radar_pool,
)


class Command(BaseCommand):
    help = "과거 주차 레이더 배지 상품의 1차 조기상환 적중 여부 판정"

    def add_arguments(self, parser):
        parser.add_argument("--weeks", type=int, default=26,
                            help="현재 주 제외, 과거 몇 주를 검증할지 (기본 26)")

    def handle(self, *args, **opts):
        weeks = opts["weeks"]
        today = date.today()
        cur_monday = today - timedelta(days=today.weekday())

        price_cache = {}

        def price_on(ticker, d):
            key = (ticker, d)
            if key not in price_cache:
                price_cache[key] = market.fetch_price_on(ticker, d)
            return price_cache[key]

        collected = judged = met_cnt = skipped = 0

        for i in range(1, weeks + 1):
            monday = cur_monday - timedelta(weeks=i)
            sunday = monday + timedelta(days=6)

            for asset_type in RADAR_KI_EXCL:  # 지수형 / 종목형
                pool = _radar_pool(monday, asset_type)  # {pid: {tier, ...}} 배지 대상만
                group = Product.objects.filter(
                    sub_end__gte=monday, sub_end__lte=sunday,
                    asset_type=asset_type, loss_prob__isnull=False,
                    barriers_raw__isnull=False,
                )
                for p in group:
                    r = pool.get(p.id)
                    tier = r["tier"] if r else "없음"  # 배지 없으면 대조군

                    # 1차 평가일 = 발행일 + (first_eval_months 또는 period_months)개월
                    first_months = p.first_eval_months or p.period_months
                    if not p.issue_date or not first_months:
                        continue  # 스케줄 산정 불가 → 검증 대상 제외
                    eval_date = _add_months(p.issue_date, first_months)
                    barrier = p.barrier_first
                    if barrier is None and p.barriers_raw:
                        barrier = p.barriers_raw[0]

                    existing = RadarVerdict.objects.filter(product=p).first()
                    if existing and existing.met is not None:
                        skipped += 1
                        continue  # 이미 확정 판정 → 재계산 안 함

                    collected += 1

                    worst_level = None
                    met = None
                    if eval_date <= today and barrier is not None:
                        base = p.issue_date
                        for asset in market.split_assets(p.assets_raw):
                            ticker = market.resolve_ticker(asset)
                            ref = price_on(ticker, base) if ticker else None
                            ev = price_on(ticker, eval_date) if ticker else None
                            if not (ref and ev):
                                worst_level = None
                                break
                            level = round(ev / ref * 100, 1)
                            if worst_level is None or level < worst_level:
                                worst_level = level
                        if worst_level is not None:
                            met = worst_level >= barrier

                    RadarVerdict.objects.update_or_create(
                        product=p,
                        defaults=dict(
                            tier=tier, week_monday=monday, eval_date=eval_date,
                            barrier=barrier, worst_level=worst_level, met=met,
                        ),
                    )
                    if met is not None:
                        judged += 1
                        if met:
                            met_cnt += 1
                    label = {True: "적중", False: "미충족", None: "대기/미확보"}[met]
                    self.stdout.write(
                        f"[{monday} {asset_type}] {p.issuer} {p.product_no} "
                        f"({tier}) 평가 {eval_date} 배리어 {barrier} "
                        f"워스트 {worst_level} -> {label}"
                    )

        self.stdout.write(
            f"수집 {collected}건 / 판정확정 {judged}건 / 적중 {met_cnt}건 / 스킵 {skipped}건"
        )
