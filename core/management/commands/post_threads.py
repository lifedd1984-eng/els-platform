"""주간 TOP5 스레드 자동 포스팅 — daily.sh에서 월요일에만 실행.

사용:
  python manage.py post_threads              # 월요일이면 게시 (그 외 요일 스킵)
  python manage.py post_threads --force      # 요일 무관 즉시 게시
  python manage.py post_threads --dry-run    # 게시 없이 본문만 출력
"""

from datetime import date

from django.core.management.base import BaseCommand

DISCLAIMER = ("※ 조건 부합 상품의 시스템 분류이며 투자권유가 아닙니다. "
              "원금손실 가능 상품입니다.")


def build_post():
    """이번 주 TOP5 스레드 본문 (500자 제한 고려해 간결하게)."""
    from core.models import radar_top5

    top = radar_top5()
    if not top:
        return None

    today = date.today()
    lines = [f"이번 주 ELS, 300여 개 중 조건 통과는 {len(top)}개",
             f"({today:%m/%d} 기준 · 손실확률 0% · 1년내 상환확률 90%↑)", ""]
    for i, p in enumerate(top, 1):
        ki = "노낙인" if p.is_no_ki else f"낙인{p.ki}%"
        lines.append(f"{i}. {p.issuer} · 연 {p.yield_rate:g}% · {ki}")
    lines += ["", "전체 분석과 손실확률 계산 근거는 프로필 링크에서",
              "elsrader.site", "", DISCLAIMER]
    return "\n".join(lines)


class Command(BaseCommand):
    help = "주간 TOP5 스레드 포스팅 (월요일만, --force로 강제)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **opts):
        if not opts["force"] and date.today().weekday() != 0:
            self.stdout.write("월요일 아님 — 스킵 (--force로 강제 가능)")
            return

        text = build_post()
        if not text:
            self.stdout.write("TOP5 없음 — 포스팅 스킵")
            return

        if opts["dry_run"]:
            self.stdout.write("── dry-run 본문 ──\n" + text)
            return

        from core.threads_api import post_text
        post_id = post_text(text)
        self.stdout.write(f"스레드 게시 완료: {post_id}")
        try:                              # 성공 알림 (실패해도 게시엔 영향 없음)
            from core.telegram import send_message
            send_message(f"[스레드] 주간 TOP5 게시 완료 (id {post_id})")
        except Exception:
            pass
