"""
보유상품 조기상환 평가일 D-7/D-1 알림 (텔레그램 + 사용자별 웹 푸시).

아침 크론(morning.sh, 08:00 KST)에서 호출 — 새벽 배치(scrape_kofia)에서 분리해
웹 푸시가 한밤중에 울리지 않게 한다. 중복 발송은 RedemptionAlert가 막아준다.
"""

from django.core.management.base import BaseCommand

from core.notify import notify_redemptions


class Command(BaseCommand):
    help = "상환 평가일 D-7/D-1 알림 발송 (아침 크론용, RedemptionAlert로 중복 방지)"

    def handle(self, *args, **opts):
        notify_redemptions(stdout=self.stdout)
        self.stdout.write("상환 평가일 알림 점검 완료")
