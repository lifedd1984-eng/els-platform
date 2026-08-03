from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from core.models import ThreadsReply
from core.threads_replies import (GREETING_REPLIES, SAMPLE_TEXTS, SERVICE_REPLIES,
                                  choose_reply, classify, is_approved,
                                  reply_delay_minutes)


class ThreadsReplyClassifierTest(SimpleTestCase):
    """스레드 댓글 분류기 회귀 테스트.

    이 분류기가 자동 응답의 법적 경계선이다. 규칙을 고칠 때 예전에 C였던 문장이
    조용히 A로 넘어가면 미등록 투자자문업 리스크가 생긴다. 그래서 샘플 문장의
    기대 버킷을 코드에 못 박아 둔다.
    """

    def test_샘플_문장이_기대한_버킷으로_분류된다(self):
        for text, want in SAMPLE_TEXTS:
            got, reason = classify(text)
            self.assertEqual(got, want, f"{text!r} -> {got}({reason}), 기대 {want}")

    def test_판단_요청은_절대_A로_가지_않는다(self):
        for text, want in SAMPLE_TEXTS:
            if want == "C":
                self.assertNotEqual(classify(text)[0], "A", text)

    def test_빈_본문은_인사로_오인하지_않는다(self):
        self.assertEqual(classify("")[0], "B")
        self.assertEqual(classify("   ")[0], "B")

    def test_같은_댓글은_항상_같은_문구와_지연을_낸다(self):
        # random을 쓰지 않는 이유 — 재실행해도 결과가 재현돼야 검증이 가능하다
        for rid in ("17900000000000001", "17900000000000002"):
            self.assertEqual(choose_reply(rid, GREETING_REPLIES),
                             choose_reply(rid, GREETING_REPLIES))
            self.assertEqual(reply_delay_minutes(rid, 3, 20),
                             reply_delay_minutes(rid, 3, 20))

    def test_지연은_정해진_범위_안이다(self):
        for i in range(500):
            self.assertIn(reply_delay_minutes(f"id{i}", 3, 20), range(3, 21))

    def test_최근에_쓴_문구는_피한다(self):
        rid = "17900000000000003"
        first = choose_reply(rid, SERVICE_REPLIES)
        self.assertNotEqual(choose_reply(rid, SERVICE_REPLIES, [first]), first)

    def test_문구가_전부_최근이면_가장_오래된_것을_고른다(self):
        recent = list(SERVICE_REPLIES)          # 최신순 목록
        self.assertEqual(choose_reply("x", SERVICE_REPLIES, recent), recent[-1])

    def test_승인되지_않은_문장은_통과하지_못한다(self):
        self.assertTrue(all(is_approved(t) for t in GREETING_REPLIES + SERVICE_REPLIES))
        self.assertFalse(is_approved("지금 들어가셔도 괜찮습니다"))
        self.assertFalse(is_approved(GREETING_REPLIES[0] + " "))

    def test_정형_문구에는_투자_판단이_없다(self):
        금지어 = ("추천", "괜찮", "안전", "수익", "매수", "매도", "유리", "손실",
                  "낙인", "상품", "종목", "지수")
        for text in GREETING_REPLIES + SERVICE_REPLIES:
            for w in 금지어:
                self.assertNotIn(w, text, f"{text!r}에 '{w}'가 들어 있다")


# 2026-08-03(월) 14:00 KST — 발송 허용 시간대 한가운데로 고정한다.
# 고정하지 않으면 테스트를 밤에 돌릴 때 시간대 제한에 걸려 결과가 달라진다.
NOW = datetime(2026, 8, 3, 5, 0, tzinfo=dt_timezone.utc)


