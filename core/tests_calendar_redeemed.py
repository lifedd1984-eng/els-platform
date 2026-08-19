"""상환 캘린더에 상환 완료 건을 남기는 규칙 테스트.

배경 (2026-08-18 조 팀장 실측)
  redemption_calendar가 status="보유중"만 가져왔다. 그래서 상환을 확정하는
  순간(status가 조기상환/만기상환/낙인후상환으로 바뀜) 그 투자가 달력에서
  통째로 사라졌다. 그날 확정한 4건(키움 1837·1839·1840, 삼성 30994)이
  같이 증발한 것이 발견의 계기다.

여기서 지키려는 것
  ① 상환이 끝난 건도 달력에 남는다 — 상태(조기상환 등)를 그대로 달고
  ② **상환된 회차까지만** 남는다. 그 이후 회차는 실제로 오지 않으므로
     그리면 거짓 정보다 (4회차 상품이 1회차에서 상환되면 2~4차는 없다)
  ③ 끝난 건은 '이 달 예상 결과' 집계에서 빠진다 — 앞으로의 예측이라
     이미 결과가 나온 건을 넣으면 투자금액·예상상환금이 부풀려진다
  ④ 보유중 건은 예전 그대로다 (전 회차 표시 + 요약 집계 포함)

상환 회차 판정 근거는 Investment.redeemed_round 한 곳이다.
운영 DB에서 RedemptionVerdict가 0건이라(2026-08-18 실측) 판정을 verdict에만
기대면 상환 회차를 영영 못 정한다. 그래서 redeemed_at을 주 근거로 쓰고
verdict는 있으면 함께 보되 **더 이른 쪽**을 택한다.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Investment, Product, RedemptionVerdict

TODAY = date.today()

# 회차별 평가일 — 오늘 기준 상대값이라 언제 돌려도 결과가 같다.
# 1차만 지났고 2~4차는 아직 오지 않았다. 서로 다른 달에 떨어진다.
R1 = TODAY - timedelta(days=40)
R2 = TODAY + timedelta(days=50)
R3 = TODAY + timedelta(days=140)
R4 = TODAY + timedelta(days=230)
EVAL_DATES = [R1, R2, R3, R4]


def _events(resp):
    """렌더된 달력 칸에 실제로 그려진 이벤트 전부."""
    return [ev for week in resp.context["weeks"]
            for cell in week for ev in cell["events"]]


class CalendarRedeemedTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("cal", password="x")
        self.client.force_login(self.user)
        self._seq = 0

    def _inv(self, status="보유중", redeemed_at=None, **kw):
        self._seq += 1
        fields = dict(
            issuer="테스트증권", product_no=str(1000 + self._seq),
            yield_rate=12.0, barriers_raw=[90, 85, 80, 75],
            asset_type="지수형", loss_prob=1.5,
            period_months=3, issue_date=R1 - timedelta(days=90),
            expiry_date=R4, eval_dates=[d.isoformat() for d in EVAL_DATES])
        fields.update(kw)
        p = Product.objects.create(**fields)
        return Investment.objects.create(
            user=self.user, product=p, amount=10_000_000,
            invested_at=R1 - timedelta(days=90),
            status=status, redeemed_at=redeemed_at)

    def _get(self, d):
        return self.client.get(reverse("calendar"), {"y": d.year, "m": d.month})

    # ── ① 상환 완료 건이 달력에 남는다 ──────────────────
    def test_상환_완료_건이_달력에서_사라지지_않는다(self):
        inv = self._inv(status="조기상환", redeemed_at=R1 + timedelta(days=2))
        evs = _events(self._get(R1))
        self.assertEqual([e["inv"].id for e in evs], [inv.id])
        self.assertEqual(evs[0]["n"], 1)

    def test_상환된_회차에_상태가_그대로_붙는다(self):
        for status in ("조기상환", "만기상환", "낙인후상환"):
            with self.subTest(status=status):
                Investment.objects.all().delete()
                self._inv(status=status, redeemed_at=R1 + timedelta(days=2))
                ev = _events(self._get(R1))[0]
                self.assertEqual(ev["done_label"], status)
                self.assertTrue(ev["done"])

    def test_화면에_상태와_회색_처리가_찍힌다(self):
        self._inv(status="조기상환", redeemed_at=R1 + timedelta(days=2))
        html = self._get(R1).content.decode()
        self.assertIn("cal-event done", html)          # 회색 칩
        self.assertIn("조기상환 </span>", html)         # 칩 앞머리 상태 문구
        self.assertIn("상환 완료 1건", html)            # 상단 요약 줄
        self.assertNotIn("지난 </span>", html)          # '지난'과 겹쳐 찍히지 않는다

    # ── ② 상환 회차 이후는 그리지 않는다 ────────────────
    def test_상환_이후_회차는_달력에_나오지_않는다(self):
        self._inv(status="조기상환", redeemed_at=R1 + timedelta(days=2))
        for d in (R2, R3, R4):
            with self.subTest(round_date=d):
                self.assertEqual(_events(self._get(d)), [])

    def test_보유중이면_이후_회차가_그대로_나온다(self):
        inv = self._inv()
        for n, d in enumerate(EVAL_DATES, start=1):
            with self.subTest(n=n):
                evs = _events(self._get(d))
                self.assertEqual([e["n"] for e in evs], [n])
                self.assertEqual(evs[0]["inv"].id, inv.id)
                self.assertFalse(evs[0]["done"])
                self.assertEqual(evs[0]["done_label"], "")

    def test_2차에서_상환되면_1차는_남고_3차부터_사라진다(self):
        # 1차는 배리어 미달로 지나간 실제 평가라 기록으로 남겨야 한다
        self._inv(status="조기상환", redeemed_at=R2 + timedelta(days=2))
        self.assertEqual([e["n"] for e in _events(self._get(R1))], [1])
        self.assertEqual([e["n"] for e in _events(self._get(R2))], [2])
        self.assertEqual(_events(self._get(R3)), [])
        self.assertEqual(_events(self._get(R4)), [])

    def test_상환된_회차에만_상태가_붙고_앞_회차는_지난_평가다(self):
        self._inv(status="조기상환", redeemed_at=R2 + timedelta(days=2))
        self.assertEqual(_events(self._get(R1))[0]["done_label"], "")
        self.assertEqual(_events(self._get(R2))[0]["done_label"], "조기상환")

    def test_만기상환은_마지막_회차까지_남는다(self):
        self._inv(status="만기상환", redeemed_at=R4 + timedelta(days=3))
        for n, d in enumerate(EVAL_DATES, start=1):
            with self.subTest(n=n):
                self.assertEqual([e["n"] for e in _events(self._get(d))], [n])
        self.assertEqual(_events(self._get(R4))[0]["done_label"], "만기상환")

    # ── ③ 월간 요약에서 뺀다 ───────────────────────────
    def test_상환_완료_건은_요약_집계에_들어가지_않는다(self):
        self._inv(status="조기상환", redeemed_at=R1 + timedelta(days=2))
        r = self._get(R1)
        self.assertEqual(len(_events(r)), 1)     # 달력 칸에는 보인다
        self.assertIsNone(r.context["summary"])  # 요약에는 없다

    def test_보유중_건만_요약에_잡힌다(self):
        held = self._inv()
        self._inv(status="조기상환", redeemed_at=R1 + timedelta(days=2))
        r = self._get(R1)
        s = r.context["summary"]
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["invested"], held.amount)  # 완료 건 1천만원이 안 더해진다
        self.assertEqual(s["by_type"]["지수형"]["count"], 1)

    def test_요약이_없어도_상세_표는_그대로_나온다(self):
        # 완료 건만 있는 달 — 예전엔 여기서 '평가 예정이 없습니다'가 떴다
        self._inv(status="조기상환", redeemed_at=R1 + timedelta(days=2))
        r = self._get(R1)
        html = r.content.decode()
        self.assertEqual(r.context["event_count"], 1)
        self.assertIn("이 달 평가 상세", html)
        self.assertNotIn("이 달에는 평가 예정이 없습니다", html)

    def test_완료와_보유중이_같은_달에_섞여도_각자_표시된다(self):
        held = self._inv()
        done = self._inv(status="조기상환", redeemed_at=R1 + timedelta(days=2))
        r = self._get(R1)
        by_id = {e["inv"].id: e for e in _events(r)}
        self.assertEqual(set(by_id), {held.id, done.id})
        self.assertEqual(by_id[done.id]["done_label"], "조기상환")
        self.assertEqual(by_id[held.id]["done_label"], "")
        self.assertEqual(r.context["done_count"], 1)
        self.assertEqual(r.context["event_count"], 2)
        self.assertEqual(r.context["summary"]["count"], 1)

    # ── ④ 상환 회차 판정 근거 ──────────────────────────
    def test_보유중이면_상환_회차가_없다(self):
        self.assertIsNone(self._inv().redeemed_round)

    def test_상환일로_회차를_정한다(self):
        # 상환일은 평가일 며칠 뒤 정산일이라 그 이전 마지막 회차가 상환 회차다
        for redeemed, expected in ((R1, 1), (R1 + timedelta(days=3), 1),
                                   (R2 + timedelta(days=2), 2),
                                   (R4 + timedelta(days=5), 4)):
            with self.subTest(redeemed=redeemed):
                inv = self._inv(status="조기상환", redeemed_at=redeemed)
                self.assertEqual(inv.redeemed_round, expected)

    def test_판정이_있으면_충족한_가장_이른_회차를_본다(self):
        # 확정이 늦으면 check_redemptions가 다음 회차 판정도 쌓는다.
        # 최신이 아니라 최소를 봐야 실제 상환 회차가 나온다.
        inv = self._inv(status="조기상환", redeemed_at=None)
        RedemptionVerdict.objects.create(
            investment=inv, round_no=2, eval_date=R2, met=True)
        RedemptionVerdict.objects.create(
            investment=inv, round_no=1, eval_date=R1, met=True)
        self.assertEqual(inv.redeemed_round, 1)

    def test_충족하지_않은_판정은_상환_회차가_아니다(self):
        inv = self._inv(status="조기상환", redeemed_at=R2 + timedelta(days=2))
        RedemptionVerdict.objects.create(
            investment=inv, round_no=1, eval_date=R1, met=False)
        self.assertEqual(inv.redeemed_round, 2)

    def test_근거가_어긋나면_더_이른_쪽을_택한다(self):
        # 확정이 늦어 상환일이 '오늘'로 들어가면 뒤 회차까지 넘어간다.
        # 오지 않을 회차를 그리느니 지난 회차를 덜 그리는 쪽이 안전하다.
        inv = self._inv(status="조기상환", redeemed_at=R4 + timedelta(days=1))
        RedemptionVerdict.objects.create(
            investment=inv, round_no=1, eval_date=R1, met=True)
        self.assertEqual(inv.redeemed_round, 1)

    def test_근거가_하나도_없으면_1차로_본다(self):
        # 운영 DB의 상환 완료 4건이 실제로 이 상태였다(verdict 0건).
        # 근거가 없을 때 가장 적게 보여주는 값이라 1차로 떨어뜨린다.
        inv = self._inv(status="조기상환", redeemed_at=None)
        self.assertEqual(inv.redeemed_round, 1)
        self.assertEqual([e["n"] for e in _events(self._get(R1))], [1])
        self.assertEqual(_events(self._get(R2)), [])

    def test_상환일이_1차_평가일보다_빨라도_1차는_남는다(self):
        # 평가일이 '추정'이면 실측과 -7~+3일 벌어져 상환일이 앞설 수 있다.
        # 여기서 0건이 되면 달력에서 사라지던 원래 사고로 되돌아간다.
        inv = self._inv(status="조기상환", redeemed_at=R1 - timedelta(days=5))
        self.assertEqual(inv.redeemed_round, 1)
        self.assertEqual([e["n"] for e in _events(self._get(R1))], [1])

    def test_스케줄을_못_만들면_상환_회차가_없다(self):
        inv = self._inv(status="조기상환", redeemed_at=R1, barriers_raw=[])
        self.assertEqual(inv.schedule, [])
        self.assertIsNone(inv.redeemed_round)

    # ── ⑤ 남의 투자는 여전히 안 보인다 ──────────────────
    def test_다른_사람의_상환_완료_건은_보이지_않는다(self):
        other = get_user_model().objects.create_user("other", password="x")
        inv = self._inv(status="조기상환", redeemed_at=R1 + timedelta(days=2))
        Investment.objects.filter(pk=inv.pk).update(user=other)
        self.assertEqual(_events(self._get(R1)), [])
