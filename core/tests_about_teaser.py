# -*- coding: utf-8 -*-
"""소개 랜딩(/ 와 /about/)의 'AI 분석 질문' 티저 섹션 테스트.

이 섹션은 순수 추가다 — 기존 카피는 한 글자도 바뀌면 안 된다. 그래서 여기서는
새 섹션이 떴는지만 보지 않고, 앞뒤 기존 문구가 원문 그대로인지까지 같이 못박는다.

한 번 섹션이 두 벌 들어간 사고가 있었다. 티저 CTA·카드가 페이지에 정확히
한 번만 나오는지 세는 테스트(test_no_duplicate_*)가 그 재발 방지선이다.
"""
import json
import re

from django.test import TestCase

from core import views
from core.views import ASK_PRESETS

# {# #} 를 두 줄에 걸쳐 쓰면 주석으로 인식되지 않고 화면에 그대로 찍힌다.
COMMENT_LEAK = re.compile(r"\{[#%{]|[#%}]\}|\{%\s*comment|endcomment\s*%\}")

SECTION_MARK = "<!-- ══ 6.6 AI 분석 질문 ══ -->"
FAQ_MARK = "<!-- ══ 6.7 FAQ ══ -->"
GO3_MARK = "<!-- ══ 6.5 시작 3단계 ══ -->"

CTA_TEXT = "AI 분석 질문 해보기"
CHIPS = [
    "Tesla와 삼성전자 10년 최대낙폭 비교",
    "연도별 ELS 실현수익률과 손실 건수",
    "낙인 40 이하 지수형 중 수익률 높은 3개",
]

# 티저 삽입 전 about.html 에 있던 문구 원문. 하나라도 어긋나면 기존 카피를
# 건드린 것이다. 앞뒤 이웃 섹션은 특히 촘촘히 넣는다.
UNTOUCHED_COPY = [
    # 히어로 (비로그인)
    "베타 기간 무료",
    '매주 쏟아지는 <span style="white-space:nowrap">ELS 300개,</span><br><span class="lp-acc">5개</span>면 충분합니다',
    "최적의 상품만 선별해드립니다.",
    "1분 가입 · 투자권유가 아닌 정보 제공 서비스",
    # 2. 문제
    "왜 골라야 하나",
    '같은 ELS인데<br>누구는 수익, 누구는 <span style="color:var(--red)">반토막</span>',
    "ELS는 상품마다 조건이 전부 다릅니다. 어떤 것을 고르느냐가 전부입니다.",
    "혹시, 이러고 계시지 않나요?",
    # 3. 해결책
    "어떻게 고르나",
    "고르는 눈을<br>데이터로 만들었습니다",
    "매주 자동 수집",
    "22년 백테스트",
    "AI가 추천하는 TOP5",
    # 4. 증명
    "10년 전체 검증 · 2016~2025",
    "같은 10년,<br>결과는 달랐습니다",
    "연도별로 보기",
    # 5. 타이밍
    "왜 지금인가",
    "시장 조정기가<br>ELS 투자의 최적기입니다",
    "고점에서 발행된 상품은 피합니다",
    # 6. 기초
    "ELS가 처음이라면",
    "원리는 3분이면 충분합니다",
    # 6.5 시작 3단계 — 티저 바로 앞
    "시작은 간단합니다",
    "여러분은 청약만 하세요.<br>나머지는 ELS 레이더가 챙겨드립니다",
    "이번 주 TOP5 확인",
    "쓰던 증권사 앱에서 청약",
    "평가일은 알려드립니다",
    # 6.7 FAQ — 티저 바로 뒤
    "자주 묻는 질문",
    "시작 전에<br>가장 많이 묻는 것들",
    "그 밖의 궁금증은 언제든 문의하기로 보내주세요",
    "정말 무료인가요?",
    "원금을 잃을 수도 있나요?",
    "타겟 신호는 어떻게 정해지나요?",
    # 7. 최종 CTA + 각주
    "다음 주 월요일,<br>새로운 TOP5가 찾아옵니다",
    "이번 주 상품의 청약은 이번 주에 끝납니다.",
    "ELS는 예금자보호 대상이 아니며 원금손실이 발생할 수 있습니다.",
]


