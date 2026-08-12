"""텔레그램 알림 3종 비활성화 + KOFIA 수집 알림 상품 목록.

2026-08-11 조 팀장 지시
  ① 평가일 D-7/D-1 임박   (core/notify.py notify_redemptions)
  ② 낙인 근접 경보        (update_prices _maybe_alert)
  ③ 신규 프리셋 매칭      (core/notify.py notify_preset_matches)
  위 3종은 **텔레그램 발송만** 끈다. 코드를 지우지 않고 설정 토글로 막았다.
  ④ KOFIA 자동수집 완료 알림에 신규 상품 목록(발행사·상품번호)을 싣는다.

여기서 고정하는 것
  · 기본값(꺼짐)에서 telegram.send_message가 아예 호출되지 않는다
  · 그때도 DB 기록(RedemptionAlert·KnockInAlert·KnockInStatus·NotifiedMatch)과
    웹 푸시는 그대로 남는다 — 웹 푸시는 계정별 채널이라 공용 텔레그램과 별개다
  · 설정을 켜면 예전 그대로 나간다 (.env 한 줄로 되돌리는 경로)
  · KOFIA 완료 알림에 신규 상품이 나열되고, 상한을 넘으면 "... 외 N건"으로 접힌다
"""

import itertools
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from core.management.commands.scrape_kofia import NEW_LIST_LIMIT
from core.management.commands.update_prices import Command as UpdatePricesCommand
from core.models import (
    Investment, KnockInAlert, KnockInStatus, NotifiedMatch, Preset, Product,
    RedemptionAlert,
)

TODAY = date.today()

# 평가일이 확정된 상품 — 평가일 D-7 계산이 근사(기준일+N개월)에 흔들리지 않게 박는다.
KIWOOM_1863 = dict(
    issuer="키움증권", product_no="1863", name="키움증권(ELS) 1863",
    product_type="ELS", yield_rate=26.28, ki=30, is_no_ki=False,
    barriers_raw=[85, 85, 80, 80, 75, 75, 75, 75, 70, 70, 60, 60],
    barrier_first=85, barrier_last=60, period_months=3,
    assets_raw="KOSPI200 Index", asset_type="지수형", currency="KRW",
    issue_date=date(2026, 5, 4), expiry_date=date(2029, 5, 4),
    sub_end=date(2026, 5, 2),
    eval_dates=["2026-07-30", "2026-10-30", "2027-01-29", "2027-04-30",
                "2027-07-30", "2027-10-29", "2028-01-28", "2028-04-28",
                "2028-07-28", "2028-10-30", "2029-01-30", "2029-05-04"],
)


class _InvestmentBase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="admin", password="x")
        self.product = Product.objects.create(**KIWOOM_1863)
        self.inv = Investment.objects.create(
            user=self.user, product=self.product, amount=5_000_000,
            invested_at=date(2026, 5, 4), status="보유중")


class 평가일임박알림(_InvestmentBase):
    """① 평가일 D-7/D-1 — 텔레그램만 끄고 RedemptionAlert·웹 푸시는 남긴다."""

    def _run(self):
        """다음 평가일 7일 전으로 notify의 today를 고정하고 돌린다."""
        from core.notify import notify_redemptions
        seven = self.inv.next_evaluation["date"] - timedelta(days=7)
        with mock.patch("core.notify.date") as d, \
             mock.patch("core.telegram.send_message", return_value=True) as tg, \
             mock.patch("core.push.send_to_user", return_value=1) as pu:
            d.today.return_value = seven
            notify_redemptions()
        return tg, pu

    def test_기본값이면_텔레그램이_나가지_않는다(self):
        tg, _ = self._run()
        self.assertEqual(tg.call_count, 0)

    def test_꺼져_있어도_DB_기록은_남는다(self):
        self._run()
        alert = RedemptionAlert.objects.get(investment=self.inv)
        self.assertEqual(alert.alert_type, "D-7")

    def test_꺼져_있어도_웹_푸시는_그대로_간다(self):
        _, pu = self._run()
        self.assertEqual(pu.call_count, 1)

    @override_settings(TELEGRAM_REDEMPTION_ALERT_ENABLED=True)
    def test_설정을_켜면_예전처럼_나간다(self):
        tg, _ = self._run()
        self.assertEqual(tg.call_count, 1)
        self.assertIn("[상환 평가 D-7] 키움증권 1863", tg.call_args.args[0])