class ReplyThreadsCommandTest(TestCase):
    """reply_threads 발송 게이트 검증 — B·C가 새어 나가지 않는지가 핵심이다."""

    def _row(self, rid, bucket, minutes_ago, reason="", text="테스트"):
        return ThreadsReply.objects.create(
            reply_id=rid, post_id="post1", username="tester", text=text,
            timestamp=NOW - timedelta(minutes=minutes_ago),
            permalink=f"https://www.threads.net/t/{rid}",
            bucket=bucket, bucket_reason=reason, status="new")

    def _run(self, **kw):
        with mock.patch("django.utils.timezone.now", return_value=NOW), \
             mock.patch("core.threads_api.post_reply", return_value="posted") as post, \
             mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("reply_threads", stdout=mock.MagicMock(), **kw)
        return post, tg

    def test_A만_발송되고_B_C는_알림만_간다(self):
        self._row("a1", "A", 60, "A:인사감사(감사)")
        self._row("b1", "B", 60, "B:기본(A·C 신호 없음)")
        self._row("c1", "C", 60, "C:판단요청(들어가도)")

        post, tg = self._run()

        self.assertEqual(post.call_count, 1)
        sent_text, sent_to = post.call_args.args
        self.assertEqual(sent_to, "a1")
        self.assertTrue(is_approved(sent_text))

        self.assertEqual(ThreadsReply.objects.get(reply_id="a1").status, "replied")
        self.assertEqual(ThreadsReply.objects.get(reply_id="b1").status, "notified")
        self.assertEqual(ThreadsReply.objects.get(reply_id="c1").status, "notified")

        alerts = "\n".join(c.args[0] for c in tg.call_args_list)
        self.assertIn("개별 판단 요청 — 직접 답변 필요", alerts)   # C 구분 표시
        self.assertIn("사람 답변 필요", alerts)                   # B
        self.assertIn("https://www.threads.net/t/c1", alerts)     # permalink 포함

    def test_지연_전에는_보내지_않는다(self):
        self._row("fresh", "A", 1, "A:인사감사(감사)")   # 최소 지연 3분 미만
        post, _ = self._run()
        self.assertEqual(post.call_count, 0)
        self.assertEqual(ThreadsReply.objects.get(reply_id="fresh").status, "new")

    def test_너무_오래된_댓글은_건너뛴다(self):
        self._row("old", "A", 60 * 30, "A:인사감사(감사)")   # 30시간 전
        post, _ = self._run()
        self.assertEqual(post.call_count, 0)
        self.assertEqual(ThreadsReply.objects.get(reply_id="old").status, "skipped")

    def test_시간당_상한을_넘기지_않는다(self):
        for i in range(20):
            self._row(f"m{i}", "A", 60, "A:인사감사(감사)")
        post, _ = self._run()
        self.assertEqual(post.call_count, 15)

    def test_발송_실패는_재시도하지_않고_알린다(self):
        self._row("boom", "A", 60, "A:인사감사(감사)")
        with mock.patch("django.utils.timezone.now", return_value=NOW), \
             mock.patch("core.threads_api.post_reply", side_effect=RuntimeError("500")), \
             mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("reply_threads", stdout=mock.MagicMock())
        row = ThreadsReply.objects.get(reply_id="boom")
        self.assertEqual(row.status, "skipped")          # new로 두면 중복 답글이 난다
        self.assertIn("발송 실패", row.bucket_reason)
        self.assertIn("발송 실패", "\n".join(c.args[0] for c in tg.call_args_list))

    def test_dry_run은_아무것도_보내지_않고_저장도_안_한다(self):
        self._row("a1", "A", 60, "A:서비스안내(링크)")
        self._row("c1", "C", 60, "C:판단요청(들어가도)")
        post, tg = self._run(dry_run=True)
        self.assertEqual(post.call_count, 0)
        self.assertEqual(tg.call_count, 0)
        self.assertEqual(
            set(ThreadsReply.objects.values_list("status", flat=True)), {"new"})

    def test_시간대_밖에서는_발송하지_않는다(self):
        self._row("a1", "A", 60, "A:인사감사(감사)")
        새벽 = NOW.replace(hour=19)          # 04:00 KST
        with mock.patch("django.utils.timezone.now", return_value=새벽), \
             mock.patch("core.threads_api.post_reply") as post, \
             mock.patch("core.telegram.send_message"):
            call_command("reply_threads", stdout=mock.MagicMock())
        self.assertEqual(post.call_count, 0)
        self.assertEqual(ThreadsReply.objects.get(reply_id="a1").status, "new")

    def test_서비스_질문에는_안내_문구가_나간다(self):
        self._row("a1", "A", 60, "A:서비스안내(링크)", text="링크 어디예요?")
        post, _ = self._run()
        self.assertIn(post.call_args.args[0], SERVICE_REPLIES)

    def test_연속_발송은_같은_문구를_반복하지_않는다(self):
        for i in range(5):
            self._row(f"g{i}", "A", 60, "A:인사감사(감사)")
        post, _ = self._run()
        texts = [c.args[0] for c in post.call_args_list]
        self.assertEqual(len(texts), len(set(texts)))
