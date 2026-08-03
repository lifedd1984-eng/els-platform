"""스레드 댓글 자동 응답 — A버킷만 정형 문구로 답하고, B·C는 텔레그램으로 넘긴다.

절대 제약
  자동으로 나가는 문장은 core/threads_replies.py의 상수 문구뿐이다. 질문 내용을
  한 글자도 반영하지 않는다. 개별 사정이 반영된 답변은 미등록 투자자문업이 될
  수 있고(자본시장법, 3년 이하 징역 또는 1억원 이하 벌금), 대법원 2018도4413은
  유·무료가 아니라 "답변에 상대방의 개별 사정이 반영되었느냐"를 본다.
  그래서 B(사람이 답할 질문)·C(개별 판단 요청)는 발송 경로 자체가 없다.

안전장치를 이렇게 둔 이유
  시간대 제한  새벽에 답글이 달리면 사람이 아니라는 게 바로 드러난다.
  발송 지연    댓글 달린 지 몇 초 만에 답하면 마찬가지다. 3~20분 뒤에 답한다.
  시간당 상한  규칙이 잘못 돌아 폭주해도 피해가 15건에서 멈춘다.
  문구 중복    같은 문장이 연달아 달리면 자동화가 드러나고 신뢰를 잃는다.
  최종 화이트리스트  선택 로직에 버그가 나도 즉흥 문장만은 못 나가게 막는다.

실패는 재시도하지 않는다
  게시 API가 오류를 줘도 실제로는 올라갔을 수 있다. 재시도하면 같은 댓글에
  답글이 두 번 달린다. 그래서 실패는 skipped로 닫고 텔레그램으로 알린 뒤
  사람이 판단하게 한다.

사용:
  python manage.py reply_threads               # 조건 맞는 A버킷 발송 + B·C 알림
  python manage.py reply_threads --dry-run     # 아무것도 보내지 않고 계획만 출력
  python manage.py reply_threads --samples     # 분류기 점검 (샘플 문장 -> 버킷)
  python manage.py reply_threads --limit 3     # 이번 실행 최대 발송 건수
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import ThreadsReply
from core.threads_replies import (GREETING_REPLIES, SAMPLE_TEXTS, SERVICE_REPLIES,
                                  choose_reply, classify, is_approved,
                                  is_service_reason, reply_delay_minutes)

# ┌────────────────────────────────────────────────────────────────┐
# │ 발송 안전장치 — 값 조정은 여기서만 한다                          │
# └────────────────────────────────────────────────────────────────┘
WINDOW_START = 8         # 발송 허용 시작 시각 (KST)
WINDOW_END = 23          # 발송 허용 종료 시각 — 이 시각부터는 안 보냄
DELAY_MIN = 3            # 댓글 작성 후 최소 지연(분)
DELAY_MAX = 20           # 최대 지연(분)
HOURLY_MAX = 15          # 최근 1시간 발송 상한
RECENT_TEXT_N = 50       # 문구 중복 검사 대상 (최근 발송 N건)
MAX_AGE_HOURS = 24       # 이보다 오래된 댓글은 이제 와서 답하지 않는다
NOTIFY_MAX_PER_RUN = 20  # 밀린 B·C가 많아도 한 번에 이만큼만 알린다
# 20건을 건별로 쏘면 텔레그램 분당 한도(같은 대화 ≈20건)에 그대로 닿는다.
# 한도에 걸리면 send_message가 False를 주고 그 건은 new로 남아 다음 실행에서
# 또 20건을 쏘는 악순환이 된다. 5건씩 한 메시지로 묶으면 최대 4통이라 여유가 있다.
NOTIFY_BATCH_SIZE = 5


class Command(BaseCommand):
    help = "스레드 A버킷 정형 응답 발송 + B·C 텔레그램 알림"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="발송·저장 없이 계획만 출력")
        parser.add_argument("--samples", action="store_true",
                            help="샘플 문장 분류 결과만 출력하고 종료")
        parser.add_argument("--limit", type=int, default=None,
                            help="이번 실행 최대 발송 건수 (기본: 시간당 상한까지)")

    def handle(self, *args, **opts):
        if opts["samples"]:
            self._print_samples()
            return

        self.dry = opts["dry_run"]
        now = timezone.localtime()

        if not (WINDOW_START <= now.hour < WINDOW_END):
            msg = (f"발송 가능 시간이 아님 ({now:%H:%M} KST, "
                   f"{WINDOW_START:02d}:00~{WINDOW_END:02d}:00만 발송)")
            if not self.dry:
                self.stdout.write(msg + " — 자동 응답은 미루고 C버킷만 알림")
                # C(개별 판단 요청·컴플레인)는 야간에도 즉시 알린다. 사기·환불·
                # 고소 같은 글을 아침까지 8시간 모르고 있는 손해가 알림 소음보다
                # 크다. B는 급하지 않으므로 아침 첫 실행 때 몰아서 나간다.
                self._notify_manual(now, buckets=["C"])
                return
            # dry-run은 점검용이라 시간대와 무관하게 계획을 보여준다
            self.stdout.write(f"[dry-run] {msg} — 점검을 위해 계속 진행")

        self._send_auto_replies(now, opts["limit"])
        self._notify_manual(now)

    # ------------------------------------------------------------------
    # A버킷 정형 응답
    # ------------------------------------------------------------------
    def _send_auto_replies(self, now, limit):
        sent_last_hour = ThreadsReply.objects.filter(
            status="replied", replied_at__gte=now - timedelta(hours=1)).count()
        budget = HOURLY_MAX - sent_last_hour
        if limit is not None:
            budget = min(budget, limit)
        if budget <= 0:
            self.stdout.write(f"최근 1시간 {sent_last_hour}건 발송 — 상한 도달, 발송 생략")
            return

        recent_texts = list(
            ThreadsReply.objects.filter(status="replied", replied_at__isnull=False)
            .order_by("-replied_at")[:RECENT_TEXT_N]
            .values_list("replied_text", flat=True))

        # 루프 안에서 status를 바꾸므로 미리 확정해 둔다 (커서를 열어 둔 채
        # 필터 대상 컬럼을 고치면 결과가 흔들린다)
        rows = list(ThreadsReply.objects.filter(bucket="A", status="new")
                    .order_by("timestamp")[:200])
        sent = waiting = aged = 0

        for row in rows:
            if sent >= budget:
                self.stdout.write(f"이번 실행 상한 {budget}건 도달 — 나머지는 다음 실행")
                break

            base = row.timestamp or row.created_at
            delay = reply_delay_minutes(row.reply_id, DELAY_MIN, DELAY_MAX)
            due = base + timedelta(minutes=delay)

            if base < now - timedelta(hours=MAX_AGE_HOURS):
                aged += 1
                self._close(row, f"{MAX_AGE_HOURS}시간 초과로 발송 안 함")
                continue
            if now < due:
                waiting += 1
                if self.dry:
                    self.stdout.write(
                        f"  대기  @{row.username} — {timezone.localtime(due):%H:%M} 발송 예정 "
                        f"(지연 {delay}분): {row.text[:40]}")
                continue

            pool = SERVICE_REPLIES if is_service_reason(row.bucket_reason) else GREETING_REPLIES
            text = choose_reply(row.reply_id, pool, recent_texts)

            # 최종 방어선 — 상수 목록에 없는 문장은 어떤 경우에도 내보내지 않는다
            if not is_approved(text):
                self._close(row, "승인 문구가 아님 — 발송 차단")
                self._alert(f"[스레드 댓글] 승인되지 않은 문구가 선택되어 발송을 막았습니다.\n"
                            f"댓글 {row.reply_id} / 문구: {text[:80]}")
                continue

            if self.dry:
                self.stdout.write(
                    f"  발송  @{row.username} [{row.bucket_reason}]\n"
                    f"        원문: {row.text[:60]}\n"
                    f"        답글: {text}")
                sent += 1
                recent_texts.insert(0, text)
                continue

            try:
                from core.threads_api import post_reply
                post_reply(text, row.reply_id)
            except Exception as e:
                # 재시도하지 않는다 — 실패로 보였지만 게시됐을 수 있다(중복 답글 방지)
                self._close(row, f"발송 실패: {e}"[:120])
                self._alert(f"[스레드 댓글] 자동 응답 발송 실패 — 재시도하지 않습니다.\n"
                            f"@{row.username}: {row.text[:80]}\n{e}\n{row.permalink}")
                continue

            row.status = "replied"
            row.replied_text = text
            row.replied_at = timezone.now()
            row.save(update_fields=["status", "replied_text", "replied_at"])
            sent += 1
            recent_texts.insert(0, text)
            self.stdout.write(f"  답글 완료 @{row.username}: {text}")

        head = "[dry-run] " if self.dry else ""
        self.stdout.write(self.style.SUCCESS(
            f"{head}A버킷 발송 {sent}건 / 지연 대기 {waiting}건 / 시간초과 {aged}건 "
            f"(최근 1시간 누적 {sent_last_hour + (0 if self.dry else sent)}건)"))

    # ------------------------------------------------------------------
    # B·C버킷 알림
    # ------------------------------------------------------------------
    def _notify_manual(self, now, buckets=None):
        buckets = buckets or ["B", "C"]
        rows = list(ThreadsReply.objects.filter(
            bucket__in=buckets, status="new").order_by("timestamp")[:NOTIFY_MAX_PER_RUN])
        if not rows:
            self.stdout.write("사람이 답할 댓글 없음")
            return

        notified = 0
        # 묶음 단위로 보낸다. C가 섞인 묶음은 제목에 그대로 드러나야 하므로
        # 항목별 머리말은 그대로 두고 앞에 묶음 안내만 붙인다.
        for i in range(0, len(rows), NOTIFY_BATCH_SIZE):
            chunk = rows[i:i + NOTIFY_BATCH_SIZE]
            body = self._manual_message(chunk)
            if self.dry:
                self.stdout.write("  ── 알림 예정 ──\n" + body)
                continue
            # 발송에 성공했을 때만 닫는다. send_message는 실패해도 예외 없이
            # False만 돌려주는데, 예전에는 결과를 안 보고 notified로 닫아서
            # 텔레그램이 레이트리밋에 걸리면 컴플레인이 영영 묻혔다.
            # 실패하면 new로 남겨 다음 실행(10분 뒤)에 다시 시도한다. (2026-08-03)
            # 묶음이 실패하면 그 묶음 전체가 new로 남는다 — 한 건이라도 묻히는
            # 것보다 중복 알림이 낫다.
            if self._alert(body):
                for row in chunk:
                    row.status = "notified"
                    row.save(update_fields=["status"])
                notified += len(chunk)
            else:
                self.stdout.write(
                    f"  알림 실패 — 다음 실행에서 재시도: "
                    + ", ".join(f"@{r.username}" for r in chunk))

        head = "[dry-run] " if self.dry else ""
        msgs = (len(rows) + NOTIFY_BATCH_SIZE - 1) // NOTIFY_BATCH_SIZE
        left = ThreadsReply.objects.filter(bucket__in=buckets, status="new").count()
        if self.dry:                      # dry-run은 상태를 안 바꾸므로 직접 빼 준다
            left -= len(rows)
            notified = len(rows)
        tail = f" (남은 {left}건은 다음 실행)" if left > 0 else ""
        self.stdout.write(self.style.SUCCESS(
            f"{head}B·C 알림 {notified}건 / 메시지 {msgs}통{tail}"))

    @staticmethod
    def _manual_message(chunk):
        """B·C 댓글 묶음 하나를 텔레그램 메시지 한 통으로 만든다.

        본문을 400자로 자르고 있어 한 건이 넉넉히 잡아도 700자 안쪽이라,
        5건이면 텔레그램 상한(4,096자)에 닿지 않는다.
        """
        parts = []
        for j, row in enumerate(chunk, 1):
            head = ("개별 판단 요청 — 직접 답변 필요" if row.bucket == "C"
                    else "사람 답변 필요")
            num = f"({j}/{len(chunk)}) " if len(chunk) > 1 else ""
            parts.append(f"{num}{head}\n"
                         f"@{row.username or '(작성자 미상)'}\n"
                         f"{row.text[:400]}\n"
                         f"분류 근거: {row.bucket_reason}\n"
                         f"{row.permalink or '(링크 없음)'}")
        return f"[스레드 댓글] {len(chunk)}건\n\n" + "\n\n".join(parts)

    # ------------------------------------------------------------------
    def _close(self, row, reason):
        """더 이상 손대지 않도록 skipped로 닫는다 (근거를 남겨 나중에 확인 가능)."""
        self.stdout.write(f"  건너뜀 @{row.username}: {reason}")
        if self.dry:
            return
        row.status = "skipped"
        row.bucket_reason = f"{row.bucket_reason} | {reason}"[:120]
        row.save(update_fields=["status", "bucket_reason"])

    def _alert(self, text):
        """텔레그램 발송. 실제로 보냈으면 True — 호출부가 이 값으로 상태를 정한다."""
        if self.dry:
            return True
        try:
            from core.telegram import send_message
            return bool(send_message(text))
        except Exception as e:
            self.stderr.write(f"텔레그램 알림 실패: {e}")
            return False

    def _print_samples(self):
        """분류기 점검 — 샘플 문장이 기대한 버킷으로 떨어지는지 본다."""
        self.stdout.write(f"{'기대':4s} {'결과':4s} {'근거':30s} 본문")
        self.stdout.write("─" * 100)
        bad = 0
        for text, want in SAMPLE_TEXTS:
            got, reason = classify(text)
            if got != want:
                bad += 1
            mark = " " if got == want else "!"
            self.stdout.write(f"{want:4s} {got:4s} {reason:30s} {mark}{text}")
        line = f"샘플 {len(SAMPLE_TEXTS)}건 중 불일치 {bad}건"
        self.stdout.write(self.style.SUCCESS(line) if not bad
                          else self.style.ERROR(line))