class 낙인근접경보(_InvestmentBase):
    """② 낙인 근접 — 텔레그램만 끄고 KnockInStatus·KnockInAlert는 남긴다."""

    def _status(self, level_pct=32.0):
        """KI 30 기준 버퍼 2%p = '위험' 구간."""
        return KnockInStatus.objects.create(
            investment=self.inv, asset_name="KOSPI200 Index", ticker="^KS200",
            ref_price=100.0, current_price=level_pct, level_pct=level_pct)

    def _alert(self):
        cmd = UpdatePricesCommand()
        cmd.stdout = mock.MagicMock()
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            cmd._maybe_alert(self.inv)
        return tg

    def test_기본값이면_텔레그램이_나가지_않는다(self):
        self._status()
        self.assertEqual(self._alert().call_count, 0)

    def test_꺼져_있어도_경보_이력은_남는다(self):
        """이력이 안 남으면 다시 켰을 때 밀린 경보가 한꺼번에 쏟아진다."""
        self._status()
        self._alert()
        self.assertEqual(
            list(KnockInAlert.objects.values_list("level_band", flat=True)), ["위험"])

    def test_배치가_낙인_거리를_그대로_갱신한다(self):
        """판정·DB 기록은 손대지 않았다 — 시세만 끊고 배치를 통째로 돌린다."""
        with mock.patch("core.market.resolve_ticker", return_value="^KS200"), \
             mock.patch("core.market.fetch_current_price", return_value=32.0), \
             mock.patch("core.market.fetch_price_on", return_value=100.0), \
             mock.patch("core.telegram.send_message", return_value=True) as tg:
            call_command("update_prices", stdout=mock.MagicMock())
        status = KnockInStatus.objects.get(investment=self.inv)
        self.assertEqual(status.level_pct, 32.0)
        self.assertEqual(self.inv.ki_buffer, 2.0)
        self.assertTrue(KnockInAlert.objects.filter(level_band="위험").exists())
        self.assertEqual(tg.call_count, 0)

    @override_settings(TELEGRAM_KNOCKIN_ALERT_ENABLED=True)
    def test_설정을_켜면_예전처럼_나간다(self):
        self._status()
        tg = self._alert()
        self.assertEqual(tg.call_count, 1)
        self.assertIn("[낙인 위험] 키움증권 1863", tg.call_args.args[0])


def _match_product(product_no, yield_rate=30.0):
    """프리셋(수익률 25% 이상)에 걸리는 청약중 상품 하나."""
    return Product.objects.create(
        issuer="미래에셋증권", product_no=product_no, name=f"미래에셋(ELS) {product_no}",
        product_type="ELS", yield_rate=yield_rate, ki=45, is_no_ki=False,
        barrier_first=90, barrier_last=65, assets_raw="KOSPI200 Index",
        asset_type="지수형", currency="KRW", period_months=6,
        sub_end=TODAY + timedelta(days=3))


class 프리셋매칭알림(TestCase):
    """③ 신규 프리셋 매칭 — 텔레그램만 끄고 NotifiedMatch는 남긴다."""

    def setUp(self):
        self.preset = Preset.objects.create(
            name="나의플랜", asset_type="전체", ki_min=None, ki_max=None,
            include_no_ki=False, yield_min=25.0, notify=True)
        self.product = _match_product("7001")

    def _run(self):
        from core import notify
        with mock.patch("core.telegram.send_message", return_value=True) as tg:
            notify.notify_preset_matches()
        return tg

    def test_기본값이면_텔레그램이_나가지_않는다(self):
        self.assertEqual(self._run().call_count, 0)

    def test_꺼져_있어도_매칭_기록은_남는다(self):
        """기록을 안 남기면 매 배치가 같은 상품을 신규로 다시 센다."""
        self._run()
        self.assertTrue(
            NotifiedMatch.objects.filter(preset=self.preset, product=self.product).exists())

    def test_같은_상품을_두_번_신규로_세지_않는다(self):
        self._run()
        self._run()
        self.assertEqual(NotifiedMatch.objects.count(), 1)

    @override_settings(TELEGRAM_PRESET_MATCH_ALERT_ENABLED=True)
    def test_설정을_켜면_예전처럼_나간다(self):
        tg = self._run()
        self.assertEqual(tg.call_count, 1)
        text = tg.call_args.args[0]
        self.assertIn("[프리셋 매칭] 나의플랜 — 신규 1건", text)
        self.assertIn("미래에셋증권 7001", text)


