"""
관심상품 청약마감 D-1 텔레그램 알림. 매일 아침 배치에서 호출.
같은 날 중복 발송은 logs/deadline_YYYYMMDD.flag 마커로 방지(send_digest 패턴).
"""

from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.notify import notify_watchlist_deadline


class Command(BaseCommand):
    help = "관심상품 청약마감 D-1 알림 (같은 날 1회)"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="중복 마커 무시하고 발송")

    def handle(self, *args, **opts):
        today = date.today()
        marker_dir = Path(settings.BASE_DIR) / "logs"
        marker_dir.mkdir(exist_ok=True)
        marker = marker_dir / f"deadline_{today:%Y%m%d}.flag"

        if not opts["force"] and marker.exists():
            self.stdout.write("오늘 이미 발송됨 - 생략")
            return

        notify_watchlist_deadline(stdout=self.stdout)
        marker.write_text(f"{today}")
