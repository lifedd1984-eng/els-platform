"""AI 분석 질문(/ask/) 백엔드 테스트.

여기서 지키려는 것
  · 한도와 차감 규칙 — 돈이 걸린 부분이라 규칙이 흐려지면 바로 새어 나간다.
  · 사후검사 — 권유·평가어·미래단정·포트폴리오 행동제안이 문장으로 새는지.
  · 숫자 접지 — 모델이 도구에 없는 수치를 지어내면 잡히는지.
  · 도구가 커버리지 밖 요청에 빈 결과 대신 사유 코드를 주는지.
  · 프롬프트 프리픽스가 요청마다 바뀌지 않는지 (바뀌면 캐시가 통째로 죽는다).

모델 호출은 전부 가짜로 바꾼다 — 테스트가 API를 때리면 안 된다.
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core import ask_agent, ask_blocks, ask_tools, portfolio_facts
from core.models import AskLog, Investment, KnockInStatus, PriceBar, Product

User = get_user_model()


def _tool_use(name, inp, tid="t1"):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _resp(blocks, **usage):
    u = {"input_tokens": 100, "output_tokens": 50,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    u.update(usage)
    return {"content": blocks, "usage": u}


def _seed_mu(n, start=date(2024, 1, 1), ticker="MU"):
    """시세 n일치를 한 번에 넣는다.

    한 건씩 create하면 테스트마다 수십 번 INSERT가 돌아 모듈 전체가
    3분을 넘겼다. setUpTestData + bulk_create로 클래스당 1회로 줄인다.
    """
    PriceBar.objects.bulk_create([
        PriceBar(ticker=ticker, date=d, close=100 + i, adj_close=100 + i)
        for i, d in enumerate(_bdays(start, n))
    ])


class FakeAPI:
    """_call 대역. 턴1/턴2 응답을 순서대로 돌려준다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, model, system, tools, messages, max_tokens, tool_choice=None):
        self.calls.append({"model": model, "system": system, "tools": tools,
                           "messages": messages, "max_tokens": max_tokens})
        return self.responses.pop(0) if self.responses else _resp([])


# ══════════════════════════════════════════════════════════════════
# 도구 — 사유 코드
# ══════════════════════════════════════════════════════════════════