def _kofia_row(product_no, **kw):
    """KOFIA 응답 한 줄 (kofia_scraper.fetch_subscribing 반환 형식)."""
    base = dict(
        issuer="NH투자증권", product_no=product_no,
        product_code=f"KR6NH{product_no}", name=f"NH투자증권(ELS) {product_no}",
        assets_raw="KOSDAQ150 Index",
        description="원금비보장, 80-80-80-80-75-70/30 KI",
        broker_url="", prospectus_url="", yield_rate=20.2, max_loss=-100.0,
        issue_date=date(2026, 8, 12), expiry_date=date(2029, 8, 13),
        sub_start=date(2026, 8, 4), sub_end=date(2026, 8, 12),
    )
    base.update(kw)
    return base


_run_seq = itertools.count()


class KOFIA수집완료알림(TestCase):
    """④ KOFIA 자동수집 완료 알림 — 건수만 오던 것에 신규 상품 목록을 싣는다.

    대상 근거: '수집'을 KOFIA에서 하는 배치는 scrape_kofia 하나다
    (import_els는 ELS_Curator.exe가 만든 downloads 엑셀을 읽는 별개 경로 — README 참조).
    """

    def _run(self, rows):
        with mock.patch("core.kofia_scraper.fetch_subscribing", return_value=rows), \
             mock.patch("core.telegram.send_message", return_value=True) as tg, \
             mock.patch("core.notify.notify_preset_matches"), \
             mock.patch("core.management.commands.scrape_kofia.timezone_today",
                        side_effect=lambda: f"t{next(_run_seq)}"):
            call_command("scrape_kofia", stdout=mock.MagicMock())
        # 완료 알림은 항상 마지막 한 통이다 (앞에 결손·주기 경보가 붙을 수 있다)
        return tg.call_args_list[-1].args[0]

    def test_신규_상품이_발행사_상품번호로_나열된다(self):
        text = self._run([_kofia_row("25065"), _kofia_row("25066")])
        self.assertIn("전체 2건 / 신규 2건", text)
        self.assertIn("- NH투자증권 25065", text)
        self.assertIn("- NH투자증권 25066", text)

    def test_목록에_낙인과_쿠폰이_같이_나온다(self):
        # _kofia_row 기본 description "80-80-80-80-75-70/30 KI" → 낙인 30.
        text = self._run([_kofia_row("25065")])
        self.assertIn("- NH투자증권 25065 낙인30% 쿠폰20.2%", text)

    def test_노낙인_상품은_낙인없음으로_나온다(self):
        text = self._run([_kofia_row("25065", description="원금보장형, NoKI")])
        self.assertIn("낙인없음", text)

    def test_이미_있는_상품은_목록에_실리지_않는다(self):
        self._run([_kofia_row("25065")])                       # 먼저 수집
        text = self._run([_kofia_row("25065"), _kofia_row("25067")])
        self.assertIn("전체 2건 / 신규 1건", text)
        self.assertNotIn("25065", text)
        self.assertIn("- NH투자증권 25067", text)

    def test_상한을_넘으면_외_N건으로_접는다(self):
        rows = [_kofia_row(str(26000 + i)) for i in range(NEW_LIST_LIMIT + 3)]
        text = self._run(rows)
        self.assertEqual(text.count("- NH투자증권 "), NEW_LIST_LIMIT)
        self.assertIn("... 외 3건", text)

    def test_신규가_없으면_목록_없이_건수만_보낸다(self):
        self._run([_kofia_row("25065")])
        text = self._run([_kofia_row("25065")])
        self.assertIn("전체 1건 / 신규 0건", text)
        self.assertNotIn("- NH투자증권", text)
        self.assertNotIn("외 ", text)
