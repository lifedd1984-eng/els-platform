import os
import tempfile
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from core.models import HistoricalIssue, Investment, Product, ThreadsReply
from core.threads_replies import (GREETING_REPLIES, SAMPLE_TEXTS, SERVICE_REPLIES,
                                  choose_reply, classify, is_approved,
                                  reply_delay_minutes)


class ScheduleBadgeTest(TestCase):
    """스케줄이 근사로 떨어졌는데 배지가 '확정'으로 나오지 않는지 본다.

    예전엔 schedule은 date.fromisoformat을 실제로 돌려 보고 실패하면 근사로
    떨어졌는데, schedule_badge는 eval_dates의 '개수'만 세서 배지 없음(=확정)을
    냈다. 화면·엑셀 내려받기가 근사 날짜를 확정이라고 표시했다.
    두 프로퍼티가 같은 헬퍼(Product.fixed_eval_dates)를 보는지가 이 테스트의 핵심이다.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="tester", password="x")

    def _inv(self, eval_dates):
        # 한 테스트에서 여러 건을 만드므로 회차를 겹치지 않게 준다
        # (uniq_product가 sub_end=NULL끼리도 같다고 보기 때문에 "1" 고정이면 충돌)
        self._seq = getattr(self, "_seq", 0) + 1
        p = Product.objects.create(
            issuer="테스트증권", product_no=str(self._seq), yield_rate=6.0,
            barriers_raw=[90, 85, 80], period_months=6, first_eval_months=6,
            issue_date=date(2025, 1, 10), expiry_date=date(2026, 7, 10),
            eval_dates=eval_dates)
        return Investment.objects.create(
            user=self.user, product=p, amount=10_000_000,
            invested_at=date(2025, 1, 9))

    def _근사로_떨어졌는데_추정_배지가_붙는다(self, inv):
        p = inv.product
        self.assertIsNone(p.fixed_eval_dates)
        self.assertEqual(inv.schedule_badge, "추정")
        # 근사 = 기준일 + N개월 (여기서는 2025-01-10 + 6/12/18개월)
        self.assertEqual([r["date"] for r in inv.schedule],
                         [date(2025, 7, 10), date(2026, 1, 10), date(2026, 7, 10)])

    def test_제로패딩_없는_날짜는_확정으로_표시하지_않는다(self):
        # "2025-7-10"은 ISO 형식이 아니라 date.fromisoformat이 거부한다
        self._근사로_떨어졌는데_추정_배지가_붙는다(
            self._inv(["2025-7-10", "2026-1-12", "2026-7-13"]))

    def test_원소에_None이_섞이면_확정으로_표시하지_않는다(self):
        self._근사로_떨어졌는데_추정_배지가_붙는다(
            self._inv(["2025-07-11", None, "2026-07-13"]))

    def test_리스트가_아닌_문자열은_확정으로_표시하지_않는다(self):
        # JSONField에 문자열이 그대로 들어가면 len()이 글자수(3)라 개수 검사를 통과했다
        self._근사로_떨어졌는데_추정_배지가_붙는다(self._inv("abc"))

    def test_정상값은_확정이고_스케줄도_그_날짜를_쓴다(self):
        inv = self._inv(["2025-07-11", "2026-01-12", "2026-07-13"])
        self.assertIsNone(inv.schedule_badge)
        self.assertEqual([r["date"] for r in inv.schedule],
                         [date(2025, 7, 11), date(2026, 1, 12), date(2026, 7, 13)])

    def test_개수가_배리어와_다르면_확정이_아니다(self):
        inv = self._inv(["2025-07-11", "2026-01-12"])
        self.assertEqual(inv.schedule_badge, "추정")

    def test_배지와_스케줄은_언제나_같은_판단을_쓴다(self):
        # 배지가 '확정'(None)인데 스케줄이 근사인 조합이 하나도 없어야 한다
        cases = [None, [], "abc", ["2025-7-10", "2026-1-12", "2026-7-13"],
                 ["2025-07-11", None, "2026-07-13"], ["2025-07-11", "2026-01-12"],
                 ["2025-07-11", "2026-01-12", "2026-07-13"]]
        for ev in cases:
            inv = self._inv(ev)
            근사 = [r["date"] for r in inv.schedule] == [
                date(2025, 7, 10), date(2026, 1, 10), date(2026, 7, 10)]
            self.assertEqual(inv.schedule_badge is None, not 근사, f"eval_dates={ev!r}")


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

    def test_BC_알림은_5건씩_묶어_보낸다(self):
        # 20건을 건별로 쏘면 텔레그램 분당 한도(≈20건)에 그대로 닿는다
        for i in range(20):
            self._row(f"b{i}", "B", 60, "B:기본(A·C 신호 없음)", text=f"질문{i}")
        _, tg = self._run()
        self.assertEqual(tg.call_count, 4)                    # 20건 → 4통
        bodies = [c.args[0] for c in tg.call_args_list]
        for body in bodies:
            self.assertLess(len(body), 4096)                  # 텔레그램 상한
        합본 = "\n".join(bodies)
        for i in range(20):                                   # 한 건도 빠지지 않는다
            self.assertIn(f"질문{i}", 합본)
        self.assertEqual(ThreadsReply.objects.filter(status="notified").count(), 20)

    def test_묶음_발송이_실패하면_그_묶음은_new로_남는다(self):
        for i in range(5):
            self._row(f"b{i}", "B", 60, "B:기본(A·C 신호 없음)")
        with mock.patch("django.utils.timezone.now", return_value=NOW), \
             mock.patch("core.threads_api.post_reply"), \
             mock.patch("core.telegram.send_message", return_value=False):
            call_command("reply_threads", stdout=mock.MagicMock())
        self.assertEqual(ThreadsReply.objects.filter(status="new").count(), 5)


class PostThreadsDailyCheckTest(TestCase):
    """--check-all이 문제를 찾고도 0으로 끝나면 크론이 초록불을 본다."""

    def test_문제가_있으면_종료코드_1(self):
        나쁜원고 = [{"day": 1, "type": "E", "text": "지금 들어가면 무조건 수익 납니다"}]
        with mock.patch("core.management.commands.post_threads_daily.DRAFTS", 나쁜원고), \
             mock.patch("core.management.commands.post_threads_daily.check",
                        return_value=["금칙어: 무조건"]):
            with self.assertRaises(SystemExit) as cm:
                call_command("post_threads_daily", check_all=True,
                             stdout=mock.MagicMock())
        self.assertEqual(cm.exception.code, 1)

    def test_문제가_없으면_예외없이_끝난다(self):
        좋은원고 = [{"day": 1, "type": "E", "text": "오늘의 기록입니다"}]
        with mock.patch("core.management.commands.post_threads_daily.DRAFTS", 좋은원고), \
             mock.patch("core.management.commands.post_threads_daily.check",
                        return_value=[]):
            call_command("post_threads_daily", check_all=True, stdout=mock.MagicMock())


class TemplateCompileTest(TestCase):
    """모든 템플릿이 컴파일되는지 확인.

    왜 필요한가
      Django 템플릿 문법 오류는 manage.py check도 기존 테스트도 잡지 못한다.
      실제로 여러 줄 {# #} 주석(단일 행 전용이라 문법 오류)을 넣었는데 둘 다
      통과했고, 렌더 시점에야 터졌다. 화면이 통째로 500이 되는 종류의 오류라
      배포 전에 걸러야 한다. (2026-08-04)
    """

    def test_모든_템플릿이_컴파일된다(self):
        from pathlib import Path

        from django.conf import settings
        from django.template.loader import get_template

        root = Path(settings.BASE_DIR) / "core" / "templates" / "core"
        names = sorted(p.name for p in root.glob("*.html"))
        self.assertGreater(len(names), 10, "템플릿을 못 찾았다 — 경로 확인 필요")

        broken = []
        for name in names:
            try:
                get_template(f"core/{name}")
            except Exception as e:
                broken.append(f"{name}: {type(e).__name__} {e}")
        self.assertEqual(broken, [], "컴파일 실패 템플릿:\n" + "\n".join(broken))


class ProductCodeBackfillTest(TestCase):
    """발행사+발행일+회차로 SEIBro ISIN을 찾을 때 엉뚱한 상품을 집지 않는지 본다.

    왜 필요한가
      회차번호만 보면 동명이인을 집는다. 운영 SEIBro 293,088행 중 같은
      (발행사, 회차) 키에 2건 이상 몰린 키가 51,764개고, 실제로 '키움증권1863'은
      2022년분과 2026년분이 함께 있다. 틀린 ISIN이 들어가면 기준가·평가일이
      통째로 다른 상품 값으로 덮이므로, 비워 두는 편이 훨씬 안전하다. (2026-08-05)
    """

    def _seibro(self, isin, name, issuer, issue, expiry):
        return HistoricalIssue.objects.create(
            isin=isin, name=name, issuer=issuer, product_type="ELS",
            issue_date=issue, expiry_date=expiry)

    def _product(self, issuer, no, issue, expiry, real_issue=None):
        return Product.objects.create(
            issuer=issuer, product_no=no, name=no, issue_date=issue,
            expiry_date=expiry, real_issue_date=real_issue, yield_rate=6.0)

    def _resolve(self, p):
        from core.management.commands.backfill_product_code import (
            SeibroIndex, product_anchors, resolve)
        anchors = product_anchors(p)
        index = SeibroIndex.for_anchors(anchors)
        return resolve(index, p.issuer, p.product_no, anchors, p.expiry_date)

    def test_회차가_같아도_발행일이_다르면_옛_상품을_집지_않는다(self):
        self._seibro("KR6KW0000SG0", "키움증권1863(ELS)", "키움증권",
                     date(2022, 2, 18), date(2025, 2, 18))
        right = self._seibro("KR6KW0005DP2", "키움증권뉴글로벌100조1863(ELS)", "키움증권",
                             date(2026, 5, 4), date(2029, 5, 4))
        p = self._product("키움증권", "1863", date(2026, 5, 4), date(2029, 5, 4))
        status, rec, _ = self._resolve(p)
        self.assertEqual(status, "matched")
        self.assertEqual(rec[0], right.isin)

    def test_같은_발행일에_회차가_겹치면_아무것도_채우지_않는다(self):
        self._seibro("KR6KW0000AA1", "키움증권1863(ELS)", "키움증권",
                     date(2026, 5, 4), date(2029, 5, 4))
        self._seibro("KR6KW0000BB2", "키움증권뉴글로벌100조1863(ELS)", "키움증권",
                     date(2026, 5, 4), date(2029, 5, 4))
        p = self._product("키움증권", "1863", date(2026, 5, 4), date(2029, 5, 4))
        status, rec, _ = self._resolve(p)
        self.assertEqual(status, "ambiguous")
        self.assertIsNone(rec)

    def test_만기가_크게_다르면_다른_상품으로_보고_버린다(self):
        self._seibro("KR6KW0000CC3", "키움증권1863(ELS)", "키움증권",
                     date(2026, 5, 4), date(2029, 5, 4))
        p = self._product("키움증권", "1863", date(2026, 5, 4), date(2027, 5, 4))
        status, rec, _ = self._resolve(p)
        self.assertEqual(status, "expiry_conflict")
        self.assertIsNone(rec)

    def test_발행사_표기가_달라도_이어진다(self):
        h = self._seibro("KR6KB0000DD4", "KBable4159(ELS)", "케이비증권",
                         date(2026, 1, 16), date(2029, 1, 19))
        p = self._product("KB증권", "4159", date(2026, 1, 16), date(2029, 1, 19))
        status, rec, _ = self._resolve(p)
        self.assertEqual(status, "matched")
        self.assertEqual(rec[0], h.isin)

    def test_브랜드_접두가_숫자를_물면_접미일치로_찾는다(self):
        # NH는 회차 145를 "N2145"로 적는다 — 숫자열이 "2145"로 읽힌다
        h = self._seibro("KR6NH0005S44", "N2145(공모/ELS)", "NH투자증권",
                         date(2026, 1, 16), date(2029, 1, 16))
        p = self._product("NH투자증권", "145", date(2026, 1, 16), date(2029, 1, 16))
        status, rec, why = self._resolve(p)
        self.assertEqual(status, "matched")
        self.assertEqual(rec[0], h.isin)
        self.assertIn("접미", why)

    def test_접미일치_후보가_여럿이면_건너뛴다(self):
        self._seibro("KR6NH0000EE5", "N2145(공모/ELS)", "NH투자증권",
                     date(2026, 1, 16), date(2029, 1, 16))
        self._seibro("KR6NH0000FF6", "N3145(공모/ELS)", "NH투자증권",
                     date(2026, 1, 16), date(2029, 1, 16))
        p = self._product("NH투자증권", "145", date(2026, 1, 16), date(2029, 1, 16))
        status, rec, _ = self._resolve(p)
        self.assertEqual(status, "ambiguous")
        self.assertIsNone(rec)

    def test_회차가_두자리면_접미일치를_쓰지_않는다(self):
        # "1196"이 "96"으로 끝난다고 96회차로 볼 수는 없다
        self._seibro("KR6BS0000GG7", "BNK투자증권(ELS)1196", "비엔케이투자증권",
                     date(2026, 1, 16), date(2029, 1, 18))
        p = self._product("비엔케이투자증권", "96", date(2026, 1, 16), date(2029, 1, 18))
        status, rec, _ = self._resolve(p)
        self.assertEqual(status, "no_candidate")
        self.assertIsNone(rec)

    def test_real_issue_date가_있으면_그것으로_맞춘다(self):
        # issue_date(=청약종료일)는 하루 어긋나 있고 real_issue_date가 정확한 경우
        h = self._seibro("KR6HN0000HH8", "하나증권17342(ELS)", "하나증권",
                         date(2026, 1, 13), date(2029, 1, 15))
        p = self._product("하나증권", "17342", date(2026, 1, 12), date(2029, 1, 15),
                          real_issue=date(2026, 1, 13))
        status, rec, _ = self._resolve(p)
        self.assertEqual(status, "matched")
        self.assertEqual(rec[0], h.isin)

    def test_종목명_형태가_달라도_회차를_뽑는다(self):
        from core.management.commands.backfill_product_code import extract_seq
        cases = {
            "키움증권1863(ELS)": "1863",
            "키움증권뉴글로벌100조1863(ELS)": "1863",
            "NH투자증권2599(공모/ELB)": "2599",
            "삼성증권2535(사모/ELB)": "2535",
            "한국투자증권트루온(ELS)446": "446",
            "다올투자증권(사모/ELB)58": "58",
            "교보증권12614(ELB)사채": "12614",
            "한화스마트ONELB164(ELB)": "164",
        }
        for name, expect in cases.items():
            self.assertEqual(extract_seq(name), expect, name)

    def test_apply_없이는_아무것도_저장하지_않는다(self):
        from io import StringIO
        self._seibro("KR6KW0009XX1", "키움증권1900(ELS)", "키움증권",
                     date(2026, 5, 4), date(2029, 5, 4))
        p = self._product("키움증권", "1900", date(2026, 5, 4), date(2029, 5, 4))
        out = StringIO()
        call_command("backfill_product_code", stdout=out)
        p.refresh_from_db()
        self.assertEqual(p.product_code, "")
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertIn("유일 매칭 1건", out.getvalue())

    def test_이미_채워진_상품은_대상이_아니다(self):
        from io import StringIO
        self._seibro("KR6KW0009YY2", "키움증권1901(ELS)", "키움증권",
                     date(2026, 5, 4), date(2029, 5, 4))
        p = self._product("키움증권", "1901", date(2026, 5, 4), date(2029, 5, 4))
        p.product_code = "KR6KW0009ZZ3"
        p.save(update_fields=["product_code"])
        out = StringIO()
        call_command("backfill_product_code", stdout=out)
        self.assertIn("총 대상 0건", out.getvalue())
        # 기존 값이 SEIBro에 없다는 점은 별도로 보고된다
        self.assertIn("SEIBro에 없는 코드 1건", out.getvalue())

    def test_dry_run과_apply를_함께_주면_거부한다(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("backfill_product_code", "--dry-run", "--apply")


class BasePriceDateTest(SimpleTestCase):
    """최초기준가격 산정일 규칙 — 확정값 798건 전수검증으로 굳힌 규칙(2026-08-05)."""

    class _P:
        def __init__(self, **kw):
            self.issuer = kw.get("issuer", "")
            self.issue_date = kw.get("issue_date")
            self.sub_end = kw.get("sub_end")
            self.base_eval_date = kw.get("base_eval_date")
            self.real_issue_date = kw.get("real_issue_date")

    def _d(self, **kw):
        from core import market
        return market.base_price_date(self._P(**kw))

    def test_거래일_오프셋은_언제나_0이다(self):
        # back=1은 자산별 거래소 달력을 타서 해외자산에서 어긋났다 — 폐지했다.
        for kw in (dict(issuer="키움증권", issue_date=date(2026, 5, 4), sub_end=date(2026, 4, 30)),
                   dict(issuer="삼성증권", issue_date=date(2026, 5, 4)),
                   dict(issuer="대신증권", issue_date=date(2026, 5, 4)),
                   dict(issuer="NH투자증권", issue_date=date(2026, 7, 24),
                        sub_end=date(2026, 7, 24))):
            self.assertEqual(self._d(**kw)[1], 0)

    def test_설명서_확정값이_언제나_최우선이다(self):
        got, _ = self._d(issuer="키움증권", issue_date=date(2026, 5, 4),
                         sub_end=date(2026, 4, 30), base_eval_date=date(2026, 4, 29))
        self.assertEqual(got, date(2026, 4, 29))

    def test_키움삼성대신은_청약마감일이_기준일이다(self):
        for issuer in ("키움증권", "삼성증권", "대신증권"):
            got, _ = self._d(issuer=issuer, issue_date=date(2026, 5, 4),
                             sub_end=date(2026, 4, 30))
            self.assertEqual(got, date(2026, 4, 30), issuer)

    def test_청약마감일이_없으면_발행일_직전영업일이다(self):
        # 2026-05-04는 월요일 → 직전 영업일은 5/1(금)
        self.assertEqual(self._d(issuer="키움증권", issue_date=date(2026, 5, 4))[0],
                         date(2026, 5, 1))
        # 2026-05-11(월) → 5/8(금). 주말은 건너뛴다.
        self.assertEqual(self._d(issuer="삼성증권", issue_date=date(2026, 5, 11))[0],
                         date(2026, 5, 8))

    def test_그밖의_발행사는_실제발행일이_기준일이다(self):
        got, _ = self._d(issuer="한화투자증권", issue_date=date(2026, 5, 4),
                         sub_end=date(2026, 4, 30), real_issue_date=date(2026, 5, 6))
        self.assertEqual(got, date(2026, 5, 6))
        # real_issue_date가 없으면 issue_date가 곧 실제 발행일(엑셀 수입분)
        got, _ = self._d(issuer="한화투자증권", issue_date=date(2026, 5, 4),
                         sub_end=date(2026, 4, 30))
        self.assertEqual(got, date(2026, 5, 4))

    def test_KOFIA_수집분은_기존_오프셋표를_그대로_쓴다(self):
        # issue_date == sub_end 인 행만 오프셋 대상. 이번 검증에서 교차확인이 안 돼 유지.
        self.assertEqual(self._d(issuer="NH투자증권", issue_date=date(2026, 7, 24),
                                 sub_end=date(2026, 7, 24))[0], date(2026, 7, 25))
        self.assertEqual(self._d(issuer="신한투자증권", issue_date=date(2026, 7, 24),
                                 sub_end=date(2026, 7, 24))[0], date(2026, 7, 24))

    def test_규칙화_불가_발행사는_옛_로직_그대로다(self):
        # 유안타·유진은 어느 규칙에도 안 맞아 손대지 않았다 (조 팀장 판단 대기)
        for issuer in ("유안타증권", "유진투자증권"):
            self.assertEqual(self._d(issuer=issuer, issue_date=date(2026, 5, 4),
                                     sub_end=date(2026, 4, 30))[0], date(2026, 5, 4), issuer)
            self.assertEqual(self._d(issuer=issuer, issue_date=date(2026, 7, 24),
                                     sub_end=date(2026, 7, 24))[0], date(2026, 7, 24), issuer)

    def test_발행일이_없으면_기준일도_없다(self):
        self.assertEqual(self._d(issuer="키움증권"), (None, 0))


class DisclosedRefPriceTest(SimpleTestCase):
    """SEIBro 공시 기준가 매칭 — 틀린 값을 넣느니 폴백하는 쪽이 언제나 낫다."""

    def _m(self, assets_raw, seibro):
        from core import market
        return market.disclosed_asset_prices(assets_raw, seibro)

    def test_이름이_달라도_ISIN으로_맞춘다(self):
        # 서비스 'Micron' ↔ 공시 'MICRON TECHNOLOGY INC' — 이름으론 못 맞춘다.
        # 순서가 서로 뒤집혀 있어도 티커로 대응하므로 상관없다.
        got = self._m("Broadcom , Micron", [
            {"name": "MICRON TECHNOLOGY INC", "isin": "US5951121038", "std_price": "517.16"},
            {"name": "BROADCOM INC EXOF 005644980 SG9999014823", "isin": "US11135F1012",
             "std_price": "417.43"}])
        self.assertEqual(got["Micron"][0], 517.16)
        self.assertEqual(got["Broadcom"][0], 417.43)

    def test_국내지수는_공시값을_쓰지_않는다(self):
        # 서비스는 KODEX200 ETF(지수의 약 100배)를 현재가로 쓴다. 공시는 지수 포인트라
        # 그대로 넣으면 스케일이 깨진다. 레벨은 비율이라 폴백으로도 정확하다.
        got = self._m("KOSPI200 Index/Micron Technology", [
            {"name": "코스피 200지수", "isin": "KSD101000028", "std_price": "1126.33"},
            {"name": "MICRON TECHNOLOGY INC", "isin": "US5951121038", "std_price": "990.21"}])
        from core import market
        self.assertEqual(got["KOSPI200 Index"], (None, market.REF_SKIP_PROXY))
        self.assertEqual(got["Micron Technology"][0], 990.21)

    def test_공시값이_0이면_미확보로_본다(self):
        from core import market
        got = self._m("Micron", [{"name": "MICRON TECHNOLOGY INC",
                                  "isin": "US5951121038", "std_price": "0.0"}])
        self.assertEqual(got["Micron"], (None, market.REF_SKIP_ZERO))

    def test_자산_개수가_다르면_상품_전체를_폴백한다(self):
        from core import market
        got = self._m("Palantir , Micron", [
            {"name": "DOW JONES EURO STOXX 50 INDEX", "isin": "KSD310000145",
             "std_price": "4113.19"},
            {"name": "S&P 500 Index", "isin": "KSD310000568", "std_price": "4380.26"},
            {"name": "기아", "isin": "KR7000270009", "std_price": "79500"}])
        self.assertEqual({v[1] for v in got.values()}, {market.REF_SKIP_UNMATCHED})

    def test_자산이_1대1로_대응되지_않으면_폴백한다(self):
        # 같은 상품번호를 다른 상품이 쓰는 오매칭을 이 검사가 막는다
        from core import market
        got = self._m("Tesla , Micron", [
            {"name": "DOW JONES EURO STOXX 50 INDEX", "isin": "KSD310000145",
             "std_price": "3885.32"},
            {"name": "S&P 500 Index", "isin": "KSD310000568", "std_price": "4411.67"}])
        self.assertEqual({v[1] for v in got.values()}, {market.REF_SKIP_UNMATCHED})

    def test_공시값이_시세와_동떨어지면_기각한다(self):
        # SEIBro가 최초기준가격을 지수화 기준점(1·100·1000)으로 공시하는 상품이 있다
        from core import market
        self.assertEqual(market.pick_ref_price(517.16, 542.21), (517.16, "공시"))
        self.assertEqual(market.pick_ref_price(1.0, 542.21), (542.21, "폴백"))
        self.assertEqual(market.pick_ref_price(None, 542.21), (542.21, "폴백"))
        # 비교할 폴백이 없으면 정규화 상수만 걸러내고 공식값을 쓴다
        self.assertEqual(market.pick_ref_price(517.16, None), (517.16, "공시"))
        self.assertEqual(market.pick_ref_price(100.0, None), (None, "폴백"))


class PeakIssueGateTest(SimpleTestCase):
    """고점 게이트는 '발행 시점' 기준이다 — 10년 검증 스크립트와 같은 식.

    EC2 sweep_peak_relax.py가 확정 수치를 낼 때 쓴 식:
        past = s[(s.index >= 발행일−365일) & (s.index < 발행일)]
        pk   = 기준가 / past.max() * 100
    서비스 코드만 '오늘 종가'를 쓰고 있어 어긋나 있었다. 되돌린 뒤 같은 표본
    69,903건을 다시 돌려 타겟 5,392건·정상상환 99.68%·손실 17건을 재현했고,
    자산 단위 고점비율은 최대차 0.000000%p로 완전히 같았다. (2026-08-05)
    """

    ISSUE = date(2026, 1, 2)

    class _P:
        """게이트가 실제로 들여다보는 필드만 가진 가짜 상품."""

        id = 1
        product_code = ""
        assets_raw = "삼성전자"
        issuer = "한화투자증권"        # 기준일 = 실제 발행일 규칙(오프셋 없음)
        base_eval_date = None
        real_issue_date = None

        def __init__(self, **kw):
            self.__dict__.update(kw)

    def _hist(self, end, n, start_price=100.0, end_price=100.0):
        """end로 끝나는 영업일 n개 [(날짜, 종가)] — start_price에서 end_price로 직선."""
        days = []
        d = end
        while len(days) < n:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        days.reverse()
        step = (end_price - start_price) / (n - 1)
        return [(x, start_price + step * i) for i, x in enumerate(days)]

    def _peak(self, hist, as_of=None, **kw):
        from core.models import _peak_from_series
        return _peak_from_series(hist, as_of or self.ISSUE, 0, **kw)

    def _gate(self, p, hist):
        """fetch_history만 갈아끼우고 실제 v7_peak_gate를 돌린다."""
        from core.models import v7_peak_gate
        with mock.patch("core.market.fetch_history", return_value=hist), \
                mock.patch("core.market.resolve_ticker", return_value="005930.KS"):
            return v7_peak_gate(p, {})      # refs={} → DB 조회 없이 폴백 기준가

    # ── 창의 정의 ────────────────────────────────────────────────
    def test_게이트는_창에서_기준일_당일을_뺀다(self):
        # 검증 스크립트의 `s.index < str(h.issue_date)`와 같은 뜻.
        hist = self._hist(self.ISSUE, 200)
        hist[-1] = (self.ISSUE, 120.0)          # 기준일 당일이 최고가
        # 당일을 빼면 분모가 100이라 120%가 나온다 → 고점 발행으로 걸린다
        self.assertAlmostEqual(self._peak(hist, include_asof=False)[0], 120.0)
        # 화면용(당일 포함)은 100%를 넘기지 않는다 — 판정은 어차피 둘 다 95 이상
        self.assertAlmostEqual(self._peak(hist)[0], 100.0)

    def test_창은_거래일이_아니라_캘린더_365일이다(self):
        # fetch_history(days=N)의 N은 yfinance가 거래일로 해석한다(370d ≈ 1.5년).
        # 날짜로 자르지 않으면 1년보다 훨씬 먼 고점이 분모로 들어온다.
        hist = self._hist(self.ISSUE, 200)
        hist.insert(0, (self.ISSUE - timedelta(days=400), 200.0))   # 창 밖 고점
        self.assertAlmostEqual(self._peak(hist, include_asof=False)[0], 100.0)

    def test_창_안_거래일이_너무_적으면_값을_내지_않는다(self):
        self.assertEqual(self._peak(self._hist(self.ISSUE, 10), include_asof=False),
                         (None, None))

    # ── 분자(기준가) ─────────────────────────────────────────────
    def test_공시_최초기준가격이_분자다(self):
        hist = self._hist(self.ISSUE, 200)
        # 공시값이 시세와 어울리면 그대로 분자로 쓴다 (검증 스크립트와 동일)
        self.assertAlmostEqual(
            self._peak(hist, include_asof=False, disclosed=110.0)[0], 110.0)
        # 정규화 기준점(1·100·1000…)은 기각하고 산정일 종가로 폴백한다
        self.assertAlmostEqual(
            self._peak(hist, include_asof=False, disclosed=1.0)[0], 100.0)

    def test_직전_1년_상승률도_같은_분자로_잰다(self):
        # 검증 스크립트: ret1y = ref / past.iloc[0] − 1  (창 첫 종가 대비)
        hist = self._hist(self.ISSUE, 200, start_price=50.0, end_price=100.0)
        _, ret = self._peak(hist, include_asof=False)
        self.assertAlmostEqual(ret, 100.0)

    # ── 발행 시점 고정 = 비결정성 해소 ───────────────────────────
    def test_발행일_뒤_시세는_게이트_결과를_바꾸지_않는다(self):
        """워커가 과거 주차를 언제 다시 계산해도 같은 답이 나와야 한다.

        예전엔 closes[-1](오늘 종가)을 써서, 발행 뒤 폭락한 상품일수록
        게이트를 통과했다 — 취지와 정반대이면서 매일 답이 달라졌다.
        """
        p = self._P(issue_date=self.ISSUE, sub_end=self.ISSUE - timedelta(days=3))
        upto = self._hist(self.ISSUE, 200, start_price=50.0, end_price=100.0)
        after = [self.ISSUE + timedelta(days=i)
                 for i in range(1, 40) if (self.ISSUE + timedelta(days=i)).weekday() < 5]
        crash = upto + [(d, 40.0) for d in after]
        boom = upto + [(d, 400.0) for d in after]

        self.assertEqual(self._gate(p, crash), self._gate(p, boom))
        self.assertEqual(self._gate(p, crash), self._gate(p, upto))
        ok, peak = self._gate(p, crash)
        # 기준가가 창 최고와 사실상 같고 1년에 100% 올랐으니 고점 발행 — 탈락이 정답
        self.assertFalse(ok)
        self.assertGreaterEqual(peak, 95)

    def test_예전_게이트라면_폭락분이_통과했다(self):
        """위 테스트가 무엇을 막고 있는지 못 박아 둔다."""
        upto = self._hist(self.ISSUE, 200, start_price=50.0, end_price=100.0)
        crash = upto + [(self.ISSUE + timedelta(days=i), 40.0) for i in range(1, 40)]
        closes = [c for _, c in crash]
        self.assertLess(closes[-1] / max(closes) * 100, 95)   # 옛 식이면 통과

    # ── 완화 예외 ────────────────────────────────────────────────
    def test_완만한_우상향은_고점_부근이어도_통과한다(self):
        from core.models import RADAR_V7_RELAX_RET1Y
        p = self._P(issue_date=self.ISSUE, sub_end=self.ISSUE - timedelta(days=3))
        flat = self._hist(self.ISSUE, 200, start_price=100.0, end_price=100.0)
        ok, peak = self._gate(p, flat)
        self.assertTrue(ok)                     # 고점 100%지만 1년 상승률 0%
        self.assertGreaterEqual(peak, 95)
        self.assertEqual(RADAR_V7_RELAX_RET1Y, 15)

    def test_급등_뒤_고점이면_탈락한다(self):
        p = self._P(issue_date=self.ISSUE, sub_end=self.ISSUE - timedelta(days=3))
        surge = self._hist(self.ISSUE, 200, start_price=70.0, end_price=100.0)
        self.assertFalse(self._gate(p, surge)[0])   # 1년 상승률 42.9% > 15%

    # ── 발행 전(청약 중) ─────────────────────────────────────────
    def test_아직_발행_전이면_오늘_기준으로_잰다(self):
        from core.models import peak_as_of
        future = date.today() + timedelta(days=30)
        as_of, back, issued = peak_as_of(
            self._P(issue_date=future, sub_end=future - timedelta(days=3)))
        self.assertEqual((as_of, back, issued), (date.today(), 0, False))
        # 발행이 끝나면 그 시점으로 고정된다
        self.assertEqual(peak_as_of(self._P(issue_date=self.ISSUE,
                                            sub_end=self.ISSUE - timedelta(days=3))),
                         (self.ISSUE, 0, True))


class PeakDisplayVerdictTest(SimpleTestCase):
    """화면 문구·색은 게이트와 **같은 원값**으로 갈라야 한다.

    표시값을 반올림해 95와 비교하면 94.55~94.98%가 95%로 올라가, 게이트가
    통과시킨 상품에 "고점 부근에서 발행됐습니다"라는 정반대 문구와 빨간 숫자가
    붙었다(2026-08-06 운영 배지 26건 중 14건). 판정은 그대로 두고 표시 계층만
    게이트 결과를 그대로 받아쓰게 고친 것을 여기서 못 박는다.
    """

    ISSUE = date(2026, 1, 2)
    _P = PeakIssueGateTest._P
    _hist = PeakIssueGateTest._hist

    def _verdict(self, p, hist, ticker="005930.KS"):
        """fetch_history만 갈아끼우고 실제 peak_gate_verdict를 돌린다."""
        from core.models import peak_gate_verdict
        with mock.patch("core.market.fetch_history", return_value=hist), \
                mock.patch("core.market.resolve_ticker", return_value=ticker):
            return peak_gate_verdict(p, {})

    def _product(self):
        return self._P(issue_date=self.ISSUE, sub_end=self.ISSUE - timedelta(days=3))

    # ── 색 단계 ─────────────────────────────────────────────────
    def test_색_단계는_반올림_전_값으로_가른다(self):
        from core.models import peak_level
        # 실제로 걸렸던 값들 — round()를 거치면 전부 95가 된다
        for raw in (94.5525, 94.6678, 94.9789, 94.9794):
            self.assertEqual(round(raw), 95)          # 표시 숫자는 95가 맞다
            self.assertEqual(peak_level(raw), "mid")  # 그래도 '고점'은 아니다
        self.assertEqual(peak_level(95.0), "high")
        self.assertEqual(peak_level(98.2015), "high")
        self.assertEqual(peak_level(89.6), "low")     # round()면 90 → 'mid'였다
        self.assertIsNone(peak_level(None))

    def test_경고색은_게이트가_걸러낸_상품에만_쓴다(self):
        """문구는 "통과"라 해 놓고 숫자만 빨갛게 두면 그게 또 어긋난다."""
        from core.models import peak_tone
        # 탈락한 것만 warn
        self.assertEqual(peak_tone(98.2, True), "warn")
        # 통과분은 원값이 아무리 높아도 warn이 아니다 (완화 예외 12건이 여기)
        self.assertEqual(peak_tone(98.2, False), "watch")
        self.assertEqual(peak_tone(94.98, False), "watch")
        self.assertEqual(peak_tone(83.0, False), "ok")
        # 판정 못 한 경우도 경고색은 쓰지 않는다
        self.assertEqual(peak_tone(99.0, False), "watch")
        self.assertIsNone(peak_tone(None, False))

    # ── 세 갈래 판정 ─────────────────────────────────────────────
    def test_경계값은_게이트와_같은_편에_선다(self):
        """94.98% — 표시는 95%지만 게이트는 통과. 문구도 통과여야 한다."""
        hist = self._hist(self.ISSUE, 200)      # 200일 내내 100
        hist[-1] = (self.ISSUE, 94.98)          # 기준가만 94.98 → 고점대비 94.98%
        ok, relaxed, block, raw = self._verdict(self._product(), hist)
        self.assertAlmostEqual(raw, 94.98)
        self.assertEqual(round(raw), 95)        # 화면에는 95%로 찍힌다
        self.assertEqual((ok, relaxed, block), (True, False, False))

    def test_완화_예외도_화면에서는_그냥_통과다(self):
        """완화 예외는 내부 구분일 뿐 — 화면 분기는 gate_pass 하나로 끝난다.

        어느 경로로 통과했든 사용자에게는 "고점 발행 기준을 통과했다" 하나면
        된다는 것이 2026-08-06 태훈님 확정. 문구도 색도 나머지 통과분과 같다.
        """
        from core.models import peak_tone
        flat = self._hist(self.ISSUE, 200)      # 고점대비 100%, 1년 상승률 0%
        ok, relaxed, block, raw = self._verdict(self._product(), flat)
        self.assertEqual((ok, relaxed, block), (True, True, False))
        self.assertGreaterEqual(raw, 95)        # 원값은 고점 부근이 맞지만
        # 경고색은 게이트가 실제로 걸러낸 상품 전용이라 여기 오면 안 된다
        self.assertNotEqual(peak_tone(raw, block), "warn")
        # 경계 통과분(94.98%)과 색이 같아야 한다 — 갈라 보이면 안 된다
        self.assertEqual(peak_tone(raw, block), peak_tone(94.98, False))

    def test_급등_뒤_고점은_고점_발행으로_알려준다(self):
        surge = self._hist(self.ISSUE, 200, start_price=70.0, end_price=100.0)
        ok, relaxed, block, raw = self._verdict(self._product(), surge)
        self.assertEqual((ok, relaxed, block), (False, False, True))
        self.assertIsNotNone(raw)

    def test_시세를_못_구하면_어느_쪽도_주장하지_않는다(self):
        ok, relaxed, block, raw = self._verdict(
            self._product(), self._hist(self.ISSUE, 200), ticker=None)
        self.assertEqual((ok, relaxed, block, raw), (False, False, False, None))

    def test_판정은_게이트가_내린_그대로다(self):
        """peak_gate_verdict가 v7_peak_gate와 어긋나면 안 된다 — 규칙 복제 금지."""
        from core.models import v7_peak_gate
        p = self._product()
        cases = [self._hist(self.ISSUE, 200),
                 self._hist(self.ISSUE, 200, start_price=70.0, end_price=100.0),
                 self._hist(self.ISSUE, 200, start_price=99.0, end_price=100.0)]
        for hist in cases:
            with mock.patch("core.market.fetch_history", return_value=hist), \
                    mock.patch("core.market.resolve_ticker", return_value="005930.KS"):
                gate_ok, gate_peak = v7_peak_gate(p, {})
            ok, _relaxed, block, raw = self._verdict(p, hist)
            self.assertEqual(ok, gate_ok)
            self.assertEqual(block, not gate_ok)
            self.assertEqual(raw, gate_peak)

    # ── 템플릿이 다시 반올림값으로 돌아가지 않게 ──────────────────
    def test_템플릿은_반올림값으로_고점을_가르지_않는다(self):
        from pathlib import Path
        from django.conf import settings
        tpl = Path(settings.BASE_DIR) / "core" / "templates" / "core"
        detail = (tpl / "product_detail.html").read_text(encoding="utf-8")
        for banned in ("pk.issue_max < 95", "pk.issue_max >= 95",
                       "pk.now_max < 95", "pk.now_max >= 95"):
            self.assertNotIn(banned, detail)
        for need in ("pk.gate_pass", "pk.gate_block", "pk.issue_tone"):
            self.assertIn(need, detail)
        # 완화 예외를 화면에서 갈라 보이면 안 된다 (2026-08-06 태훈님 확정)
        self.assertNotIn("pk.gate_relaxed", detail)
        for name in ("weekly.html", "_mobile_row.html"):
            src = (tpl / name).read_text(encoding="utf-8")
            for banned in ("p.peak_ratio < 90", "p.peak_ratio < 95",
                           "p.peak_ratio >= 95"):
                self.assertNotIn(banned, src)
            self.assertIn("p.peak_level", src)


class RefreshRefPriceCommandTest(TestCase):
    """refresh_ref_price — 기본이 dry-run이고 --apply 없이는 절대 저장하지 않는다."""

    def setUp(self):
        from core.models import KnockInStatus
        self.user = get_user_model().objects.create_user("reftester", password="x")
        self.product = Product.objects.create(
            issuer="삼성증권", product_no="31255", product_code="KR6SS0008FQ0",
            assets_raw="Micron Technology", ki=50,
            issue_date=date(2026, 7, 23), sub_end=date(2026, 7, 23))
        HistoricalIssue.objects.create(
            isin="KR6SS0008FQ0", name="삼성증권31255(ELS)", issuer="삼성증권",
            product_type="ELS", issue_date=date(2026, 7, 24),
            assets=[{"name": "MICRON TECHNOLOGY INC", "isin": "US5951121038",
                     "std_price": "990.21"}])
        self.inv = Investment.objects.create(
            user=self.user, product=self.product, amount=10_000_000,
            invested_at=date(2026, 7, 23), status="보유중")
        self.st = KnockInStatus.objects.create(
            investment=self.inv, asset_name="Micron Technology", ticker="MU",
            ref_price=900.0, current_price=810.0, level_pct=90.0)

    def _run(self, *args):
        from io import StringIO
        out = StringIO()
        call_command("refresh_ref_price", "--no-fetch", *args, stdout=out)
        return out.getvalue()

    def test_dry_run은_보고만_하고_저장하지_않는다(self):
        text = self._run()
        self.assertIn("DRY-RUN", text)
        self.assertIn("공시값 채택       1", text)
        self.st.refresh_from_db()
        self.assertEqual(self.st.ref_price, 900.0)      # 그대로
        self.assertEqual(self.st.level_pct, 90.0)

    def test_apply를_주면_공시값으로_저장된다(self):
        self._run("--apply")
        self.st.refresh_from_db()
        self.assertEqual(self.st.ref_price, 990.21)
        self.assertEqual(self.st.level_pct, round(810.0 / 990.21 * 100, 1))

    def test_원인을_공시채택과_폴백규칙으로_나눠_센다(self):
        text = self._run()
        self.assertIn("공시채택  기준가 1건", text)
        self.assertIn("폴백규칙  기준가 0건", text)

    def test_낙인_판정이_뒤집히면_따로_보고한다(self):
        # 레벨 90 → 81.8 이면 KI 85 기준으로 '미돌파 → 돌파'가 된다
        self.product.ki = 85
        self.product.save(update_fields=["ki"])
        text = self._run()
        self.assertIn("낙인 돌파(레벨 ≤ KI) 판정이 뒤집히는 투자: 1건", text)
        self.assertIn("미돌파 → 돌파", text)

    def test_상품코드가_없으면_폴백을_유지한다(self):
        self.product.product_code = ""
        self.product.save(update_fields=["product_code"])
        text = self._run()
        self.assertIn("공시값 채택       0", text)
        self.assertIn("코드없음", text)

    def test_dry_run과_apply를_함께_주면_거부한다(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("refresh_ref_price", "--dry-run", "--apply")


class ImportElsColumnMappingTest(TestCase):
    """엑셀 열을 이름으로 찾는지 본다.

    ELS_Curator 엑셀은 주차마다 열 구성이 달라진다(실측 15종). 예전엔 열 번호를
    고정으로 읽어서, '청약종료일' 열이 아예 없는 파일(20260616_0925)의 그 자리에
    있던 낙인조건을 sub_end로 넣으려다 369행이 통째로 sub_end=NULL이 됐다.
    NULL은 SQLite 유니크 비교를 빠져나가므로 같은 상품이 두 행으로 갈라졌고
    중복 274쌍이 생겼다. (2026-08-05)
    """

    DESC = "[스텝다운] 3년/6개월/80-80-75-75-70-65/45 KI"

    def _write(self, tmpdir, name, header, rows):
        import openpyxl
        path = os.path.join(tmpdir, name)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "ALL"
        ws.append(header)
        for r in rows:
            ws.append(r)
        wb.save(path)
        return path

    def _run(self, header, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "청약중인상품_20260616_0925.xlsx", header, rows)
            with mock.patch("core.telegram.send_message"):
                call_command("import_els", file=path, no_notify=True)

    def test_열_순서가_바뀌어도_헤더로_찾는다(self):
        # 상품유형이 11번이 아니라 13번에 있는 레이아웃 (20260624_1200 계열)
        self._run(
            ["선택", "발행회사", "신용등급", "상품명", "기초자산", "발행일", "만기일",
             "연수익률", "최대손실률(%)", "청약시작일", "청약종료일", "micron여부",
             "지수형여부", "상품유형"],
            [[1, "키움증권", "AA", "1863", "삼성전자", 20260618, 20290618,
              7.5, -100, 20260608, 20260617, "N", "N", self.DESC]],
        )
        p = Product.objects.get(issuer="키움증권", product_no="1863")
        self.assertEqual(p.sub_end, date(2026, 6, 17))
        self.assertEqual(p.description, self.DESC)
        self.assertEqual(p.ki, 45)          # 설명을 제대로 읽어야 낙인이 나온다

    def test_청약종료일_열이_없으면_파일을_통째로_버린다(self):
        # sub_end=NULL로 밀어넣으면 유니크 제약을 빠져나가 중복이 쌓인다
        self._run(
            ["선택", "발행회사", "신용등급", "상품명", "기초자산", "발행일", "만기일",
             "연수익률", "최대손실률(%)", "청약시작일", "낙인조건", "상품유형"],
            [[1, "키움증권", "AA", "1863", "삼성전자", 20260618, 20290618,
              7.5, -100, 20260608, "KI45%", self.DESC]],
        )
        self.assertFalse(Product.objects.exists())

    def test_청약종료일_값이_비면_그_행만_건너뛴다(self):
        header = ["선택", "발행회사", "신용등급", "상품명", "기초자산", "발행일", "만기일",
                  "연수익률", "최대손실률(%)", "청약시작일", "청약종료일", "상품유형"]
        self._run(header, [
            [1, "키움증권", "AA", "1863", "삼성전자", 20260618, 20290618,
             7.5, -100, 20260608, None, self.DESC],
            [2, "키움증권", "AA", "1864", "삼성전자", 20260618, 20290618,
             7.5, -100, 20260608, 20260617, self.DESC],
        ])
        self.assertEqual([p.product_no for p in Product.objects.all()], ["1864"])
        self.assertFalse(Product.objects.filter(sub_end__isnull=True).exists())


class PriceBarStoreTest(TestCase):
    """시세 이력 저장소 — 조정/미조정 분리와 중복 방지가 지켜지는지.

    이 테이블의 존재 이유가 '요약하지 않은 원본'이라, 두 계열이 섞이거나
    같은 티커·날짜가 두 줄 생기면 그 위에서 계산하는 모든 지표가 조용히 틀어진다.
    네트워크를 쓰지 않는다 — 순수 DB 검증.
    """

    def _bar(self, ticker, d, close, adj, **kw):
        from core.models import PriceBar
        return PriceBar.objects.create(
            ticker=ticker, date=d, close=close, adj_close=adj, **kw)

    def test_같은_티커_같은_날짜는_두_줄_생기지_않는다(self):
        from django.db import IntegrityError
        self._bar("AAA", date(2026, 1, 5), 100.0, 90.0)
        with self.assertRaises(IntegrityError):
            self._bar("AAA", date(2026, 1, 5), 101.0, 91.0)

    def test_조정과_미조정이_서로_다른_계열로_나온다(self):
        from core.models import PriceBar
        self._bar("AAA", date(2026, 1, 5), 100.0, 90.0)
        self._bar("AAA", date(2026, 1, 6), 110.0, 99.0)
        # 낙인·기준가용 = 미조정
        self.assertEqual([v for _, v in PriceBar.closes_for_barrier("AAA")],
                         [100.0, 110.0])
        # 수익률·낙폭용 = 조정
        self.assertEqual([v for _, v in PriceBar.closes_for_return("AAA")],
                         [90.0, 99.0])

    def test_증분_이어받기는_보완행을_기준으로_삼지_않는다(self):
        """보완 행까지 최신일로 치면 원본이 복구돼도 그 구간을 다시 안 받는다."""
        from core.models import PriceBar
        self._bar("AAA", date(2026, 1, 5), 100.0, 100.0)
        self._bar("AAA", date(2026, 1, 9), 105.0, 105.0,
                  source=PriceBar.SOURCE_FILLED, source_ticker="BBB", scale=0.01)
        self.assertEqual(PriceBar.last_dates(["AAA"])["AAA"], date(2026, 1, 9))
        self.assertEqual(
            PriceBar.last_dates(["AAA"], primary_only=True)["AAA"], date(2026, 1, 5))

    def test_보완행은_출처가_데이터에_남는다(self):
        from core.models import PriceBar
        self._bar("AAA", date(2026, 1, 9), 105.0, 105.0,
                  source=PriceBar.SOURCE_FILLED, source_ticker="BBB", scale=0.01)
        row = PriceBar.objects.get(ticker="AAA", date=date(2026, 1, 9))
        self.assertEqual(row.source, PriceBar.SOURCE_FILLED)
        self.assertEqual(row.source_ticker, "BBB")
        self.assertEqual(row.scale, 0.01)
        self.assertIsNone(row.volume)      # 다른 계열 거래량은 옮기지 않는다


class SyncPricesStaleTest(SimpleTestCase):
    """정체 판정은 같은 시장끼리 비교해야 연휴에 오경보가 나지 않는다."""

    def test_시장_분류(self):
        from core.management.commands.sync_prices import market_of
        self.assertEqual(market_of("005930.KS"), "KR")
        self.assertEqual(market_of("035760.KQ"), "KR")
        self.assertEqual(market_of("^KS200"), "KR")     # 지수도 국내 달력
        self.assertEqual(market_of("^N225"), "JP")
        self.assertEqual(market_of("^HSCE"), "HK")
        self.assertEqual(market_of("^STOXX50E"), "EU")
        self.assertEqual(market_of("AAPL"), "US")
        self.assertEqual(market_of("^GSPC"), "US")

    def test_국내_연휴는_동료끼리_같이_밀려_경보가_안_난다(self):
        """추석에 국내 티커가 전부 11일 밀려도, 미국 티커와 비교하지 않으므로 조용하다."""
        from core.management.commands.sync_prices import market_of
        eff = {"005930.KS": date(2026, 9, 25), "000660.KS": date(2026, 9, 25),
               "^KS200": date(2026, 9, 25), "AAPL": date(2026, 10, 6)}
        peer_max, peer_n = {}, {}
        for t, d in eff.items():
            m = market_of(t)
            peer_max[m] = max(peer_max.get(m, d), d)
            peer_n[m] = peer_n.get(m, 0) + 1
        self.assertEqual(peer_n["KR"], 3)
        # 국내 3종은 자기들끼리 최신 → 뒤처짐 0일
        for t in ("005930.KS", "000660.KS", "^KS200"):
            self.assertEqual((peer_max["KR"] - eff[t]).days, 0)
