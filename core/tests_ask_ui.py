# -*- coding: utf-8 -*-
"""AI 분석 질문(/ask/) 화면 템플릿 렌더 테스트.

뷰·엔진 자체는 백엔드 담당이 맡는다. 여기서는 템플릿만 본다 —
views._ask_context 와 ask_blocks.build 가 내려주는 구조를 그대로 넣어
4가지 상태가 실제로 그려지는지, 주석이 화면에 새지 않는지,
네비가 로그인 여부에 맞게 나오는지.
"""
import datetime as dt
import re

from django.contrib.auth.models import AnonymousUser, User
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from core.views import ASK_DISCLAIMERS, ASK_PRESETS

# {# #} 를 두 줄에 걸쳐 쓰면 주석으로 인식되지 않고 화면에 그대로 찍힌다.
# 렌더 결과에 템플릿 문법 흔적이 남아 있으면 그 사고다.
COMMENT_LEAK = re.compile(r"\{[#%{]|[#%}]\}|\{%\s*comment|endcomment\s*%\}")

# ask_tools.coverage() 실출력 모양 (운영 DB 기준 수치)
COVERAGE = {
    "day": "2026-08-06", "price_first": "2016-08-05", "price_last": "2026-08-05",
    "tickers": [], "ticker_n": 44, "domestic_n": 20, "overseas_n": 24,
    "product_n": 4495, "listed_n": 4050, "excluded_types": ["ELB", "DLB"],
    "stat_year_from": 2015, "stat_year_to": 2026,
}


def _quota(used=1, limit=3, exhausted=False, unlimited=False):
    return {"used": used, "limit": limit, "unlimited": unlimited,
            "exempt_reason": None, "remaining": None if unlimited else max(0, limit - used),
            "exhausted": exhausted,
            "percent": 100 if unlimited else round(used / limit * 100),
            "reset_text": "내일 오전 0시에 다시 채워집니다."}


def _cell(text, tone=None, bold=False):
    return {"text": text, "tone": tone, "bold": bold}


# ask_blocks.build() 가 내는 것과 같은 모양
CHART_BLOCK = {
    "type": "chart", "viewbox": "0 0 720 244",
    "points": "46.0,193.1 271.5,146.7 706.0,33.6",
    "y_ticks": [{"y": 212, "label": "10", "solid": True},
                {"y": 120.6, "label": "100", "solid": False}],
    "x_ticks": [{"x": 46, "label": "2016"}, {"x": 706, "label": "2026"}],
    "y_bottom": 212, "y_top": 20, "x0": 46, "x1": 706,
    "shade": {"x": 563.0, "y": 20, "w": 55.0, "h": 192},
    "markers": [
        {"kind": "peak", "cx": 563.0, "cy": 103.9, "label": "고점 $152.36",
         "anchor": "middle", "filled": False},
        {"kind": "trough", "cx": 618.0, "cy": 137.9, "label": "저점 $64.56 (-57.6%)",
         "anchor": "start", "filled": True}],
    "legend": [{"color": "var(--blue)", "label": "Micron 조정종가"},
               {"color": None, "label": "로그 눈금 — 상승폭이 커서 선형 눈금이면 초반 구간이 보이지 않습니다"}],
    "last_label": "$893.19", "last_x": 706.0, "last_y": 33.6,
    "freq_note": "월말 종가", "n_points": 121,
}

# 첫 열이 아닌데도 텍스트인 열(기초자산)이 있는 표 — 정렬이 columns.num 을 따라야 한다
KI_TABLE = {
    "type": "table",
    "columns": [{"key": "p", "label": "상품", "num": False},
                {"key": "a", "label": "기초자산", "num": False},
                {"key": "lv", "label": "현재 레벨", "num": True},
                {"key": "bf", "label": "여유", "num": True}],
    "rows": [[_cell("NH투자증권 320"), _cell("SK하이닉스"),
              _cell("58.8%"), _cell("18.8%p", "red", True)],
             [_cell("키움증권 4066"), _cell("SK하이닉스"),
              _cell("54.1%"), _cell("19.1%p", "red", True)]],
    "foot": [_cell("합계 (투자 단위)", bold=True), _cell("—"), _cell("—"),
             _cell("32.00%", "red", True)],
}


