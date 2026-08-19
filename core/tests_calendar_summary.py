"""상환 캘린더 월간 요약 카드 — 실제/추정 혼합 집계 규칙.

배경 (2026-08-19 조 팀장 지시)
  ① 상환이 끝난 건도 요약에 넣는다. 하루 전(08-18)에는 '앞으로 일어날 일의
     예측'이라며 뺐지만, 끝난 건은 결과를 이미 알고 있으므로 빼는 대신
     **실제 상환금**(Investment.redeemed_amount)을 쓴다. 한 카드에 추정과
     실제가 섞이므로 화면에서 갈라 보여준다.
  ② '예상 손실율(금액가중)' 칸을 빼고 '예상 수익률'을 넣는다.
     수익률 = 세전수익 ÷ **상환금을 산출한 건들의** 투자금액 × 100.
     분자(세전수익)가 그 건들에서만 나오므로 분모도 같은 건들로 맞춘다.
     전 건이 산출되면 분모는 화면의 '대상 투자금액'과 같은 값이다.

숫자를 지어내지 않는 자리 — 다음 둘은 건수·투자금액에는 남기고
금액 집계(상환금·세전수익·수익률)에서만 뺀다:
  · 상환은 끝났는데 redeemed_amount가 등록되지 않은 건
  · 뒤 회차에서 상환된 건의 앞 회차 (상환 없이 지나간 평가 — 그 회차에서
    조기상환에 성공했을 때의 추정치를 쓰면 안 된 일을 쓰는 셈이 된다)

한 투자가 같은 달에 두 번 평가받으면 대표 1건만 집계한다(중복 방지).
대표는 가장 이른 회차이되, 상환이 일어난 회차가 그 달에 있으면 그 회차다.

달력에 무엇을 그리는지(상환 회차까지만 남긴다 등)는
core/tests_calendar_redeemed.py가 따로 본다.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Investment, Product

TODAY = date.today()


def _month(offset):
    """오늘로부터 offset개월 떨어진 달의 1일."""
    m = TODAY.month + offset
    return date(TODAY.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)


# 달 전체가 과거인 달 / 미래인 달 — 오늘이 며칠이든 결과가 같다.
MA, MB = _month(-2), _month(-1)
MC, MD = _month(2), _month(3)

# 3개월 주기·연 12% 상품의 회차별 세전 추정 상환금 (투자금 1천만원 기준)
#   n회차 = amount x (1 + 12% x 3n/12)  →  1차 10,300,000 / 2차 10,600,000
EST_R1, EST_R2 = 10_300_000, 10_600_000


class CalendarSummaryTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sum", password="x")
        self.client.force_login(self.user)
        self._seq = 0

    def _inv(self, evals, *, status="보유중", redeemed_at=None, redeemed_amount=None,
             amount=10_000_000, yield_rate=12.0, asset_type="지수형"):
        """평가일을 직접 지정한 투자 1건. 배리어 개수 = 평가일 개수 = 회차 수."""
        self._seq += 1
        p = Product.objects.create(
            issuer="테스트증권", product_no=str(2000 + self._seq),
            yield_rate=yield_rate, barriers_raw=[90] * len(evals),
            asset_type=asset_type, loss_prob=1.5, period_months=3,
            issue_date=evals[0] - timedelta(days=90), expiry_date=evals[-1],
            eval_dates=[d.isoformat() for d in evals])
        return Investment.objects.create(
            user=self.user, product=p, amount=amount,
            invested_at=evals[0] - timedelta(days=90), status=status,
            redeemed_at=redeemed_at, redeemed_amount=redeemed_amount)

    def _evals(self, first=MA):
        """1차만 first 달에 두고 나머지는 다른 달로 흩는 4회차 평가일."""
        return [first + timedelta(days=4), MB + timedelta(days=4),
                MC + timedelta(days=4), MD + timedelta(days=4)]

    def _sum(self, d):
        return self.client.get(reverse("calendar"),
                               {"y": d.year, "m": d.month}).context["summary"]

    def _html(self, d):
        return self.client.get(reverse("calendar"),
                               {"y": d.year, "m": d.month}).content.decode()

    # ── ① 완료 건이 요약에 들어간다 ─────────────────────
    def test_완료_건만_있는_달도_요약이_나온다(self):
        self._inv(self._evals(), status="조기상환",
                  redeemed_at=MA + timedelta(days=6), redeemed_amount=10_250_000)
        s = self._sum(MA)
        self.assertIsNotNone(s)
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["done_n"], 1)
        self.assertEqual(s["invested"], 10_000_000)
        self.assertEqual(s["upcoming"], 0)
        self.assertEqual(s["past"], 0)  # 완료 건은 '지남'이 아니라 '상환 완료'

    # ── ② 완료 건은 실제 상환금을 쓴다 ──────────────────
    def test_완료_건은_추정치가_아니라_실제_상환금을_쓴다(self):
        inv = self._inv(self._evals(), status="조기상환",
                        redeemed_at=MA + timedelta(days=6), redeemed_amount=10_250_000)
        # 같은 회차의 추정치는 10,300,000 — 실제와 다른 값이어야 시험이 성립한다
        self.assertEqual(inv.schedule[0]["expected"], EST_R1)
        s = self._sum(MA)
        self.assertEqual(s["real_n"], 1)
        self.assertEqual(s["real_sum"], 10_250_000)
        self.assertEqual(s["redeem_total"], 10_250_000)
        self.assertEqual(s["est_n"], 0)
        self.assertEqual(s["profit"], 250_000)

    def test_실제와_추정이_한_달에_섞이면_따로_집계된다(self):
        self._inv(self._evals())                       # 보유중 → 추정
        self._inv(self._evals(), status="조기상환",     # 완료 → 실제
                  redeemed_at=MA + timedelta(days=6), redeemed_amount=10_250_000)
        s = self._sum(MA)
        self.assertEqual(s["count"], 2)
        self.assertEqual((s["real_n"], s["real_sum"]), (1, 10_250_000))
        self.assertEqual((s["est_n"], s["est_sum"]), (1, EST_R1))
        self.assertEqual(s["redeem_total"], 10_250_000 + EST_R1)
        self.assertEqual(s["invested"], 20_000_000)
        self.assertEqual(s["profit"], 550_000)

    # ── ③ 수익률 ───────────────────────────────────────
    def test_수익률은_세전수익_나누기_투자금액이다(self):
        self._inv(self._evals())
        s = self._sum(MA)
        self.assertEqual(s["profit"], 300_000)
        self.assertEqual(s["return_rate"], 3.0)  # 300,000 / 10,000,000

    def test_수익률은_금액가중이다(self):
        # 1천만원이 3%, 3천만원이 1% → 단순평균 2%가 아니라 금액가중 1.5%
        self._inv(self._evals())                                  # +300,000
        self._inv(self._evals(), amount=30_000_000, yield_rate=4.0)  # +300,000
        s = self._sum(MA)
        self.assertEqual(s["profit"], 600_000)
        self.assertEqual(s["invested"], 40_000_000)
        self.assertEqual(s["return_rate"], 1.5)

    def test_실제와_추정이_섞인_수익률도_같은_식이다(self):
        self._inv(self._evals())
        self._inv(self._evals(), status="조기상환",
                  redeemed_at=MA + timedelta(days=6), redeemed_amount=10_250_000)
        s = self._sum(MA)
        self.assertEqual(s["return_rate"], 2.75)  # 550,000 / 20,000,000

    def test_손실_상환이면_수익률이_마이너스다(self):
        self._inv(self._evals(), status="낙인후상환",
                  redeemed_at=MA + timedelta(days=6), redeemed_amount=6_000_000)
        s = self._sum(MA)
        self.assertEqual(s["profit"], -4_000_000)
        self.assertEqual(s["return_rate"], -40.0)
        html = self._html(MA)
        self.assertIn("-4,000,000", html)
        self.assertIn("-40.0", html)

    # ── ④ 완료 건이 없는 달은 예전과 같다 ────────────────
    def test_완료_건이_없으면_예전_문구와_숫자를_그대로_쓴다(self):
        self._inv(self._evals())
        s = self._sum(MA)
        self.assertEqual(s["real_n"], 0)
        self.assertEqual(s["done_n"], 0)
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["past"], 1)
        self.assertEqual(s["redeem_total"], EST_R1)
        html = self._html(MA)
        self.assertIn(f"{MA.month}월 예상 결과", html)
        self.assertIn("(조기상환 성공 가정)", html)
        self.assertIn("예상 상환금", html)
        self.assertIn("예상 세전수익", html)
        self.assertIn("이 달에 평가일이 있는 보유 상품 기준입니다.", html)
        # 실제/추정 갈래는 완료 건이 있을 때만 뜬다 (칸 안 문구는 '실제 <b>N건</b>')
        self.assertNotIn("실제 <b>", html)
        self.assertNotIn("상환 완료 <b>", html)

    def test_예정만_있는_달은_예정으로_잡힌다(self):
        self._inv(self._evals(first=MC))
        s = self._sum(MC)
        self.assertEqual((s["upcoming"], s["past"], s["done_n"]), (1, 0, 0))

    # ── ⑤ 숫자를 지어내지 않는 자리 ─────────────────────
    def test_실제_상환금이_없는_완료_건은_금액_집계에서_뺀다(self):
        # 건수·투자금액에는 남는다 — 평가는 실제로 있었기 때문이다.
        # 추정치로 메우지 않는다: 상환이 끝난 건에 '성공했다면' 값을 넣는 것은
        # 이미 나온 결과를 지어내는 셈이다.
        self._inv(self._evals(), status="조기상환",
                  redeemed_at=MA + timedelta(days=6), redeemed_amount=None)
        s = self._sum(MA)
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["done_n"], 1)
        self.assertEqual(s["invested"], 10_000_000)
        self.assertEqual((s["real_n"], s["est_n"], s["amount_n"]), (0, 0, 0))
        self.assertEqual(s["no_amount_n"], 1)
        self.assertEqual(s["redeem_total"], 0)
        self.assertIsNone(s["return_rate"])
        self.assertIn("상환금을 알 수 없는 1건", self._html(MA))

    def test_상환_없이_지나간_앞_회차는_금액_집계에서_뺀다(self):
        # 2차에서 상환된 건의 1차 — 그 회차에서는 상환되지 않았다.
        self._inv(self._evals(), status="조기상환",
                  redeemed_at=MB + timedelta(days=6), redeemed_amount=EST_R2)
        a = self._sum(MA)   # 1차가 있는 달 — 상환은 없었다
        self.assertEqual(a["count"], 1)
        self.assertEqual((a["done_n"], a["past"]), (0, 1))
        self.assertEqual((a["real_n"], a["est_n"]), (0, 0))
        self.assertEqual(a["no_amount_n"], 1)
        self.assertEqual(a["redeem_total"], 0)
        self.assertIsNone(a["return_rate"])
        b = self._sum(MB)   # 2차가 있는 달 — 여기서 실제 상환금이 잡힌다
        self.assertEqual((b["done_n"], b["real_n"]), (1, 1))
        self.assertEqual(b["real_sum"], EST_R2)
        self.assertEqual(b["return_rate"], 6.0)

    # ── ⑥ 한 달에 두 회차가 겹칠 때의 대표 ───────────────
    def test_같은_달에_두_회차가_있으면_상환_회차가_대표다(self):
        evals = [MA + timedelta(days=4), MA + timedelta(days=24),
                 MC + timedelta(days=4), MD + timedelta(days=4)]
        self._inv(evals, status="조기상환",
                  redeemed_at=MA + timedelta(days=26), redeemed_amount=EST_R2)
        r = self.client.get(reverse("calendar"), {"y": MA.year, "m": MA.month})
        self.assertEqual(r.context["event_count"], 2)   # 달력에는 두 회차 다 그린다
        s = r.context["summary"]
        self.assertEqual(s["count"], 1)                 # 요약은 대표 1건
        self.assertEqual(s["invested"], 10_000_000)     # 투자금액이 두 번 안 더해진다
        self.assertEqual((s["done_n"], s["real_n"]), (1, 1))
        self.assertEqual(s["real_sum"], EST_R2)
        self.assertEqual(s["no_amount_n"], 0)

    def test_보유중이_같은_달에_두_번_평가받으면_이른_회차가_대표다(self):
        evals = [MA + timedelta(days=4), MA + timedelta(days=24),
                 MC + timedelta(days=4), MD + timedelta(days=4)]
        self._inv(evals)
        s = self._sum(MA)
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["est_sum"], EST_R1)          # 2차 추정치가 아니다
        self.assertEqual(s["return_rate"], 3.0)

    # ── ⑦ 화면 표기 ────────────────────────────────────
    def test_화면이_실제와_추정을_갈라_보여준다(self):
        self._inv(self._evals())
        self._inv(self._evals(), status="조기상환",
                  redeemed_at=MA + timedelta(days=6), redeemed_amount=10_250_000)
        html = self._html(MA)
        self.assertIn(f"{MA.month}월 결과", html)
        self.assertNotIn(f"{MA.month}월 예상 결과", html)
        self.assertIn("(실제 1건 + 추정 1건)", html)
        self.assertIn("상환 완료 <b>1건</b>", html)
        self.assertIn("실제 <b>1건</b> 10,250,000원", html)
        self.assertIn("추정 <b>1건</b> 10,300,000원", html)
        self.assertIn("상환이 끝난 1건은 <b>실제 상환금</b>", html)
        # 완료 건이 섞이면 '예상'을 뗀다 — 절반이 실제 값이기 때문이다
        self.assertNotIn("예상 상환금", html)
        self.assertNotIn("예상 세전수익", html)

    def test_예상_손실율_칸이_사라지고_수익률이_들어간다(self):
        self._inv(self._evals())
        html = self._html(MA)
        self.assertNotIn("예상 손실율", html)
        self.assertNotIn("금액가중", html)
        self.assertNotIn("손실확률 산출 상품이 투자금의", html)
        self.assertIn("예상 수익률", html)
        self.assertIn("(투자금 대비)", html)
        self.assertIn("+3.0", html)

    def test_다른_사람의_완료_건은_요약에_안_잡힌다(self):
        other = get_user_model().objects.create_user("other2", password="x")
        inv = self._inv(self._evals(), status="조기상환",
                        redeemed_at=MA + timedelta(days=6), redeemed_amount=10_250_000)
        Investment.objects.filter(pk=inv.pk).update(user=other)
        self.assertIsNone(self._sum(MA))
