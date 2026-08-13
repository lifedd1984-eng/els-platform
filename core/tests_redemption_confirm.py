"""조기상환 확정 대기 흐름 + 개인 투자 알림 스코프.

배경 (2026-08-10 조 팀장 지시로 만든 테스트)
    같은 상품(키움 1863)을 두 건 보유한 상태에서 판정 알림이 발행사+상품번호만
    적어 두 건이 구분되지 않았고, 최초 1회만 알리는 구조라 한 건(inv694)이
    일주일간 확정되지 않은 채 방치됐다. 여기서 고정하는 것은 세 가지다.
      ① 충족 판정이 난 건은 '상환 확정 대기'로 화면 맨 위에 뜨고, 예상 금액을
         고쳐 확정하면 그 값이 저장된다.
      ② 예상 금액은 Investment.schedule 한 곳에서만 나온다 — 실제 입금액과 대조.
      ③ 텔레그램 개인 투자 알림은 지정 계정 것만 나간다(웹 푸시는 계정별로 유지).
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Investment, Product, RedemptionVerdict

# 운영 실측값 (2026-08-10 운영 DB 읽기 전용 조회)
#   product 1204 = 키움증권 1863 · 연 26.28% · 3개월 주기 · 1회차 평가일 2026-07-30
#   inv552 admin    5,000,000원 → 2026-08-04 실제 상환 5,328,500원 (+6.57%)
#   inv694 jey0916 20,000,000원 → 판정 2026-08-03, 8/10까지 미확정 방치
KIWOOM_1863 = dict(
    issuer="키움증권", product_no="1863", yield_rate=26.28,
    barriers_raw=[85, 85, 80, 80, 75, 75, 75, 75, 70, 70, 60, 60],
    period_months=3, ki=30,
    issue_date=date(2026, 5, 4), expiry_date=date(2029, 5, 4),
    eval_dates=["2026-07-30", "2026-10-30", "2027-01-29", "2027-04-30",
                "2027-07-30", "2027-10-29", "2028-01-28", "2028-04-28",
                "2028-07-28", "2028-10-30", "2029-01-30", "2029-05-04"],
)
INV552_ACTUAL = 5_328_500      # 증권사 실제 입금액
INV694_EXPECTED = 21_314_000   # 같은 식을 20,000,000원에 적용한 값


class _Base(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="admin", password="x")
        self.other = User.objects.create_user(username="jey0916", password="x")
        self.product = Product.objects.create(**KIWOOM_1863)

    def _inv(self, user, amount, account=""):
        return Investment.objects.create(
            user=user, product=self.product, amount=amount,
            invested_at=date(2026, 5, 4), broker_account=account, status="보유중")

    def _verdict(self, inv, met=True, notified_days_ago=None,
                 round_no=1, eval_date=date(2026, 7, 30)):
        v = RedemptionVerdict.objects.create(
            investment=inv, round_no=round_no, eval_date=eval_date,
            barrier=85.0, worst_level=93.1, met=met)
        if notified_days_ago is not None:
            ts = timezone.now() - timedelta(days=notified_days_ago)
            RedemptionVerdict.objects.filter(pk=v.pk).update(
                notified_at=ts, last_notified_at=ts)
            v.refresh_from_db()
        return v


class 예상상환금계산(_Base):
    """계산은 Investment.schedule 한 곳에서만 — 실제 입금액으로 검증한다."""

    def test_inv552_실제입금액을_그대로_재현한다(self):
        inv = self._inv(self.admin, 5_000_000)
        self._verdict(inv)
        self.assertEqual(inv.schedule[0]["expected"], INV552_ACTUAL)
        self.assertEqual(inv.redemption_expected, INV552_ACTUAL)

    def test_세후환산은_실제입금액과_어긋난다(self):
        """세후값을 자동채움으로 쓰면 안 되는 근거를 고정한다."""
        inv = self._inv(self.admin, 5_000_000)
        self.assertNotEqual(inv.schedule[0]["expected_after_tax"], INV552_ACTUAL)

    def test_inv694_같은_식으로_계산된다(self):
        inv = self._inv(self.other, 20_000_000, "키움증권")
        self._verdict(inv)
        self.assertEqual(inv.redemption_expected, INV694_EXPECTED)

    def test_판정이_없으면_예상금액도_없다(self):
        self.assertIsNone(self._inv(self.admin, 5_000_000).redemption_expected)

    def test_미충족_판정은_확정대기가_아니다(self):
        inv = self._inv(self.admin, 5_000_000)
        self._verdict(inv, met=False)
        self.assertIsNone(inv.redemption_pending)
        self.assertIsNone(inv.redemption_expected)


class 확정대기화면(_Base):
    def setUp(self):
        super().setUp()
        self.inv = self._inv(self.other, 20_000_000, "키움증권")
        self._verdict(self.inv)
        self.client.force_login(self.other)

    def test_확정대기_카드에_건이_특정되게_뜬다(self):
        html = self.client.get("/portfolio/").content.decode()
        self.assertIn("상환 확정 대기 1건", html)
        self.assertIn("키움증권 1863", html)
        self.assertIn("20,000,000원", html)       # 같은 상품 두 건을 가르는 값
        self.assertIn("키움증권</span>", html)     # 계좌 메모 배지
        self.assertIn("21,314,000", html)         # 예상 상환금 자동채움
        self.assertIn("상환 확정", html)

    def test_확정대기_건은_보유목록에서_빠진다(self):
        """목록에 섞여 있으면 그냥 지나친다 — 그게 이번 사고의 경로였다."""
        html = self.client.get("/portfolio/").content.decode()
        self.assertIn("보유 중 0건", html)

    def test_확정하면_조기상환으로_바뀌고_금액이_저장된다(self):
        self.client.post("/portfolio/", {
            "action": "redeem", "id": self.inv.id, "status": "조기상환",
            "redeemed_at": "2026-08-10", "redeemed_amount": "21,314,000",
        })
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, "조기상환")
        self.assertEqual(self.inv.redeemed_amount, INV694_EXPECTED)
        self.assertEqual(self.inv.redeemed_at, date(2026, 8, 10))

    def test_확정전에_금액을_고치면_고친_값이_저장된다(self):
        """예상값은 자동채움일 뿐 — 실제 입금액이 다를 수 있다."""
        self.client.post("/portfolio/", {
            "action": "redeem", "id": self.inv.id, "status": "조기상환",
            "redeemed_at": "2026-08-10", "redeemed_amount": "21,300,000",
        })
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.redeemed_amount, 21_300_000)

    def test_확정하면_확정대기에서_사라진다(self):
        self.client.post("/portfolio/", {
            "action": "redeem", "id": self.inv.id, "status": "조기상환",
            "redeemed_at": "2026-08-10", "redeemed_amount": "21,314,000",
        })
        html = self.client.get("/portfolio/").content.decode()
        self.assertNotIn("상환 확정 대기", html)

    def test_남의_투자는_보이지_않는다(self):
        self.client.force_login(self.admin)
        self.assertNotIn("상환 확정 대기", self.client.get("/portfolio/").content.decode())


@override_settings(TELEGRAM_ALERT_USERNAMES=["admin"])
class 미확정재알림(_Base):
    """최초 1회만 알리던 게 이번 사고의 직접 원인 — N영업일마다 다시 알린다."""

    def _run(self, **kw):
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("check_redemptions", stdout=mock.MagicMock(), **kw)
        return tg

    def test_5영업일_지나면_다시_알린다(self):
        inv = self._inv(self.admin, 5_000_000, "키움 CMA")
        self._verdict(inv, notified_days_ago=9)   # 9일 전 = 영업일 7일 전
        tg = self._run()
        sent = "\n".join(c.args[0] for c in tg.call_args_list)
        self.assertIn("조기상환 미확정", sent)
        self.assertIn("키움증권 1863", sent)
        self.assertIn("5,000,000원", sent)        # 건을 특정할 수 있어야 한다
        self.assertIn("키움 CMA", sent)
        self.assertIn("5,328,500원", sent)        # 예상 상환금

    def test_5영업일_전에는_다시_알리지_않는다(self):
        inv = self._inv(self.admin, 5_000_000)
        self._verdict(inv, notified_days_ago=2)
        self.assertEqual(self._run().call_count, 0)

    def test_재알림_후_시각이_갱신돼_매일_울리지_않는다(self):
        inv = self._inv(self.admin, 5_000_000)
        v = self._verdict(inv, notified_days_ago=9)
        self._run()
        v.refresh_from_db()
        self.assertGreater(v.last_notified_at, timezone.now() - timedelta(minutes=5))
        self.assertEqual(self._run().call_count, 0)   # 바로 다시 돌려도 조용하다

    def test_확정된_건은_재알림하지_않는다(self):
        inv = self._inv(self.admin, 5_000_000)
        self._verdict(inv, notified_days_ago=9)
        inv.status = "조기상환"
        inv.save()
        self.assertEqual(self._run().call_count, 0)

    def test_알림기록이_없는_옛_행도_판정시각_기준으로_알린다(self):
        """이 기능 이전에 쌓인 행(notified_at=NULL)이 영영 안 알려지면 안 된다."""
        inv = self._inv(self.admin, 5_000_000)
        v = self._verdict(inv)
        RedemptionVerdict.objects.filter(pk=v.pk).update(
            checked_at=timezone.now() - timedelta(days=9))
        self.assertEqual(self._run().call_count, 1)

    def test_주기는_옵션으로_바꿀_수_있다(self):
        inv = self._inv(self.admin, 5_000_000)
        self._verdict(inv, notified_days_ago=9)
        self.assertEqual(self._run(remind_days=30).call_count, 0)

    def test_no_notify면_아무것도_보내지_않는다(self):
        inv = self._inv(self.admin, 5_000_000)
        self._verdict(inv, notified_days_ago=9)
        self.assertEqual(self._run(no_notify=True).call_count, 0)


@override_settings(TELEGRAM_ALERT_USERNAMES=["admin"])
class 텔레그램스코프(_Base):
    """공용 채널이라 전 계정 투자가 한 방에 섞여 온다 — 지정 계정 것만 남긴다."""

    def test_admin_외_계정_투자는_재알림이_나가지_않는다(self):
        inv = self._inv(self.other, 20_000_000, "키움증권")
        self._verdict(inv, notified_days_ago=9)
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("check_redemptions", stdout=mock.MagicMock())
        self.assertEqual(tg.call_count, 0)

    @override_settings(TELEGRAM_REDEMPTION_ALERT_ENABLED=True)
    def test_평가일_D_1_텔레그램은_admin_것만_나간다(self):
        # 발송 자체는 2026-08-11부터 기본 꺼짐이다. 여기서 고정하려는 것은
        # '켜져 있을 때 지정 계정 것만 나간다'이므로 토글만 켜고 본다.
        # D-7은 같은 날 지시로 없어져서(notify.notify_redemptions) D-1로 본다.
        from core.notify import notify_redemptions
        self._inv(self.other, 20_000_000)
        admin_inv = self._inv(self.admin, 5_000_000)
        # 다음 평가일은 오늘 기준으로 정해지므로 날짜를 박지 않는다
        one = admin_inv.next_evaluation["date"] - timedelta(days=1)
        with mock.patch("core.notify.date") as d, \
             mock.patch("core.telegram.send_message", return_value=True) as tg, \
             mock.patch("core.push.send_to_user", return_value=1) as pu:
            d.today.return_value = one
            notify_redemptions()
        self.assertEqual(tg.call_count, 1)
        self.assertIn("5,000,000원", tg.call_args_list[0].args[0])
        # 웹 푸시는 계정별로 가므로 제한하지 않는다 — 두 계정 모두 받는다
        self.assertEqual(pu.call_count, 2)

    def test_설정이_비면_전_계정에_보낸다(self):
        """되돌리기 — .env만 비우면 원래 동작으로 돌아온다."""
        inv = self._inv(self.other, 20_000_000)
        self._verdict(inv, notified_days_ago=9)
        with override_settings(TELEGRAM_ALERT_USERNAMES=[]), \
             mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("check_redemptions", stdout=mock.MagicMock())
        self.assertEqual(tg.call_count, 1)

    def test_주간요약도_admin_보유분만_싣는다(self):
        """2026-08-10 조 팀장 확정 — 요약 안에 여러 계정 투자가 섞여 나가던 것을 맞췄다."""
        from core.models import KnockInStatus
        from core.notify import notify_weekly_digest
        for user in (self.admin, self.other):
            inv = self._inv(user, 5_000_000)
            KnockInStatus.objects.create(
                investment=inv, asset_name="S&P500", ref_price=100.0,
                current_price=32.0, level_pct=32.0)   # KI 30 → 버퍼 2%p = 위험
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            notify_weekly_digest()
        text = tg.call_args_list[0].args[0]
        self.assertIn("7일내 조기상환 평가 0건", text)   # 두 건 다 평가일이 7일 밖
        self.assertIn("위험 1 / 경고 0 / 안전 0", text)  # admin 것만 세었다

    def test_주간요약은_제한이_없으면_전_계정을_센다(self):
        from core.models import KnockInStatus
        from core.notify import notify_weekly_digest
        for user in (self.admin, self.other):
            inv = self._inv(user, 5_000_000)
            KnockInStatus.objects.create(
                investment=inv, asset_name="S&P500", ref_price=100.0,
                current_price=32.0, level_pct=32.0)
        with override_settings(TELEGRAM_ALERT_USERNAMES=[]), \
             mock.patch("core.telegram.send_message", return_value=True) as tg:
            notify_weekly_digest()
        self.assertIn("위험 2 / 경고 0 / 안전 0", tg.call_args_list[0].args[0])

    def test_배치보고는_계정을_가리지_않는다(self):
        """notify_batch는 서비스 전체 상태 보고라 범위 밖이다."""
        self._inv(self.other, 20_000_000)
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("notify_batch", results="scrape=0", stdout=mock.MagicMock())
        self.assertEqual(tg.call_count, 1)


@override_settings(TELEGRAM_ALERT_USERNAMES=["admin"])
class 오판정무시(_Base):
    """증권사 확인 결과 상환이 아니면 그 회차 재알림만 멈춘다."""

    def setUp(self):
        super().setUp()
        self.inv = self._inv(self.admin, 5_000_000, "키움 CMA")
        self.client.force_login(self.admin)

    def _dismiss(self, round_no=1, undo=False):
        return self.client.post("/portfolio/", {
            "action": "undismiss_verdict" if undo else "dismiss_verdict",
            "id": self.inv.id, "round_no": round_no,
        })

    def test_무시_버튼이_확정대기_카드에_있다(self):
        self._verdict(self.inv)
        html = self.client.get("/portfolio/").content.decode()
        self.assertIn("이 판정 무시", html)
        self.assertNotIn("무시함", html)

    def test_무시하면_배지가_뜨고_숨지_않는다(self):
        self._verdict(self.inv)
        self._dismiss()
        html = self.client.get("/portfolio/").content.decode()
        self.assertIn("무시함", html)
        self.assertIn("키움증권 1863", html)          # 완전히 숨기지 않는다
        self.assertIn("무시 해제", html)
        self.assertIn("상환 확정 대기 0건", html)      # 할 일 건수에선 빠진다
        self.assertIn("(무시 1건)", html)

    def test_무시하면_재알림이_멈춘다(self):
        self._verdict(self.inv, notified_days_ago=9)
        self._dismiss()
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("check_redemptions", stdout=mock.MagicMock())
        self.assertEqual(tg.call_count, 0)

    def test_무시를_해제하면_다시_알린다(self):
        self._verdict(self.inv, notified_days_ago=9)
        self._dismiss()
        self._dismiss(undo=True)
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("check_redemptions", stdout=mock.MagicMock())
        self.assertEqual(tg.call_count, 1)

    def test_무시해도_다음_회차는_정상_알린다(self):
        """한 번 틀렸다고 다음 회차까지 막으면 안 된다."""
        self._verdict(self.inv, notified_days_ago=9)
        self._dismiss(round_no=1)
        self._verdict(self.inv, notified_days_ago=9,
                      round_no=2, eval_date=date(2026, 10, 30))
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("check_redemptions", stdout=mock.MagicMock())
        self.assertEqual(tg.call_count, 1)
        self.assertIn("2회차", tg.call_args_list[0].args[0])

    def test_무시해도_확정은_그대로_할_수_있다(self):
        self._verdict(self.inv)
        self._dismiss()
        self.client.post("/portfolio/", {
            "action": "redeem", "id": self.inv.id, "status": "조기상환",
            "redeemed_at": "2026-08-10", "redeemed_amount": "5,328,500",
        })
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, "조기상환")

    def test_남의_판정은_무시할_수_없다(self):
        other_inv = self._inv(self.other, 20_000_000)
        v = self._verdict(other_inv)
        self.client.post("/portfolio/", {
            "action": "dismiss_verdict", "id": other_inv.id, "round_no": 1})
        v.refresh_from_db()
        self.assertIsNone(v.dismissed_at)