class AboutTeaserTests(TestCase):
    """/ 와 /about/ 는 같은 뷰라 두 경로 모두에서 확인한다."""

    def setUp(self):
        # about() 는 날짜 키 모듈 캐시를 쓴다. 다른 테스트가 채워둔 ctx 를
        # 물려받으면 결과가 실행 순서에 따라 달라진다.
        views._ABOUT_CACHE.update(day=None, ctx=None)

    def _bodies(self):
        out = {}
        for path in ("/", "/about/"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            out[path] = res.content.decode()
        return out

    # ── 섹션이 뜬다 ────────────────────────────────
    def test_section_renders_on_both_paths(self):
        for path, body in self._bodies().items():
            self.assertIn(SECTION_MARK, body, path)
            self.assertIn('<span class="lp-label">AI 분석 질문</span>', body, path)
            self.assertIn("궁금한 건<br>한 줄로 물어보세요", body, path)
            self.assertIn("네 가지를 한 화면에서 묻습니다", body, path)

    def test_chips_cta_quota_and_disclaimer(self):
        for path, body in self._bodies().items():
            for chip in CHIPS:
                self.assertIn(chip, body, f"{path} / {chip}")
            self.assertIn(CTA_TEXT, body, path)
            self.assertIn("로그인 후 이용 &middot; 하루 3회 무료", body, path)
            self.assertIn(
                "사실·통계 조회 전용입니다. 추천, 매수·매도 의견, 시세 전망은 "
                "제공하지 않습니다.", body, path)

    def test_cta_points_at_ask(self):
        for path, body in self._bodies().items():
            self.assertIn(f'<a href="/ask/" class="btn btn-primary lp-btn">{CTA_TEXT}</a>',
                          body, path)

    def test_chips_match_shipped_ask_presets(self):
        """티저 예시는 /ask/ 화면의 실제 예시와 같은 문장이어야 한다.

        어느 한쪽만 바뀌면 눌러 들어간 사용자가 다른 문장을 만난다.
        """
        shipped = {item for group in ASK_PRESETS for item in group["items"]}
        for chip in CHIPS:
            self.assertIn(chip, shipped)

    # ── 중복 방지 (재발 방지선) ──────────────────────
    def test_no_duplicate_teaser_section(self):
        for path, body in self._bodies().items():
            self.assertEqual(body.count(SECTION_MARK), 1, f"{path}: 섹션 마커")
            self.assertEqual(body.count('class="card aq-card"'), 1, f"{path}: aq-card")
            self.assertEqual(body.count(CTA_TEXT), 1, f"{path}: CTA")
            self.assertEqual(body.count('<span class="lp-label">AI 분석 질문</span>'), 1,
                             f"{path}: 라벨")
            for chip in CHIPS:
                self.assertEqual(body.count(chip), 1, f"{path}: {chip}")

    def test_no_duplicate_teaser_css(self):
        for path, body in self._bodies().items():
            # 기본 규칙 1벌 + 880px 이하 오버라이드 1벌 = .aq-card 선언 2회가 정상
            self.assertEqual(body.count(".aq-card {"), 2, f"{path}: .aq-card 규칙")
            self.assertEqual(body.count(".aq-list {"), 1, f"{path}: .aq-list 규칙")
            self.assertEqual(body.count(".aq-note {"), 1, f"{path}: .aq-note 규칙")

    # ── 기존 카피 불변 ──────────────────────────────
    def test_existing_copy_untouched(self):
        for path, body in self._bodies().items():
            for line in UNTOUCHED_COPY:
                self.assertIn(line, body, f"{path}: 기존 카피가 바뀌었다 — {line!r}")

    def test_existing_section_order_preserved(self):
        """기존 7개 장의 순서가 그대로고, 티저만 6.5 와 6.7 사이에 끼어야 한다."""
        for path, body in self._bodies().items():
            order = [
                "왜 골라야 하나", "어떻게 고르나", "10년 전체 검증 · 2016~2025",
                "왜 지금인가", "ELS가 처음이라면", "시작은 간단합니다",
            ]
            idx = [body.index(s) for s in order]
            self.assertEqual(idx, sorted(idx), f"{path}: 기존 섹션 순서가 바뀌었다")
            self.assertLess(body.index(GO3_MARK), body.index(SECTION_MARK), path)
            self.assertLess(body.index(SECTION_MARK), body.index(FAQ_MARK), path)

    # ── 위생 ────────────────────────────────────────
    def test_no_template_comment_leak(self):
        # JSON-LD 는 스캔에서 뺀다 — 답변 객체가 "...}}," 로 닫혀서 `}}` 가
        # 정상적으로 들어 있다. 템플릿 문법이 실제로 샐 수 있는 건 본문 쪽이다.
        for path, body in self._bodies().items():
            scanned = re.sub(r'<script type="application/ld\+json">.*?</script>',
                             "", body, flags=re.S)
            leak = COMMENT_LEAK.search(scanned)
            self.assertIsNone(leak, f"{path}: 템플릿 문법이 화면에 샜다 — {leak}")

    def test_jsonld_still_parses(self):
        """티저는 Q&A 형태가 아니므로 FAQPage 구조화 데이터는 그대로여야 한다."""
        for path, body in self._bodies().items():
            m = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                          body, re.S)
            self.assertIsNotNone(m, f"{path}: JSON-LD 블록이 없다")
            data = json.loads(m.group(1))
            self.assertEqual(data["@type"], "FAQPage", path)
            self.assertEqual(len(data["mainEntity"]), 5, f"{path}: FAQ 항목 수")
            names = [q["name"] for q in data["mainEntity"]]
            self.assertIn("정말 무료인가요?", names, path)
            self.assertIn("타겟 신호는 어떻게 정해지나요?", names, path)
            # 티저 문구가 구조화 데이터로 새어 들어가면 안 된다
            self.assertNotIn(CTA_TEXT, m.group(1), path)

    def test_mobile_override_present(self):
        """880px 은 이 랜딩의 기존 분기점이다 — 티저도 같은 값을 써야 한다."""
        for path, body in self._bodies().items():
            css = body[body.index("/* AI 분석 질문 티저"):body.index("@media (max-width: 880px) {")]
            self.assertIn("@media (max-width:880px){", css, path)
            self.assertIn(".aq-item { font-size:13.5px;", css, path)

    def test_no_banned_wording_in_teaser(self):
        """새로 쓰는 글이므로 '최적'·'추천해' 류를 쓰지 않는다.

        기존 카피에는 남아 있지만(원문 보존), 티저 블록 안에는 없어야 한다.
        면책 문장의 '추천, 매수·매도 의견 ... 제공하지 않습니다' 는 부정문이라 예외다.
        """
        for path, body in self._bodies().items():
            block = body[body.index(SECTION_MARK):body.index(FAQ_MARK)]
            self.assertNotIn("최적", block, path)
            self.assertNotIn("추천해", block, path)
            self.assertNotIn("추천드", block, path)
            self.assertNotIn("보장", block, path)
            self.assertIn("제공하지 않습니다", block, path)
