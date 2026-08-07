"""상대 기간·능력 질문 처리 — 2026-08-07 조 팀장 첫 실사용 결함 회귀 방지.

실사용에서 "지난 10년"을 물었는데 해석턴이 2016-08-05~2024-08-06(8.00년)을
만들어 넘겼다. 틀린 구간의 숫자가 사실처럼 표에 나갔다. 날짜는 오늘에서
기계적으로 나오는 값이라 모델이 만들 여지를 없앤다.
"""
from datetime import date

from django.test import SimpleTestCase

from core.ask_agent import _ALL_ASSETS, _HELP_Q, help_answer, resolve_period

T = date(2026, 8, 7)


class ResolvePeriodTest(SimpleTestCase):
    def test_지난_10년은_오늘에서_10년_전까지다(self):
        s, e, label = resolve_period("지난 10년간 기초자산 수익률", T)
        self.assertEqual((s, e), ("2016-08-07", "2026-08-07"))
        self.assertEqual(label, "지난 10년")

    def test_지난_최근_없이_N년만_적어도_잡는다(self):
        # 실사용에서 가장 흔한 표현이고, 여기서 새면 모델이 날짜를 지어낸다
        s, e, _ = resolve_period("Micron 10년 누적수익률", T)
        self.assertEqual((s, e), ("2016-08-07", "2026-08-07"))

    def test_개월도_잡는다(self):
        s, e, label = resolve_period("최근 3개월 추이", T)
        self.assertEqual((s, e), ("2026-05-07", "2026-08-07"))
        self.assertEqual(label, "최근 3개월")

    def test_해를_넘기는_개월(self):
        s, e, _ = resolve_period("최근 14개월", T)
        self.assertEqual(s, "2025-06-07")
        self.assertEqual(e, "2026-08-07")

    def test_올해와_작년(self):
        self.assertEqual(resolve_period("올해 수익률", T)[:2],
                         ("2026-01-01", "2026-08-07"))
        self.assertEqual(resolve_period("작년 손실률", T)[:2],
                         ("2025-01-01", "2025-12-31"))

    def test_절대연도를_적었으면_건드리지_않는다(self):
        # 사용자가 구간을 직접 지정한 것이므로 서버가 덮어쓰면 안 된다
        self.assertEqual(resolve_period("2018년부터 2022년까지", T),
                         (None, None, None))

    def test_기간_표현이_없으면_없음(self):
        self.assertEqual(resolve_period("낙인 40 이하 지수형", T),
                         (None, None, None))

    def test_비상식적_길이는_무시한다(self):
        self.assertEqual(resolve_period("지난 99년", T), (None, None, None))

    def test_윤년_2월29일_기준일에도_깨지지_않는다(self):
        s, _, _ = resolve_period("지난 4년", date(2028, 2, 29))
        self.assertTrue(s.startswith("2024-02-"))


class HelpQuestionTest(SimpleTestCase):
    def test_능력_질문을_잡는다(self):
        for q in ["넌 멀 할수있어?", "너는 뭐야", "사용법 알려줘",
                  "어떻게 써?", "할 수 있는 거 뭐야", "도움말"]:
            self.assertTrue(_HELP_Q.search(q), q)

    def test_의견_요청이나_데이터_질문은_안_잡는다(self):
        # 이걸 안내로 돌리면 진짜 질문이 답을 못 받는다
        for q in ["이 상품 어때?", "Micron 10년 추이", "수익률 높은 3개",
                  "내 포트폴리오 낙인 여유"]:
            self.assertFalse(_HELP_Q.search(q), q)

    def test_안내문에_예시와_제공하지_않는_것이_함께_있다(self):
        txt = help_answer()
        self.assertIn("추천", txt)
        self.assertIn("전망은 제공하지 않습니다", txt)


class AllAssetsTest(SimpleTestCase):
    def test_대상을_안_좁힌_질문을_잡는다(self):
        for q in ["기초자산 전체 수익률", "모든 기초자산 비교", "주요 기초자산 추이"]:
            self.assertTrue(_ALL_ASSETS.search(q), q)

    def test_자산을_지정했으면_안_잡는다(self):
        for q in ["Micron 수익률", "코스피200 최대낙폭"]:
            self.assertFalse(_ALL_ASSETS.search(q), q)