class ToolReasonCodeTests(TestCase):
    """커버리지 밖 요청은 빈 결과가 아니라 사유 코드로 돌아와야 한다.

    빈 배열을 주면 모델이 '없다'를 자기 말로 지어낸다 — 그게 정확히
    막으려는 상황이다.
    """

    @classmethod
    def setUpTestData(cls):
        _seed_mu(40)

    def setUp(self):
        ask_tools._COV.update(day=None, data=None)

    def test_보유구간_이전을_물으면_OUT_OF_RANGE(self):
        r = ask_tools.run("metric_calc", None,
                          {"asset": "Micron", "metrics": ["cumulative_return"],
                           "start": "2010-01-01", "end": "2012-01-01"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], ask_tools.OUT_OF_RANGE)
        self.assertIn("2024-01-01", r["have_from"])

    def test_모르는_자산은_NO_ASSET(self):
        r = ask_tools.run("metric_calc", None,
                          {"asset": "비트코인", "metrics": ["cumulative_return"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], ask_tools.NO_ASSET)

    def test_보유구간을_걸치면_PARTIAL_RANGE로_알린다(self):
        r = ask_tools.run("metric_calc", None,
                          {"asset": "Micron", "metrics": ["cumulative_return"],
                           "start": "2020-01-01", "end": "2030-01-01"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["reason"], ask_tools.PARTIAL_RANGE)

    def test_알_수_없는_도구는_OUT_OF_SCOPE(self):
        r = ask_tools.run("없는도구", None, {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], ask_tools.OUT_OF_SCOPE)

    def test_도구가_예외를_던져도_사유코드로_돌아온다(self):
        # RUNNERS는 import 시점의 함수 참조를 들고 있으므로 딕셔너리 쪽을 갈아끼운다
        def boom(*a, **k):
            raise ValueError("boom")
        with mock.patch.dict(ask_tools.RUNNERS, {"metric_calc": boom}):
            r = ask_tools.run("metric_calc", None, {"asset": "Micron", "metrics": []})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "TOOL_ERROR")


class ToolDisplayTests(TestCase):
    """도구는 원본값과 표시용 문자열을 함께 준다 (사후검사가 이걸 대조한다)."""

    @classmethod
    def setUpTestData(cls):
        _seed_mu(60)

    def setUp(self):
        ask_tools._COV.update(day=None, data=None)

    def test_지표는_value와_display를_함께_준다(self):
        r = ask_tools.run("metric_calc", None,
                          {"asset": "Micron", "metrics": ["cumulative_return", "mdd"]})
        cum = r["metrics"]["cumulative_return"]
        self.assertIn("value", cum)
        self.assertTrue(cum["display"].endswith("%"))
        self.assertTrue(cum["display"].startswith("+"))

    def test_displays_가_표시문자열을_전부_긁어온다(self):
        r = ask_tools.run("metric_calc", None,
                          {"asset": "Micron", "metrics": ["cumulative_return"]})
        d = ask_tools.displays(r)
        self.assertIn(r["metrics"]["cumulative_return"]["display"], d)

    def test_통화는_티커에_따라_다르게_표시된다(self):
        self.assertEqual(ask_tools.price("MU", 12.5)["display"], "$12.50")
        self.assertEqual(ask_tools.price("005930.KS", 81200)["display"], "81,200원")
        self.assertEqual(ask_tools.price("^KS200", 1033.775)["display"], "1,033.78")


class CoveragePrefixTests(TestCase):
    """프리픽스가 요청마다 바뀌면 프롬프트 캐시가 통째로 죽는다."""

    def setUp(self):
        PriceBar.objects.create(ticker="MU", date=date(2024, 1, 2),
                                close=100, adj_close=100)
        ask_tools._COV.update(day=None, data=None)

    def test_커버리지_티커수가_거래일수만큼_부풀지_않는다(self):
        # PriceBar.Meta.ordering 때문에 .distinct()가 (ticker,date)로 새는 사고를 막는다
        for i, d in enumerate(_bdays(date(2024, 2, 1), 30)):
            PriceBar.objects.create(ticker="MU", date=d, close=1, adj_close=1)
        ask_tools._COV.update(day=None, data=None)
        self.assertEqual(ask_tools.coverage()["ticker_n"], 1)

    def test_같은_날_두_번_불러도_같은_문자열(self):
        a = ask_agent.system_interpret()
        b = ask_agent.system_interpret()
        self.assertEqual(a, b)

    def test_프리픽스에_시_분이_들어가지_않는다(self):
        import re
        for s in (ask_agent.system_interpret(), ask_agent.system_answer()):
            self.assertIsNone(re.search(r"\d{1,2}:\d{2}", s),
                              "프리픽스에 시:분이 들어가면 캐시가 매 요청 깨진다")

    def test_설명턴_프리픽스에는_데이터_도구_스키마가_없다(self):
        names = {t["name"] for t in [ask_agent.ANSWER_TOOL, ask_agent.REFUSE_TOOL]}
        self.assertEqual(names, {"answer", "refuse"})
        self.assertNotIn("metric_calc", ask_agent.system_answer())


# ══════════════════════════════════════════════════════════════════
# 사후검사
# ══════════════════════════════════════════════════════════════════

class GuardTests(TestCase):
    RESULTS = [{"ok": True, "metrics": {
        "cumulative_return": {"value": 6208.3, "display": "+6,208.3%"},
        "mdd": {"value": -57.6, "display": "-57.6%"}}}]

    def test_도구값만_쓴_문장은_통과한다(self):
        flags, bad = ask_agent.check_answer(
            "누적수익률은 +6,208.3%였고 최대낙폭은 -57.6%였습니다.", self.RESULTS, False)
        self.assertEqual(flags, [])
        self.assertEqual(bad, [])

    def test_없는_숫자를_지어내면_잡힌다(self):
        flags, bad = ask_agent.check_answer(
            "누적수익률은 +9,999.9%였습니다.", self.RESULTS, False)
        self.assertIn("NUMERIC_UNGROUNDED", flags)
        self.assertTrue(any("9,999.9" in b for b in bad))

    def test_권유_표현은_1군에_걸린다(self):
        for text in ["지금 매수하시는 것을 추천합니다.",
                     "이 상품을 사세요.",
                     "지금 담으시는 게 좋습니다."]:
            with self.subTest(text=text):
                flags, _ = ask_agent.check_answer(text, self.RESULTS, False)
                self.assertIn("ADVICE", flags)

    def test_미래_단정은_2군에_걸린다(self):
        for text in ["앞으로 오를 것입니다.",
                     "연말까지 회복할 가능성이 높습니다.",
                     "추가 하락이 예상됩니다."]:
            with self.subTest(text=text):
                flags, _ = ask_agent.check_answer(text, self.RESULTS, False)
                self.assertIn("FUTURE", flags)

    def test_평가어는_3군에_걸린다(self):
        for text in ["이 상품은 안전합니다.", "지금 수준은 위험합니다.",
                     "조건이 유리합니다.", "장기 투자에 적합합니다."]:
            with self.subTest(text=text):
                flags, _ = ask_agent.check_answer(text, self.RESULTS, False)
                self.assertIn("EVAL", flags)

    def test_포트폴리오_행동제안은_4군에_걸린다(self):
        for text in ["일부를 정리하시는 편이 낫습니다.",
                     "Micron 비중을 줄이세요.",
                     "다른 상품으로 갈아타는 것을 고려하세요.",
                     "추가 매수로 평단을 낮추십시오."]:
            with self.subTest(text=text):
                flags, _ = ask_agent.check_answer(text, self.RESULTS, True)
                self.assertIn("PORTFOLIO_ACTION", flags)

    def test_4군은_포트폴리오_질문에서만_적용된다(self):
        text = "발행 건수를 정리하면 다음과 같습니다."
        self.assertNotIn("PORTFOLIO_ACTION",
                         ask_agent.check_answer(text, self.RESULTS, False)[0])

    def test_연도와_날짜는_숫자검사에_걸리지_않는다(self):
        res = [{"ok": True, "by_bucket": [{"bucket": "2018",
                                           "ret": {"value": -27.3, "display": "-27.3%"}}],
                "meta": {"first": "2016-08-05"}}]
        flags, bad = ask_agent.check_answer(
            "2016-08-05부터 봤을 때 2018년은 -27.3%였습니다.", res, False)
        self.assertEqual(bad, [])
        self.assertEqual(flags, [])


# ══════════════════════════════════════════════════════════════════
# 한도 · 차감 규칙
# ══════════════════════════════════════════════════════════════════

@override_settings(ANTHROPIC_API_KEY="test-key",
                   ASK_DAILY_LIMITS={"default": 3, "staff": 3, "superuser": None})
class QuotaTests(TestCase):

    def setUp(self):
        self.member = User.objects.create_user("member", password="x")
        self.family = User.objects.create_user("family", password="x", is_staff=True)
        self.admin = User.objects.create_superuser("boss", password="x")

    def _ok_api(self):
        return FakeAPI(
            _resp([_tool_use("product_stats", {"metric": "issue_count"})]),
            _resp([_tool_use("answer", {"text": "발행 이력을 집계했습니다.",
                                        "numbers_used": []}, "t2")]),
        )

    def _ask(self, user, q):
        self.client.force_login(user)
        return self.client.post("/ask/", {"q": q})

    def test_로그인하지_않으면_로그인_화면으로(self):
        r = self.client.get("/ask/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

    def test_일반회원은_4회째에_차단된다(self):
        for i in range(3):
            with mock.patch.object(ask_agent, "_call", self._ok_api()):
                r = self._ask(self.member, f"질문 {i}")
            self.assertIsNone(r.context["error"], f"{i + 1}번째가 막히면 안 된다")
        with mock.patch.object(ask_agent, "_call", self._ok_api()) as api:
            r = self._ask(self.member, "질문 4")
        self.assertEqual(r.context["error"]["code"], "QUOTA_EXCEEDED")
        self.assertEqual(api.calls, [], "차단됐으면 모델을 부르면 안 된다")
        self.assertEqual(
            AskLog.objects.filter(user=self.member, billed=True).count(), 3)

    def test_가족계정도_일반과_같은_한도를_쓴다(self):
        for i in range(3):
            with mock.patch.object(ask_agent, "_call", self._ok_api()):
                self._ask(self.family, f"질문 {i}")
        with mock.patch.object(ask_agent, "_call", self._ok_api()):
            r = self._ask(self.family, "질문 4")
        self.assertEqual(r.context["error"]["code"], "QUOTA_EXCEEDED")

    def test_운영자는_10회째도_통과하고_기록은_남는다(self):
        for i in range(10):
            with mock.patch.object(ask_agent, "_call", self._ok_api()):
                r = self._ask(self.admin, f"질문 {i}")
            self.assertIsNone(r.context["error"], f"{i + 1}번째가 막히면 안 된다")
        self.assertEqual(AskLog.objects.filter(user=self.admin).count(), 10)
        # 무제한이라도 기록은 남긴다 — 비용 추적이 끊기면 안 된다
        self.assertEqual(AskLog.objects.filter(user=self.admin, billed=True).count(), 0)
        self.assertTrue(all(log.cost_usd > 0 for log in
                            AskLog.objects.filter(user=self.admin)))
        self.assertTrue(r.context["quota"]["unlimited"])
        self.assertEqual(r.context["quota"]["exempt_reason"], "superuser")

    def test_같은_질문_재입력은_캐시로_답하고_차감하지_않는다(self):
        with mock.patch.object(ask_agent, "_call", self._ok_api()):
            self._ask(self.member, "연도별 실현수익률")
        with mock.patch.object(ask_agent, "_call", self._ok_api()) as api:
            r = self._ask(self.member, "연도별   실현수익률")   # 공백만 다르게
        self.assertTrue(r.context["from_cache"])
        self.assertEqual(api.calls, [], "캐시 히트면 모델을 부르면 안 된다")
        self.assertEqual(AskLog.objects.filter(user=self.member).count(), 1)
        self.assertEqual(r.context["quota"]["used"], 1)

    def test_사전_거절은_차감하지_않는다(self):
        api = FakeAPI(_resp([_tool_use("refuse", {"code": "OPINION_REQUESTED",
                                                  "detail": "의견은 드릴 수 없습니다."})]))
        with mock.patch.object(ask_agent, "_call", api):
            r = self._ask(self.member, "이 상품 사도 될까?")
        log = AskLog.objects.get(user=self.member)
        self.assertEqual(log.status, AskLog.STATUS_REFUSED)
        self.assertFalse(log.billed)
        self.assertEqual(r.context["quota"]["used"], 0)
        self.assertEqual(r.context["error"]["code"], "OPINION_REQUESTED")

    def test_사후검사_실패는_차감한다(self):
        api = FakeAPI(
            _resp([_tool_use("product_stats", {"metric": "issue_count"})]),
            _resp([_tool_use("answer", {"text": "지금 매수하시는 것을 추천합니다.",
                                        "numbers_used": []}, "t2")]),
        )
        with mock.patch.object(ask_agent, "_call", api):
            r = self._ask(self.member, "발행 통계 알려줘")
        log = AskLog.objects.get(user=self.member)
        self.assertEqual(log.status, AskLog.STATUS_GUARDED)
        self.assertTrue(log.billed, "비용은 실제로 발생했으니 차감한다")
        self.assertIn("ADVICE", log.guard_flags)
        self.assertEqual(r.context["answer"], ask_agent.GUARD_REPLACEMENT)
        self.assertNotIn("추천", r.context["answer"])

    def test_API_오류는_차감하지_않는다(self):
        def boom(*a, **k):
            raise RuntimeError("network down")
        with mock.patch.object(ask_agent, "_call", boom):
            r = self._ask(self.member, "아무 질문")
        log = AskLog.objects.get(user=self.member)
        self.assertEqual(log.status, AskLog.STATUS_ERROR)
        self.assertFalse(log.billed)
        self.assertEqual(r.context["error"]["code"], "API_ERROR")

    def test_한도_소진_기록은_히스토리에_섞이지_않는다(self):
        for i in range(3):
            with mock.patch.object(ask_agent, "_call", self._ok_api()):
                self._ask(self.member, f"질문 {i}")
        with mock.patch.object(ask_agent, "_call", self._ok_api()):
            r = self._ask(self.member, "질문 4")
        self.assertEqual(len(r.context["history"]), 3)

    def test_저장된_답을_다시_열어도_차감되지_않는다(self):
        with mock.patch.object(ask_agent, "_call", self._ok_api()):
            r = self._ask(self.member, "발행 통계")
        log_id = AskLog.objects.get(user=self.member).id
        r2 = self.client.get(f"/ask/?log={log_id}")
        self.assertTrue(r2.context["from_cache"])
        self.assertEqual(r2.context["quota"]["used"], 1)

    def test_남의_기록은_열리지_않는다(self):
        with mock.patch.object(ask_agent, "_call", self._ok_api()):
            self._ask(self.member, "발행 통계")
        log_id = AskLog.objects.get(user=self.member).id
        self.client.force_login(self.family)
        r = self.client.get(f"/ask/?log={log_id}")
        self.assertIsNone(r.context["question"])


@override_settings(ANTHROPIC_API_KEY="test-key")
class GuardedAnswerKeepsFactsTest(TestCase):
    """사후검사에 걸려도 표·근거는 그대로 남아야 한다. 숫자는 사실이다."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("u1", password="x")
        _seed_mu(60)

    def setUp(self):
        ask_tools._COV.update(day=None, data=None)

    def test_문장만_바뀌고_블록과_근거는_유지된다(self):
        api = FakeAPI(
            _resp([_tool_use("metric_calc", {"asset": "Micron",
                                             "metrics": ["cumulative_return", "mdd"]})]),
            _resp([_tool_use("answer", {"text": "이 자산은 안전합니다.",
                                        "numbers_used": []}, "t2")]),
        )
        self.client.force_login(self.user)
        with mock.patch.object(ask_agent, "_call", api):
            r = self.client.post("/ask/", {"q": "Micron 수익률"})
        self.assertEqual(r.context["status"], "guarded")
        self.assertEqual(r.context["answer"], ask_agent.GUARD_REPLACEMENT)
        self.assertTrue(r.context["blocks"], "표·차트는 지우지 않는다")
        self.assertTrue(r.context["basis"], "계산 근거는 지우지 않는다")
        self.assertTrue(any(b["type"] == "stats" for b in r.context["blocks"]))


# ══════════════════════════════════════════════════════════════════
# 포트폴리오 — 화면과 도구가 같은 숫자를 낸다
# ══════════════════════════════════════════════════════════════════

class PortfolioSharedCalcTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("holder", password="x")
        self.invs = []
        for i, (ki, amt) in enumerate([(40, 10_000_000), (35, 5_000_000),
                                       (45, 3_000_000)]):
            p = Product.objects.create(
                issuer="테스트증권", product_no=f"P{i}", yield_rate=8.0,
                ki=ki, barriers_raw=[90, 85, 80], period_months=6,
                first_eval_months=6, asset_type="종목형",
                assets_raw="삼성전자 , SK하이닉스",
                issue_date=date.today() - timedelta(days=90),
                sub_end=date.today() - timedelta(days=95))
            inv = Investment.objects.create(user=self.user, product=p, amount=amt,
                                            invested_at=date.today() - timedelta(days=90))
            self.invs.append(inv)
            KnockInStatus.objects.create(investment=inv, asset_name="삼성전자",
                                         ticker="005930.KS", ref_price=80_000,
                                         current_price=64_000, level_pct=80.0)

    def test_도구_집중도가_화면_계산과_같다(self):
        holding = list(Investment.objects.filter(user=self.user, status="보유중")
                       .select_related("product"))
        total = sum(i.amount for i in holding)
        screen = portfolio_facts.analyze_risk(holding, total)
        tool = ask_tools.run("portfolio_facts", self.user,
                             {"views": ["concentration_asset"], "limit": 10})
        by_name = {r["name"]: r for r in tool["concentration_asset"]}
        for row in screen["assets"]:
            self.assertEqual(by_name[row["name"]]["pct"]["value"], row["pct"])
            self.assertEqual(by_name[row["name"]]["amount"]["value"], row["amount"])

    def test_도구_스트레스가_화면_계산과_같다(self):
        holding = list(Investment.objects.filter(user=self.user, status="보유중")
                       .select_related("product"))
        total = sum(i.amount for i in holding)
        screen = portfolio_facts.stress_test(holding, total)
        tool = ask_tools.run("portfolio_facts", self.user, {"views": ["stress_test"]})
        self.assertIsNotNone(screen)
        st = tool["stress_test"]
        for d in screen["shocks"]:
            self.assertEqual(st["total"][str(d)]["value"], screen["total"][d])
        self.assertEqual(len(st["rows"]), len(screen["rows"]))

    def test_보유가_없으면_NO_PORTFOLIO(self):
        other = User.objects.create_user("empty", password="x")
        r = ask_tools.run("portfolio_facts", other, {"views": ["summary"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "NO_PORTFOLIO")

    def test_비로그인은_NEEDS_LOGIN(self):
        from django.contrib.auth.models import AnonymousUser
        r = ask_tools.run("portfolio_facts", AnonymousUser(), {"views": ["summary"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "NEEDS_LOGIN")

    def test_금액_표시가_억만원_단위로_나온다(self):
        self.assertEqual(portfolio_facts.won(2_017_740_000), "20억 1,774만원")
        self.assertEqual(portfolio_facts.won(10_000_000), "1,000만원")
        self.assertEqual(portfolio_facts.won(300_000_000), "3억원")
        self.assertEqual(portfolio_facts.won(5_000), "5,000원")


class ProductFilterReuseTests(TestCase):
    """조건검색은 검색 화면과 같은 코드를 타야 한다 — ELB·DLB가 새면 안 된다."""

    def setUp(self):
        base = dict(yield_rate=7.0, ki=40, asset_type="지수형",
                    assets_raw="KOSPI200", sub_end=date.today() + timedelta(days=5))
        Product.objects.create(issuer="A증권", product_no="1", product_type="ELS", **base)
        Product.objects.create(issuer="B증권", product_no="2", product_type="ELB", **base)
        Product.objects.create(issuer="C증권", product_no="3", product_type="DLB", **base)
        Product.objects.create(issuer="D증권", product_no="4", product_type="DLS", **base)

    def test_ELB_DLB는_결과에서_빠진다(self):
        r = ask_tools.run("product_filter", None, {"ki_max": 50, "limit": 50})
        issuers = {p["issuer"] for p in r["products"]}
        self.assertEqual(issuers, {"A증권", "D증권"})
        self.assertIn("ELB", r["excluded"])

    def test_의견_요구는_OUT_OF_SCOPE(self):
        r = ask_tools.run("product_filter", None, {"unanswerable": True})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], ask_tools.OUT_OF_SCOPE)

    def test_결과가_없으면_NO_DATA(self):
        r = ask_tools.run("product_filter", None, {"yield_min": 999})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], ask_tools.NO_DATA)


# ══════════════════════════════════════════════════════════════════
# 블록 · 비용
# ══════════════════════════════════════════════════════════════════

class BlockBuildTests(TestCase):

    def setUp(self):
        for i, d in enumerate(_bdays(date(2024, 1, 1), 300)):
            v = 100 + i - (40 if 100 < i < 160 else 0)
            PriceBar.objects.create(ticker="MU", date=d, close=v, adj_close=v)
        ask_tools._COV.update(day=None, data=None)

    def test_차트는_좌표를_계산해_내려준다(self):
        calls = [{"name": "price_series", "input": {"asset": "Micron", "freq": "monthly"}}]
        res = [ask_tools.run("price_series", None, calls[0]["input"])]
        blocks, _ = ask_blocks.build(calls, res)
        chart = next(b for b in blocks if b["type"] == "chart")
        self.assertTrue(chart["points"])
        self.assertTrue(chart["y_ticks"])
        self.assertTrue(chart["x_ticks"])
        for x, y in (p.split(",") for p in chart["points"].split()):
            self.assertLessEqual(46.0, float(x))
            self.assertLessEqual(float(x), 706.0)
            self.assertLessEqual(20.0, float(y))
            self.assertLessEqual(float(y), 212.0)

    def test_실패한_도구는_사유_주석으로만_남는다(self):
        calls = [{"name": "metric_calc", "input": {"asset": "없는것", "metrics": ["mdd"]}}]
        res = [ask_tools.run("metric_calc", None, calls[0]["input"])]
        blocks, basis = ask_blocks.build(calls, res)
        self.assertEqual([b["type"] for b in blocks], ["note"])
        self.assertIsNone(basis)

    def test_근거에_계열과_보완여부가_들어간다(self):
        calls = [{"name": "metric_calc",
                  "input": {"asset": "Micron", "metrics": ["cumulative_return", "mdd"]}}]
        res = [ask_tools.run("metric_calc", None, calls[0]["input"])]
        _, basis = ask_blocks.build(calls, res)
        labels = {r["label"] for r in basis["rows"]}
        self.assertLessEqual({"대상", "기간", "표본", "가격 계열", "보완 구간",
                              "계산식", "실행 도구"}, labels)


class CostTests(TestCase):

    def test_캐시_읽기가_정가보다_싸다(self):
        full = ask_agent.cost_usd("claude-sonnet-5",
                                  {"input_tokens": 10_000, "output_tokens": 0},
                                  today=date(2026, 9, 1))
        cached = ask_agent.cost_usd("claude-sonnet-5",
                                    {"cache_read_input_tokens": 10_000, "output_tokens": 0},
                                    today=date(2026, 9, 1))
        self.assertAlmostEqual(cached, full * 0.10, places=8)

    def test_캐시_쓰기는_정가의_1_25배(self):
        full = ask_agent.cost_usd("claude-haiku-4-5", {"input_tokens": 10_000})
        write = ask_agent.cost_usd("claude-haiku-4-5",
                                   {"cache_creation_input_tokens": 10_000})
        self.assertAlmostEqual(write, full * 1.25, places=8)

    def test_도입가는_기한이_지나면_정가로_돌아간다(self):
        intro = ask_agent.cost_usd("claude-sonnet-5", {"input_tokens": 1_000_000},
                                   today=date(2026, 8, 6))
        std = ask_agent.cost_usd("claude-sonnet-5", {"input_tokens": 1_000_000},
                                 today=date(2026, 9, 1))
        self.assertAlmostEqual(intro, 2.00)
        self.assertAlmostEqual(std, 3.00)


@override_settings(ANTHROPIC_API_KEY="test-key",
                   ASK_DAILY_LIMITS={"default": 3, "staff": 3, "superuser": None})
class SafetyValveTests(TestCase):
    """킬스위치·전체 상한·캐시 키 — 돈과 사고를 막는 밸브들."""

    def setUp(self):
        self.u = User.objects.create_user("v1", password="x")
        self.client.force_login(self.u)
        PriceBar.objects.create(ticker="MU", date=date(2026, 8, 6),
                                close=100, adj_close=100)
        ask_tools._COV.update(day=None, data=None)

    def _api(self):
        return FakeAPI(
            _resp([_tool_use("product_stats", {"metric": "issue_count"})]),
            _resp([_tool_use("answer", {"text": "집계했습니다.", "numbers_used": []}, "t2")]),
        )

    @override_settings(ASK_ENABLED=False)
    def test_킬스위치가_꺼져_있으면_모델을_안_부른다(self):
        with mock.patch.object(ask_agent, "_call", self._api()) as api:
            r = self.client.post("/ask/", {"q": "발행 통계"})
        self.assertEqual(r.context["error"]["code"], "DISABLED")
        self.assertEqual(api.calls, [])
        self.assertFalse(AskLog.objects.filter(billed=True).exists())

    @override_settings(ASK_GLOBAL_DAILY_CAP=2)
    def test_전체_상한을_넘으면_다른_사람도_막힌다(self):
        other = User.objects.create_user("v2", password="x")
        for i in range(2):
            with mock.patch.object(ask_agent, "_call", self._api()):
                self.client.post("/ask/", {"q": f"질문 {i}"})
        self.client.force_login(other)
        with mock.patch.object(ask_agent, "_call", self._api()) as api:
            r = self.client.post("/ask/", {"q": "다른 사람 질문"})
        self.assertEqual(r.context["error"]["code"], "GLOBAL_CAP")
        self.assertEqual(api.calls, [], "전체 상한을 넘으면 모델을 부르면 안 된다")

    def test_시세가_갱신되면_당일_캐시가_갈린다(self):
        """09:30 배치 전 답이 배치 뒤에 재사용되면 화면과 답이 어긋난다."""
        before = ask_agent.question_key("같은 질문")
        PriceBar.objects.create(ticker="MU", date=date(2026, 8, 7),
                                close=110, adj_close=110)
        ask_tools._COV.update(day=None, data=None)
        after = ask_agent.question_key("같은 질문")
        self.assertNotEqual(before, after)

    def test_포트폴리오_질문은_당일_캐시를_타지_않는다(self):
        api1 = FakeAPI(
            _resp([_tool_use("portfolio_facts", {"views": ["summary"]})]),
            _resp([_tool_use("answer", {"text": "보유 현황입니다.",
                                        "numbers_used": []}, "t2")]))
        with mock.patch.object(ask_agent, "_call", api1):
            self.client.post("/ask/", {"q": "내 보유 현황"})
        api2 = FakeAPI(
            _resp([_tool_use("portfolio_facts", {"views": ["summary"]})]),
            _resp([_tool_use("answer", {"text": "보유 현황입니다.",
                                        "numbers_used": []}, "t2")]))
        with mock.patch.object(ask_agent, "_call", api2) as api:
            r = self.client.post("/ask/", {"q": "내 보유 현황"})
        self.assertFalse(r.context["from_cache"], "보유·시세가 하루 안에도 움직인다")
        self.assertTrue(api.calls, "포트폴리오는 다시 계산해야 한다")

    def test_질문은_300자에서_잘린다(self):
        long_q = "가" * 500
        api = self._api()
        with mock.patch.object(ask_agent, "_call", api):
            ask_agent.run(self.u, long_q)
        sent = api.calls[0]["messages"][0]["content"]
        self.assertEqual(len(sent), 300)


class ProductFilterNoProseTests(TestCase):
    """조건검색 결과 위에는 LLM 자유 서술을 얹지 않는다 — 구조로 막는다."""

    def setUp(self):
        self.u = User.objects.create_user("pf", password="x")
        Product.objects.create(issuer="A증권", product_no="1", product_type="ELS",
                               yield_rate=7.0, ki=40, asset_type="지수형",
                               assets_raw="KOSPI200",
                               sub_end=date.today() + timedelta(days=5))

    def test_설명턴을_아예_돌리지_않는다(self):
        api = FakeAPI(_resp([_tool_use("product_filter", {"ki_max": 50})]))
        with mock.patch.object(ask_agent, "_call", api) as spy:
            out = ask_agent.run(self.u, "낙인 50 이하 상품")
        self.assertEqual(len(spy.calls), 1, "해석턴 하나로 끝나야 한다")
        self.assertEqual(out["status"], "ok")
        self.assertIn("조건으로", out["answer"])
        self.assertTrue(any(b["type"] == "table" for b in out["blocks"]))

    def test_정형_문구라_권유가_섞일_수_없다(self):
        api = FakeAPI(_resp([_tool_use("product_filter", {"ki_max": 50})]))
        with mock.patch.object(ask_agent, "_call", api):
            out = ask_agent.run(self.u, "낙인 50 이하 상품")
        res = [ask_tools.run("product_filter", self.u, {"ki_max": 50})]
        flags, bad = ask_agent.check_answer(out["answer"], res, False)
        self.assertEqual(flags, [], f"정형 문구가 검사에 걸렸다: {bad}")


class DerivedNumberTests(TestCase):
    """표시값을 다른 단위로 바꿔 쓰면 접지에 걸려야 한다 (배수·월환산 등)."""

    RESULTS = [{"ok": True, "metrics": {
        "cumulative_return": {"value": 6208.3, "display": "+6,208.3%"},
        "cagr": {"value": 51.4, "display": "+51.4%"}}}]

    def test_배수_환산은_접지에_걸린다(self):
        flags, bad = ask_agent.check_answer(
            "누적수익률은 +6,208.3%로 약 63배가 되었습니다.", self.RESULTS, False)
        self.assertIn("NUMERIC_UNGROUNDED", flags)
        self.assertTrue(any("63" in b for b in bad))

    def test_월환산도_접지에_걸린다(self):
        flags, bad = ask_agent.check_answer(
            "연 +51.4%면 월 4.3% 수준입니다.", self.RESULTS, False)
        self.assertIn("NUMERIC_UNGROUNDED", flags)

    def test_프롬프트가_단위_환산을_금지한다(self):
        self.assertIn("다른 단위로 바꿔 쓰지 않는다", ask_agent.system_answer())


class HighlightTests(TestCase):
    """강조는 도구 표시값에만 붙는다 — 강조 자체가 접지의 증거가 되게."""

    RESULTS = [{"ok": True, "metrics": {
        "cumulative_return": {"value": 6208.3, "display": "+6,208.3%"},
        "mdd": {"value": -57.6, "display": "-57.6%"}}}]

    def test_양수는_hl_음수는_hl_red(self):
        h = ask_agent.highlight("누적 +6,208.3%, 최대낙폭 -57.6%.", self.RESULTS)
        self.assertIn('<span class="hl">+6,208.3%</span>', h)
        self.assertIn('<span class="hl-red">-57.6%</span>', h)

    def test_지어낸_숫자는_강조되지_않는다(self):
        h = ask_agent.highlight("누적 +9,999.9%였습니다.", self.RESULTS)
        self.assertNotIn("<span", h)

    def test_HTML은_이스케이프된다(self):
        h = ask_agent.highlight("<script>alert(1)</script> +6,208.3%", self.RESULTS)
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)

    def test_줄바꿈은_문단으로(self):
        self.assertEqual(ask_agent.highlight("가.\n나.", []), "<p>가.</p><p>나.</p>")


class MobileAndChartMarkTests(TestCase):
    """템플릿이 켜지려면 계약이 채워져 있어야 한다."""

    def setUp(self):
        # 300일째 고점에서 420일째까지 완만히 흘러내렸다가 회복하는 계열 —
        # 고점·저점이 서로 다른 월에 놓여야 음영 구간이 의미를 갖는다
        for i, d in enumerate(_bdays(date(2022, 1, 3), 700)):
            if i <= 300:
                v = 100 + i * 0.5
            elif i <= 420:
                v = 250 - (i - 300) * 0.9
            else:
                v = 142 + (i - 420) * 0.6
            PriceBar.objects.create(ticker="MU", date=d, close=v, adj_close=v)
        ask_tools._COV.update(day=None, data=None)

    def test_연도별_표에_모바일_리스트가_붙는다(self):
        calls = [{"name": "metric_calc",
                  "input": {"asset": "Micron", "metrics": ["cumulative_return"],
                            "group_by": "year"}}]
        res = [ask_tools.run("metric_calc", None, calls[0]["input"])]
        blocks, _ = ask_blocks.build(calls, res)
        table = next(b for b in blocks if b["type"] == "table")
        self.assertEqual(len(table["mobile"]), len(table["rows"]))
        first = table["mobile"][0]
        self.assertLessEqual({"label", "value", "tone", "sub"}, set(first))
        self.assertIn("연도중", first["sub"])

    def test_차트에_낙폭_음영과_고점저점_마커가_실린다(self):
        calls = [{"name": "price_series", "input": {"asset": "Micron", "freq": "monthly"}}]
        res = [ask_tools.run("price_series", None, calls[0]["input"])]
        blocks, _ = ask_blocks.build(calls, res)
        chart = next(b for b in blocks if b["type"] == "chart")
        self.assertIsNotNone(chart["shade"])
        self.assertEqual(chart["shade"]["y"], 20)
        kinds = {m["kind"] for m in chart["markers"]}
        self.assertEqual(kinds, {"peak", "trough"})
        for m in chart["markers"]:
            self.assertLessEqual(46.0, m["cx"])
            self.assertLessEqual(m["cx"], 706.0)
        trough = next(m for m in chart["markers"] if m["kind"] == "trough")
        self.assertIn("저점", trough["label"])
        self.assertTrue(trough["filled"])

    def test_커버리지_문구가_모집단을_구분해_말한다(self):
        line = ask_tools.coverage_line()
        self.assertIn("기초자산 시세", line)
        self.assertIn("수집 상품", line)
        self.assertIn("검색·통계 대상", line)


class PortfolioCommentTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.notes = [{
            "label": "기초자산 집중도", "name": "SK하이닉스",
            "value": 40, "guide": 30, "gap": 10,
            "amount": 40_000_000, "count": 4, "excess_amount": 10_000_000,
        }]

    @override_settings(ANTHROPIC_API_KEY="")
    def test_API키가_없어도_상세_계산문구를_준다(self):
        r = ask_agent.portfolio_comment_summary(self.notes, 100_000_000, date(2026, 9, 1))
        self.assertEqual(r["generated_by"], "calculation")
        self.assertIn("40%", r["text"])
        self.assertIn("10%p", r["text"])
        self.assertIn("10,000,000원", r["text"])
        self.assertNotIn("줄이", r["text"])

    @override_settings(ANTHROPIC_API_KEY="test-key", ASK_MODEL_INTERPRET="claude-haiku-4-5",
                       PORTFOLIO_AI_ENABLED=True)
    def test_Haiku가_없는_숫자를_쓰면_정형문구로_대체한다(self):
        fake = _resp([{"type": "text", "text": "현재 비중은 99%입니다."}])
        with mock.patch.object(ask_agent, "_call", return_value=fake):
            r = ask_agent.portfolio_comment_summary(self.notes, 100_000_000, date(2026, 9, 1))
        self.assertEqual(r["generated_by"], "calculation")
        self.assertNotIn("99%", r["text"])


class ExternalSearchTests(TestCase):
    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_검색결과의_출처와_숫자를_근거로_보존한다(self):
        fake = _resp([{
            "type": "text", "text": "공시상 매출은 12.3% 증가했습니다.",
            "citations": [{"url": "https://example.com/filing", "title": "공시"}],
        }])
        with mock.patch.object(ask_agent, "_call", return_value=fake):
            result, _ = ask_agent._external_search("claude-haiku-4-5", "최근 공시")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sources"][0]["url"], "https://example.com/filing")
        self.assertIn("12.3%", ask_tools.displays(result))

    def test_해석프롬프트에_내부우선과_검색제한이_있다(self):
        prompt = ask_agent.system_interpret()
        self.assertIn("external_search", prompt)
        self.assertIn("최대 1회", prompt)


class SearchViewCleanupTests(TestCase):
    """자연어 조건검색 입구는 /ask/ 하나만 남긴다."""

    def test_search_뷰가_ai를_더_만들지_않는다(self):
        r = self.client.get("/search/?q=&aiq=낙인 40 이하")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("ai", r.context)
        self.assertNotIn("ai_enabled", r.context)

    def test_ai_research는_실행부만_남았다(self):
        from core import ai_research
        self.assertFalse(hasattr(ai_research, "ask"))
        self.assertTrue(hasattr(ai_research, "run_filter"))
        self.assertTrue(hasattr(ai_research, "describe"))


def _bdays(start, n):
    """주말을 건너뛴 거래일 n개."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
