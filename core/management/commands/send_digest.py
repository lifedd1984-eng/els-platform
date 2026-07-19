"""
주간 요약 텔레그램 발송. 월요일에만 발송(--force로 강제).
같은 주 중복 발송은 logs/digest_YYYYWW.flag 마커로 방지.
"""

from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.notify import notify_weekly_digest


class Command(BaseCommand):
    help = "주간 요약 텔레그램 발송 (월요일만, --force로 즉시)"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="요일·중복 무시하고 발송")

    def handle(self, *args, **opts):
        today = date.today()
        marker_dir = Path(settings.BASE_DIR) / "logs"
        marker_dir.mkdir(exist_ok=True)
        marker = marker_dir / f"digest_{today:%G%V}.flag"

        if not opts["force"]:
            if today.weekday() != 0:
                self.stdout.write("월요일 아님 - 발송 생략")
                return
            if marker.exists():
                self.stdout.write("이번주 이미 발송됨 - 생략")
                return

        notify_weekly_digest(stdout=self.stdout)
        marker.write_text(f"{today}")
