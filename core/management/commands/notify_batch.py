"""
배치 실행 결과 텔레그램 보고. run_scrape.bat 마지막에 호출된다.

사용: python manage.py notify_batch --results "scrape=0,prices=0,redeem=0,simulate=0,digest=0"
(각 값은 해당 단계의 ERRORLEVEL, 0=성공)
"""

from datetime import datetime

from django.core.management.base import BaseCommand

from core import telegram

STEP_LABELS = {
    "scrape": "KOFIA 수집",
    "prices": "시세/낙인",
    "redeem": "상환판정",
    "simulate": "손실확률",
    "digest": "주간요약",
    "backup": "DB백업",
}


class Command(BaseCommand):
    help = "배치 결과 텔레그램 보고"

    def add_arguments(self, parser):
        parser.add_argument("--results", default="", help="step=exitcode 쉼표 목록")

    def handle(self, *args, **opts):
        results = []
        for part in opts["results"].split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            try:
                code = int(v)
            except ValueError:
                code = -1
            results.append((k.strip(), code))

        if not results:
            self.stdout.write("결과 없음 - 발송 생략")
            return

        fails = [k for k, c in results if c != 0]
        head = "[배치 실패]" if fails else "[배치 완료]"
        now = datetime.now()
        weekday = "월화수목금토일"[now.weekday()]
        lines = [f"{head} {now:%m.%d}({weekday}) {now:%H:%M}"]
        for k, c in results:
            label = STEP_LABELS.get(k, k)
            mark = "OK" if c == 0 else f"실패(exit {c})"
            lines.append(f"- {label}: {mark}")
        if fails:
            lines.append("로그: platform\\logs\\scrape_*.log 확인")

        if telegram.send_message("\n".join(lines)):
            self.stdout.write("배치 결과 발송 완료")