def _answer_ctx(**over):
    ctx = {
        "active_nav": "ask",
        "presets": ASK_PRESETS,
        "coverage": COVERAGE,
        "quota": _quota(),
        "history": [],
        "from_cache": False,
        "log_id": 101,
        "ai_enabled": True,
        "question": "Micron 10년 추이랑 누적수익률, 연간수익률, 최대낙폭 정리해줘",
        "answer": "누적수익률은 +6,208.3%, 연평균 환산 +51.4%였습니다.\n\n"
                  "하루 기준 가장 큰 하락은 2020-03-16의 -19.8%였습니다.",
        # ask_agent.highlight() 출력 — 이스케이프 뒤 도구 표시값만 감싼 HTML
        "answer_html": '<p>누적수익률은 <span class="hl">+6,208.3%</span>, 연평균 환산 '
                       '<span class="hl">+51.4%</span>였습니다.</p>'
                       '<p>하루 기준 가장 큰 하락은 2020-03-16의 '
                       '<span class="hl-red">-19.8%</span>였습니다.</p>',
        "blocks": [
            {"type": "stats", "cards": [
                {"label": "누적수익률", "display": "+6,208.3%", "tone": "blue"},
                {"label": "최대낙폭 (MDD)", "display": "-57.6%", "tone": "red"},
            ]},
            {"type": "section", "title": "Micron 추이 (월말 종가 · 로그 눈금)", "icon": "fa-chart-area"},
            CHART_BLOCK,
            {"type": "section", "title": "낙인까지 여유 (적은 순)", "icon": "fa-shield-halved"},
            KI_TABLE,
            {"type": "note", "tone": "warn",
             "lines": ["고점 2024-06-18 $152.36 → 저점 2025-04-04 $64.56 (-57.6%).",
                       "고점 회복 2025-09-12."]},
            {"type": "chips", "items": ["낙인 40 이하", "지수형", "상위 3건"]},
        ],
        "basis": {"rows": [{"label": "기간", "value": "2016-08-05 ~ 2026-08-05 (10.00년)"},
                           {"label": "가격 계열", "value": "조정종가 — 배당·분할 소급 반영"}],
                  "tools": ["metric_calc", "price_series"],
                  "note": "답변에 쓰인 숫자는 전부 위 도구가 돌려준 값을 그대로 옮긴 것입니다."},
        "followups": ["Micron과 SK하이닉스 10년 수익률 비교"],
        "error": None,
        "status": "ok",
        "tools": ["metric_calc", "price_series"],
        "elapsed_ms": 3241,
        "elapsed_s": 3.2,
        "answered_at": "2026-08-06T09:41",
        "disclaimer": ASK_DISCLAIMERS["market"],
        "is_private": False,
    }
    ctx.update(over)
    return ctx


def _refused_ctx(**over):
    ctx = _answer_ctx(
        question="2010년부터 Micron 수익률 보여줘",
        answer=None, answer_html=None, blocks=[], basis=None, followups=[], tools=[],
        status="refused", disclaimer=None,
        error={"code": "OUT_OF_RANGE",
               "message": "2010년 자료는 없습니다. 보유한 기초자산 시세는 2016-08-05부터입니다.",
               "alternatives": ["2016-08-05 이후 임의 구간의 수익률·낙폭·변동성",
                                "다른 기초자산과의 비교"]})
    ctx.update(over)
    return ctx


def _quota_ctx(**over):
    at = timezone.make_aware(dt.datetime(2026, 8, 6, 9, 41))
    ctx = _answer_ctx(
        quota=_quota(used=3, limit=3, exhausted=True),
        question=None, answer=None, answer_html=None, blocks=[], basis=None, followups=[],
        tools=[], status=None, disclaimer=None, error=None, answered_at=None,
        elapsed_ms=None, elapsed_s=None,
        history=[{"id": 101, "question": "Micron 10년 추이랑 누적수익률, 연간수익률, 최대낙폭 정리해줘",
                  "at": at, "status": "ok", "tools": ["metric_calc"]},
                 {"id": 102, "question": "낙인 40 이하 지수형 중 수익률 높은 3개",
                  "at": at, "status": "ok", "tools": ["product_filter"]}])
    ctx.update(over)
    return ctx


