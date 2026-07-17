"""
보유 투자의 기초자산 시세를 조회해 낙인 거리(KnockInStatus)를 갱신하고,
위험 구간 진입 시 텔레그램 경보를 발송한다.

레벨(%) = 현재가 / 발행일 기준가 × 100
버퍼(%p) = 레벨 - KI배리어  (워스트오브 = 가장 낮은 자산 기준)

위험 구간:
  - 버퍼 ≤ 5%p  → '위험'
  - 버퍼 ≤ 15%p → '경고'
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from core import market, telegram
from core.models import Investment, KnockInStatus, KnockInAlert


BANDS = [("위험", 5), ("경고", 15)]


class Command(BaseCommand):
    help = "기초자산 시세 조회 + 낙인 거리 갱신 + 위험 경보"

    def add_arguments(self, parser):
        parser.add_argument("--no-notify", action="store_true")

    def handle(self, *args, **opts):
        notify = not opts["no_notify"]
        holdings = Investment.objects.filter(status="보유중").select_related("product")
        if not holdings:
            self.stdout.write("보유 상품 없음")
            return

        # 티커별 시세 캐시 (같은 티커 반복 조회 방지)
        current_cache = {}
        ref_cache = {}  # (ticker, issue_date) → 기준가

        for inv in holdings:
            p = inv.product
            base_date = p.issue_date or inv.invested_at
            for asset in market.split_assets(p.assets_raw):
                ticker = market.resolve_ticker(asset)
                cur = ref = None
                if ticker:
                    if ticker not in current_cache:
                        current_cache[ticker] = market.fetch_current_price(ticker)
                    cur = current_cache[ticker]
                    if base_date:
                        key = (ticker, base_date)
                        if key not in ref_cache:
                            ref_cache[key] = market.fetch_price_on(ticker, base_date)
                        ref = ref_cache[key]

                level = round(cur / ref * 100, 1) if (cur and ref) else None
                KnockInStatus.objects.update_or_create(
                    investment=inv, asset_name=asset,
                    defaults=dict(ticker=ticker or "", ref_price=ref,
                                  current_price=cur, level_pct=level),
                )

            self.stdout.write(
                f"[{inv.product.issuer} {inv.product.product_no}] "
                f"워스트 레벨 {getattr(inv.worst_ki_status, 'level_pct', None)}% "
                f"/ KI버퍼 {inv.ki_buffer}%p"
            )

            if notify:
                self._maybe_alert(inv)

    def _maybe_alert(self, inv):
        buffer = inv.ki_buffer
        if buffer is None:
            return
        band = None
        for name, threshold in BANDS:
            if buffer <= threshold:
                band = name
                break
        if not band:
            return
        _, created = KnockInAlert.objects.get_or_create(investment=inv, level_band=band)
        if not created:
            return
        worst = inv.worst_ki_status
        telegram.send_message(
            f"[낙인 {band}] {inv.product.issuer} {inv.product.product_no}\n"
            f"기초자산 '{worst.asset_name}' 현재 레벨 {worst.level_pct}%\n"
            f"KI배리어 {inv.product.ki}% 까지 {buffer}%p 남음\n"
            f"대시보드: {settings.SITE_URL}/portfolio/"
        )
        self.stdout.write(f"  → 낙인 경보 발송: {band}")
