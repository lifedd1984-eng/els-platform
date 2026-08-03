"""스레드 댓글 수집 — 최근 게시물의 새 댓글을 ThreadsReply에 저장하고 분류한다.

수집과 발송을 왜 나눴는가
  발송에는 지연(댓글 작성 후 3~20분)·시간대·시간당 상한이 걸린다. 한 명령에
  묶으면 수집이 늦어질 때 발송 조건까지 같이 밀린다. 수집은 자주 돌려 놓고,
  발송은 reply_threads가 조건이 맞는 것만 골라 가게 한다.

분류를 여기서 하는 이유
  수집 시점에 버킷을 확정해 두면 나중에 규칙을 고쳐도 "그때 무엇으로 봤는지"가
  남는다. 자동 응답 범위를 사후 검증하려면 이 기록이 있어야 한다.

사용:
  python manage.py fetch_threads_replies              # 최근 7일 게시물의 새 댓글 수집
  python manage.py fetch_threads_replies --days 14
  python manage.py fetch_threads_replies --dry-run    # 저장 없이 분류 결과만 출력
"""

from datetime import date, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core import threads_api
from core.models import ThreadsReply
from core.threads_replies import classify


def _parse_ts(value):
    """Threads의 ISO 시각 문자열 -> aware datetime.

    Threads는 +0000을 붙여 주지만, 혹시 빠져 오면 naive 값이 저장돼 발송 지연
    계산이 9시간 어긋난다. 그래서 naive면 UTC로 간주해 붙여 준다.
    """
    dt = parse_datetime(value or "")
    if dt and timezone.is_naive(dt):
        return timezone.make_aware(dt, dt_timezone.utc)
    return dt


class Command(BaseCommand):
    help = "스레드 댓글 수집 + A/B/C 분류 (발송은 reply_threads가 한다)"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7,
                            help="며칠 전 게시물까지 훑을지 (기본 7)")
        parser.add_argument("--dry-run", action="store_true",
                            help="저장하지 않고 분류 결과만 출력")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        since = date.today() - timedelta(days=opts["days"])

        try:
            posts = threads_api.fetch_my_posts(since)
        except Exception as e:
            self._fail(f"게시물 목록 조회 실패: {e}", quiet=dry)
            return

        if not posts:
            self.stdout.write(f"{since} 이후 게시물 없음 — 수집할 댓글 없음")
            return

        # 이미 저장된 댓글 id를 한 번에 읽어 둔다 (댓글마다 exists() 치면 느리다)
        known = set(ThreadsReply.objects.values_list("reply_id", flat=True))
        counts = {"A": 0, "B": 0, "C": 0}
        new_total = skipped_mine = 0

        for post in posts:
            try:
                replies = threads_api.fetch_replies(post["id"])
            except Exception as e:
                # 게시물 하나가 실패해도 나머지는 계속 훑는다
                self.stderr.write(f"[{post['id']}] 댓글 조회 실패: {e}")
                continue

            for r in replies:
                rid = r.get("id")
                if not rid or rid in known:
                    continue
                if r.get("is_reply_owned_by_me"):
                    # 우리가 단 답글. 수집하면 우리 답글에 또 답하는 사고가 난다
                    skipped_mine += 1
                    continue

                text = r.get("text") or ""
                bucket, reason = classify(text)
                counts[bucket] += 1
                new_total += 1
                known.add(rid)

                if dry:
                    preview = " ".join(text[:60].split())
                    self.stdout.write(
                        f"[{bucket}] {reason:28s} @{r.get('username', '')}: {preview}")
                    continue

                # create가 아니라 get_or_create — 크론이 겹쳐 두 프로세스가 같은
                # 댓글을 동시에 만나면 unique 위반으로 루프 전체가 죽는다.
                # (2026-08-03 검수. 겹침 자체는 threads_replies.sh의 flock으로 막지만
                #  수동 실행이 크론과 부딪히는 경우까지 여기서 흡수한다.)
                ThreadsReply.objects.get_or_create(
                    reply_id=rid,
                    defaults=dict(
                        post_id=post["id"],
                        username=r.get("username") or "",
                        text=text,
                        timestamp=_parse_ts(r.get("timestamp")),
                        permalink=r.get("permalink") or "",
                        bucket=bucket,
                        bucket_reason=reason,
                        status="new",
                    ),
                )

        head = "[dry-run] " if dry else ""
        self.stdout.write(self.style.SUCCESS(
            f"{head}게시물 {len(posts)}건 훑음 — 새 댓글 {new_total}건 "
            f"(A {counts['A']} / B {counts['B']} / C {counts['C']}), "
            f"내 답글 제외 {skipped_mine}건"))

    # ------------------------------------------------------------------
    def _fail(self, msg, quiet=False):
        """토큰 만료·권한 오류는 조용히 지나가면 며칠씩 수집이 멈춘다."""
        self.stderr.write(self.style.ERROR(msg))
        if not quiet:
            try:
                from core.telegram import send_message
                send_message(f"[스레드 댓글] 수집 실패 — {msg}")
            except Exception:
                pass
        raise SystemExit(1)
