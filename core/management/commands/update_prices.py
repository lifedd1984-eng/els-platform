"""
보유 투자의 기초자산 시세를 조회해 낙인 거리(KnockInStatus)를 갱신하고,
위험 구간 진입 시 텔레그램 경보를 발송한다.

레벨(%) = 현재가 / 최초기준가 × 100
  기준가는 두 단계로 정한다 (market.pick_ref_price):
    ① SEIBro 공시 최초기준가격(std_price) — 발행사가 신고한 공식값. 있으면 이것.
    ② 없으면 폴백 — market.fallback_ref_basis()가 준 기준일 이하의 마지막 종가
       (설명서 확정값 base_eval_date 우선, 없으면 발행사별 규칙).
버퍼(%p) = 기준가 대비 % − 낙인  (가장 부진한 자산 기준)

위험 구간:
  - 버퍼 ≤ 5%p  → '위험'
  - 버퍼 ≤ 20%p → '경고'
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from core import market, push, telegram
from core.models import Investment, KnockInStatus, KnockInAlert


BANDS = [("위험", 5), ("경고", 20)]


class Command(BaseCommand):
    help = "기초자산 시세 조회 + 낙인 거리 갱신 + 위험 경보"

    def add_arguments(self, parser):
        parser.add_argument("--no-notify", action="store_true")

    def handle(self, *args, **opts):
        notify = not opts["no_notify"]
        holdings = Investment.objects.filter(status="보유중").select_related("product", "user")
        if not holdings:
            self.stdout.write("보유 상품 없음")
            return

        # 티커별 시세 캐시 (같은 티커 반복 조회 방지)
        current_cache = {}
        ref_cache = {}  # (ticker, 기준일, 오프셋) → 기준가

        for inv in holdings:
            p = inv.product
            # ① SEIBro 공시 기준가 (상품당 1회 조회)
            disclosed = market.disclosed_ref_prices(p)
            # ② 폴백 기준일 + 거래일 오프셋 — 규칙 교체는 fallback_ref_basis() 한 곳에서만
            base_date, back = market.fallback_ref_basis(p)
            if not base_date:
                base_date, back = inv.invested_at, 0
            for asset in market.split_assets(p.assets_raw):
                ticker = market.resolve_ticker(asset)
                cur = fallback = None
                if ticker:
                    if ticker not in current_cache:
                        current_cache[ticker] = market.fetch_current_price(ticker)
                    cur = current_cache[ticker]
                    if base_date:
                        key = (ticker, base_date, back)
                        if key not in ref_cache:
                            ref_cache[key] = market.fetch_price_on(ticker, base_date, back=back)
                        fallback = ref_cache[key]
                # 폴백 시세는 공시값 검증(정규화 기준점 걸러내기)에도 쓰이므로 항상 구한다
                ref, _src = market.pick_ref_price(disclosed.get(asset, (None, ""))[0], fallback)

                level = round(cur / ref * 100, 1) if (cur and ref) else None
                KnockInStatus.objects.update_or_create(
                    investment=inv, asset_name=asset,
                    defaults=dict(ticker=ticker or "", ref_price=ref,
                                  current_price=cur, level_pct=level),
                )

            self.stdout.write(
                f"[{inv.product.issuer} {inv.product.product_no}] "
                f"가장 부진한 자산 {getattr(inv.worst_ki_status, 'level_pct', None)}% "
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
        account = f" · {inv.broker_account}" if inv.broker_account else ""

        # 웹 푸시 — 계정별 채널이라 텔레그램 스코프·토글과 무관하게 항상 보낸다.
        # 2026-08-11 조 팀장 지시로 신설 — 전엔 이 알림이 텔레그램에만 있어서,
        # 같은 날 텔레그램을 끄면 이 경보를 알려줄 채널이 하나도 안 남았다.
        n_push = push.send_to_user(
            inv.user,
            f"[낙인 {band}] {inv.product.issuer} {inv.product.product_no}",
            f"기초자산 '{worst.asset_name}' 현재 레벨 {worst.level_pct}% "
            f"· KI배리어까지 {buffer}%p 남음",
            url="/portfolio/",
            tag=f"ki-{inv.id}-{band}",
            stdout=self.stdout,
        )
        self.stdout.write(f"  → 낙인 경보 웹 푸시: {band} ({n_push}건)")

        # 텔레그램 발송은 2026-08-11 조 팀장 지시로 비활성화(설정 기본값 꺼짐).
        if not settings.TELEGRAM_KNOCKIN_ALERT_ENABLED:
            return
        if not telegram.is_alert_target(inv.user):
            return
        telegram.send_message(
            f"[낙인 {band}] {inv.product.issuer} {inv.product.product_no}\n"
            f"투자금액 {inv.amount:,}원{account}\n"
            f"기초자산 '{worst.asset_name}' 현재 레벨 {worst.level_pct}%\n"
            f"KI배리어 {inv.product.ki}% 까지 {buffer}%p 남음\n"
            f"대시보드: {settings.SITE_URL}/portfolio/"
        )
        self.stdout.write(f"  → 낙인 경보 텔레그램 발송: {band}")