class AskTemplateRenderTests(SimpleTestCase):
    """core/ask.html 4가지 상태."""

    def setUp(self):
        self.rf = RequestFactory()
        self.user = User(id=1, username="taehoon")

    def _render(self, ctx, user=None):
        req = self.rf.get("/ask/")
        req.user = self.user if user is None else user
        return render_to_string("core/ask.html", ctx, request=req)

    def test_정상답변_요약_지표_차트_표_근거_면책이_모두_그려진다(self):
        h = self._render(_answer_ctx())
        self.assertIn("Micron 10년 추이랑", h)                        # 질문 에코
        self.assertIn('누적수익률은 <span class="hl">+6,208.3%</span>', h)   # 요약 문장
        self.assertIn('2020-03-16의 <span class="hl-red">-19.8%</span>', h)
        self.assertEqual(h.count('class="stat-card"'), 2)             # 지표 카드
        self.assertIn("+6,208.3%", h)
        self.assertIn("<polyline", h)                                 # 차트
        self.assertIn('points="46.0,193.1 271.5,146.7 706.0,33.6"', h)
        self.assertIn("Micron 추이 (월말 종가 · 로그 눈금)", h)        # section 블록
        self.assertIn("<table>", h)
        self.assertIn("<tfoot>", h)
        self.assertIn("고점 회복 2025-09-12.", h)                     # note 블록
        self.assertIn('<details class="basis" open>', h)              # 계산 근거
        self.assertIn("조정종가 — 배당·분할 소급 반영", h)
        self.assertIn(ASK_DISCLAIMERS["market"], h)                   # 면책
        self.assertIn("이어서 물어볼 만한 것", h)                     # 후속 질문

    def test_답변문장은_두_문단으로_나뉜다(self):
        h = self._render(_answer_ctx())
        body = h[h.index('<div class="answer">'):]
        self.assertEqual(body[:body.index("</div>")].count("<p>"), 2)

    def test_answer_html의_수치강조가_그대로_나간다(self):
        """ask_agent.highlight() 가 씌운 .hl/.hl-red 는 살아서 나와야 한다."""
        h = self._render(_answer_ctx())
        self.assertIn('<span class="hl">+6,208.3%</span>', h)
        self.assertIn('<span class="hl-red">-19.8%</span>', h)

    def test_강조_스타일이_실제로_정의돼_있다(self):
        """마크업만 있고 CSS가 없으면 강조가 통째로 죽는다 — 눈으로만 알 수 있어서 못 박아 둔다."""
        h = self._render(_answer_ctx())
        self.assertIn(".answer .hl {", h)
        self.assertIn(".answer .hl-red {", h)

    def test_answer_html이_없으면_평문_문단으로_떨어진다(self):
        h = self._render(_answer_ctx(answer_html=None))
        self.assertIn("누적수익률은 +6,208.3%", h)
        self.assertNotIn('<span class="hl">', h)

    def test_질문은_이스케이프한다(self):
        """|safe 는 서버가 만든 answer_html 에만 건다. 사용자 입력은 절대 아니다."""
        h = self._render(_answer_ctx(question='<img src=x onerror="alert(1)">'))
        self.assertNotIn('<img src=x', h)
        self.assertIn("&lt;img src=x", h)

    def test_answer_html에_들어온_스크립트는_통과시키지_않는다(self):
        """highlight() 가 escape 후 태그를 끼우므로 원문 <script> 는 애초에 못 온다.
        계약이 깨져 날것이 오면 화면에 실행 가능한 태그로 남는다는 것을 못 박아 둔다."""
        h = self._render(_answer_ctx(answer_html="<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"))
        self.assertNotIn("<script>alert(1)</script>", h)

    def test_메타줄에_시각_소요시간_실행도구_남은횟수가_나온다(self):
        h = self._render(_answer_ctx())
        self.assertIn("2026-08-06 09:41", h)
        self.assertIn("3.2초", h)                     # elapsed_s 소수 1자리
        self.assertIn("<code>metric_calc</code>", h)
        self.assertIn("<code>price_series</code>", h)
        self.assertIn("오늘 1 / 3회", h)

    def test_차트에_낙폭음영과_고점저점_마커가_그려진다(self):
        h = self._render(_answer_ctx())
        self.assertIn('<rect x="563.0" y="20" width="55.0" height="192" '
                      'fill="#F7C9C2" opacity=".35"/>', h)
        self.assertIn("최대낙폭 구간", h)                                  # 범례
        self.assertIn('fill="#fff" stroke="#E5503C" stroke-width="1.6"', h)  # 고점 빈 점
        self.assertIn('cx="618.0" cy="137.9" r="3" fill="#E5503C"', h)       # 저점 채운 점
        self.assertIn("고점 $152.36", h)
        self.assertIn("저점 $64.56 (-57.6%)", h)
        self.assertRegex(h, r'text-anchor="start"[^>]*>저점')

    def test_낙폭구간이_없으면_음영도_마커도_안_그린다(self):
        chart = dict(CHART_BLOCK, shade=None, markers=[])
        blocks = [b if b.get("type") != "chart" else chart
                  for b in _answer_ctx()["blocks"]]
        h = self._render(_answer_ctx(blocks=blocks))
        self.assertNotIn("최대낙폭 구간", h)
        self.assertNotIn("#F7C9C2", h)
        self.assertIn("<polyline", h)

    def test_표_정렬은_columns의_num을_따른다(self):
        """기초자산은 두 번째 열이지만 텍스트라 왼쪽 정렬이어야 한다."""
        h = self._render(_answer_ctx())
        self.assertRegex(h, r'<td class=""[^>]*>SK하이닉스</td>')          # 2번째 열, 텍스트
        self.assertRegex(h, r'<td class=""[^>]*>NH투자증권 320</td>')      # 1번째 열, 텍스트
        self.assertRegex(h, r'<td class="num"[^>]*>58\.8%</td>')           # 3번째 열, 숫자
        self.assertRegex(h, r'<td class="num"[^>]*>18\.8%p</td>')

    def test_chips_블록은_배지로_그린다(self):
        h = self._render(_answer_ctx())
        self.assertIn('<span class="badge badge-blue" style="font-weight:600">낙인 40 이하</span>', h)

    def test_포트폴리오답변이면_가드블록과_전용면책이_붙는다(self):
        h = self._render(_answer_ctx(is_private=True, tools=["portfolio_facts"],
                                     disclaimer=ASK_DISCLAIMERS["portfolio"]))
        self.assertIn("본인 보유분만 조회", h)
        self.assertIn("포트폴리오 질문에 답하는 것", h)
        self.assertIn("포트폴리오 질문에서 차단하는 것", h)
        self.assertIn(ASK_DISCLAIMERS["portfolio"], h)
        self.assertNotIn(ASK_DISCLAIMERS["market"], h)

    def test_일반답변에는_가드블록이_없다(self):
        h = self._render(_answer_ctx())
        self.assertNotIn("포트폴리오 질문에서 차단하는 것", h)

    def test_저장된_결과를_다시_열면_차감없음을_알린다(self):
        h = self._render(_answer_ctx(from_cache=True))
        self.assertIn("저장된 결과 · 횟수 차감 없음", h)

    def test_거절화면은_사유코드와_대안을_보여준다(self):
        h = self._render(_refused_ctx())
        self.assertIn("2010년부터 Micron 수익률 보여줘", h)
        self.assertIn("2010년 자료는 없습니다.", h)
        self.assertIn("거절 사유 코드", h)
        self.assertIn("<code>OUT_OF_RANGE</code>", h)
        self.assertIn("대신 답할 수 있는 것", h)
        self.assertIn("다른 기초자산과의 비교", h)
        self.assertNotIn('<details class="basis"', h)
        self.assertNotIn("<polyline", h)

    def test_거절이어도_이미_돌린_표는_같이_보여준다(self):
        h = self._render(_refused_ctx(blocks=[KI_TABLE]))
        self.assertIn("거절 사유 코드", h)
        self.assertIn("NH투자증권 320", h)
        self.assertEqual(h.count('class="ask-echo"'), 1)   # 카드가 둘로 갈라지지 않는다

    def test_한도소진이면_입력창_대신_한도블록이_들어간다(self):
        h = self._render(_quota_ctx())
        self.assertNotIn('name="q"', h)                            # 입력창 없음
        self.assertIn("오늘 질문 3회를 모두 사용했습니다", h)
        self.assertIn("내일 오전 0시에 다시 채워집니다.", h)
        self.assertIn("width:100%", h)                             # 소진 게이지
        self.assertIn("?log=101", h)                               # 저장된 답변 열기
        self.assertIn("?log=102", h)

    def test_한도초과_오류카드는_한도블록과_겹쳐_그리지_않는다(self):
        h = self._render(_quota_ctx(question="아무 질문",
                                    error={"code": "QUOTA_EXCEEDED",
                                           "message": "오늘 질문 3회를 모두 사용했습니다."}))
        self.assertNotIn("거절 사유 코드", h)
        self.assertEqual(h.count("오늘 질문 3회를 모두 사용했습니다"), 1)

    def test_한도가_남으면_입력창과_예시칩_4갈래가_나온다(self):
        h = self._render(_answer_ctx())
        self.assertIn('name="q"', h)
        for g in ASK_PRESETS:
            self.assertIn(g["label"], h)
            for item in g["items"]:
                self.assertIn(item, h)
        self.assertIn("본인 것만", h)                              # 포트폴리오 갈래 자물쇠
        self.assertNotIn("오늘 질문 3회를 모두 사용했습니다", h)

    def test_무제한_계정은_한도_표시를_바꾼다(self):
        h = self._render(_answer_ctx(quota=_quota(used=7, limit=None, unlimited=True)))
        self.assertIn("오늘 <b style=\"color:var(--text-2)\">7</b>회", h)
        self.assertNotIn("하루 None회", h)

    def test_조회범위_안내는_coverage_line과_같은_라벨을_쓴다(self):
        """화면과 시스템 프롬프트가 서로 다른 수를 말하면 안 된다."""
        h = self._render(_answer_ctx())
        self.assertIn("기초자산 시세 44티커, 2016-08-05~2026-08-05", h)
        self.assertIn("수집 상품 4,495건 중 검색·통계 대상은 ELB·DLB 제외 4,050건", h)
        self.assertIn("SEIBro 상환 집계 2015~2026년", h)

    def test_어떤_상태에서도_템플릿_주석이_화면에_새지_않는다(self):
        states = {"정상": _answer_ctx(),
                  "포트폴리오": _answer_ctx(is_private=True,
                                       disclaimer=ASK_DISCLAIMERS["portfolio"]),
                  "거절": _refused_ctx(),
                  "한도소진": _quota_ctx()}
        for name, ctx in states.items():
            with self.subTest(state=name):
                leaks = COMMENT_LEAK.findall(self._render(ctx))
                self.assertEqual(leaks, [], "템플릿 문법이 렌더 결과에 남았다: %s" % leaks)


class AskNavTests(SimpleTestCase):
    """네비 노출 — /ask/ 는 로그인 필수라 로그인 사용자에게만 보인다."""

    def setUp(self):
        self.rf = RequestFactory()

    def _render(self, user):
        req = self.rf.get("/x/")
        req.user = user
        return render_to_string("core/search.html", {"q": "", "results": []}, request=req)

    def test_로그인하면_네비에_AI_분석_질문이_보인다(self):
        self.assertIn(">AI 분석 질문</a>", self._render(User(id=1, username="t")))

    def test_비로그인이면_네비에서_숨긴다(self):
        self.assertNotIn("AI 분석 질문", self._render(AnonymousUser()))


class SearchTemplateTests(SimpleTestCase):
    """/search/ — AI 조건 검색 블록은 /ask/ 로 옮기고 텍스트 검색만 남긴다."""

    def setUp(self):
        self.rf = RequestFactory()

    def _render(self, user, ctx=None):
        req = self.rf.get("/search/")
        req.user = user
        base = {"q": "", "results": [], "active_nav": "search"}
        base.update(ctx or {})
        return render_to_string("core/search.html", base, request=req)

    def test_AI_조건검색_입력창은_없어졌다(self):
        h = self._render(User(id=1, username="t"))
        self.assertNotIn('name="aiq"', h)
        self.assertNotIn("AI 조건 검색", h)

    def test_텍스트_검색은_그대로_남는다(self):
        h = self._render(User(id=1, username="t"))
        self.assertIn('name="q"', h)
        self.assertIn("발행사·상품번호·기초자산·상품명으로 전체 이력을 검색합니다", h)

    def test_로그인_사용자에게_AI_분석_질문_안내가_보인다(self):
        h = self._render(User(id=1, username="t"))
        self.assertIn("말로 조건을 쓰는 검색은 AI 분석 질문에서 합니다", h)

    def test_비로그인에게는_안내를_숨긴다(self):
        h = self._render(AnonymousUser())
        self.assertNotIn("말로 조건을 쓰는 검색은", h)
