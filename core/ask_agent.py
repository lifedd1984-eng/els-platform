"""AI 분석 질문(/ask/) — 2턴 루프 + 프롬프트 캐싱 + 사후검사.

왜 2턴인가
  원안은 자산명 해석에 왕복을 한 번 더 썼다. 그 해석은 서버가 티커맵으로
  하면 되는 일이라 모델에 물을 이유가 없다. 서버로 흡수해 3턴 → 2턴이 됐고,
  프리픽스 재전송이 한 번 줄어 이게 가장 큰 절감이다.

턴 구성
  턴1 해석(Haiku)  : 질문 → 도구 호출. 스키마 채우기라 값싼 모델로 충분하다.
  턴2 설명(Sonnet) : 도구 결과 → 문장. 사용자가 읽는 글이라 품질이 드러난다.

  두 턴은 **모델이 다르므로 캐시도 따로**다. 그래서 턴2에는 데이터 도구
  스키마를 다시 보내지 않는다 — answer·refuse 둘만 실어 프리픽스를 얇게 만든다.
  Sonnet은 최소 캐시 프리픽스가 1,024토큰이라 이 얇은 프리픽스도 캐시에 걸린다.

⚠ 프리픽스에 시:분이나 요청별 값을 넣지 말 것. 커버리지 숫자는 자정 1회
  스냅샷(ask_tools.coverage)이라 하루 동안 바이트가 고정된다. 여기에
  timezone.now()를 한 번이라도 끼우면 캐시는 통째로 무의미해진다.
"""

import hashlib
import logging
import re
import time
from datetime import date

import requests
from django.conf import settings
from django.utils.html import escape

from . import ask_tools

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TIMEOUT = 45

# ── 단가 (USD / MTok, platform.claude.com 2026-08-06 기준) ──────────
# 캐시 쓰기(5분) = 입력×1.25, 캐시 읽기 = 입력×0.1
PRICING = {
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    "claude-sonnet-5": {"in": 3.00, "out": 15.00,
                        # 도입가는 2026-08-31까지. 지나면 자동으로 정가로 돌아간다.
                        "intro": {"in": 2.00, "out": 10.00, "until": date(2026, 8, 31)}},
    "claude-opus-4-8": {"in": 5.00, "out": 25.00},
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def _rate(model, today=None):
    p = PRICING.get(model) or {"in": 3.00, "out": 15.00}
    intro = p.get("intro")
    if intro and (today or date.today()) <= intro["until"]:
        return intro["in"], intro["out"]
    return p["in"], p["out"]


def cost_usd(model, usage, today=None):
    """usage dict → USD. 캐시 쓰기/읽기 배수를 반영한다."""
    rin, rout = _rate(model, today)
    return (
        usage.get("input_tokens", 0) * rin
        + usage.get("cache_creation_input_tokens", 0) * rin * CACHE_WRITE_MULT
        + usage.get("cache_read_input_tokens", 0) * rin * CACHE_READ_MULT
        + usage.get("output_tokens", 0) * rout
    ) / 1e6


# ══════════════════════════════════════════════════════════════════
# 도구 스키마 — 바이트가 고정돼야 캐시가 산다. 여기를 고치면 캐시가 깨진다.
# ══════════════════════════════════════════════════════════════════

DATA_TOOLS = [
    {
        "name": "price_series",
        "description": "티커의 시세 계열(차트용 축약 + 표시용 문자열).",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "자산명 그대로. 서버가 티커로 해석"},
                "start": {"type": "string", "description": "YYYY-MM-DD. 생략 시 보유 최초일"},
                "end": {"type": "string", "description": "YYYY-MM-DD. 생략 시 최신일"},
                "freq": {"type": "string", "enum": ["daily", "weekly", "monthly"],
                         "description": "축약 주기. 차트용. 계산은 항상 일별 전수"},
                "price_kind": {"type": "string", "enum": ["adjusted", "raw"],
                               "description": "adjusted=조정종가(수익률용, 기본), raw=미조정 종가(낙인 판정용)"},
            },
            "required": ["asset"],
        },
    },
    {
        "name": "metric_calc",
        "description": "구간 시세 지표 계산. 지표는 enum 고정, 구간·묶음은 자유.",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "자산명 그대로. 서버가 티커로 해석"},
                "metrics": {"type": "array", "items": {"type": "string", "enum": list(ask_tools.METRICS)},
                            "description": "계산할 지표"},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
                "group_by": {"type": "string", "enum": ["none", "year", "quarter", "month"],
                             "description": "구간 분할. year면 연도별로 같은 지표를 각각 계산한다."},
                "threshold_pct": {"type": "number", "description": "days_below/level_vs_high용 기준선(%)"},
                "price_kind": {"type": "string", "enum": ["adjusted", "raw"]},
            },
            "required": ["asset", "metrics"],
        },
    },
    {
        "name": "product_filter",
        "description": "ELS 상품 조건 검색. ELB·DLB 항상 제외.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["products", "my_portfolio"],
                          "description": "내 보유/포트폴리오 관련 질문이면 my_portfolio, 그 외 products"},
                "subscribing_only": {"type": "boolean",
                                     "description": "청약 가능한(마감 전) 상품만 원하면 true. '지금 살 수 있는' 등"},
                "asset_type": {"type": "string", "enum": ["종목형", "지수형"]},
                "ki_max": {"type": "number", "description": "낙인 상한 (이하). '낙인 40 이하' → 40"},
                "ki_min": {"type": "number", "description": "낙인 하한 (이상)"},
                "no_ki": {"type": "boolean", "description": "노낙인 상품만 원하면 true"},
                "yield_min": {"type": "number", "description": "연수익률 하한 (%)"},
                "loss_prob_max": {"type": "number", "description": "손실확률 상한 (%). '안전한' → 1 정도"},
                "early_1y_min": {"type": "number", "description": "1년내 조기상환 확률 하한 (%)"},
                "issuer": {"type": "string", "description": "발행사명 일부. 예: 키움, 삼성"},
                "asset_contains": {"type": "string", "description": "기초자산명 일부. 예: 테슬라, S&P"},
                "badge_only": {"type": "boolean", "description": "레이더 배지(아주 강한/강한 신호) 상품만"},
                "eval_within_days": {"type": "integer",
                                     "description": "다음 평가일이 N일 이내 (my_portfolio 전용)"},
                "sort": {"type": "string",
                         "enum": ["yield", "ki", "loss_prob", "sub_end", "early_1y", "next_eval"],
                         "description": "정렬 기준. 미지정 시 yield"},
                "sort_dir": {"type": "string", "enum": ["asc", "desc"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "unanswerable": {"type": "boolean",
                                 "description": "조회·필터로 답할 수 없는 질문(의견·추천·예측 요구 등)이면 true"},
            },
            "required": [],
        },
    },
    {
        "name": "product_stats",
        "description": "ELS 발행·상환 집계 통계.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string",
                           "enum": ["issue_count", "realized_yield", "loss_count", "loss_rate",
                                    "ki_distribution", "yield_distribution", "term_distribution",
                                    "asset_frequency", "issuer_share"],
                           "description": "무엇을 셀지"},
                "group_by": {"type": "string",
                             "enum": ["none", "year", "issuer", "asset", "asset_type", "ki_band"],
                             "description": "어떻게 묶을지"},
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
                "asset_type": {"type": "string", "enum": ["종목형", "지수형"]},
                "asset_contains": {"type": "string", "description": "기초자산명 일부"},
                "subscribing_only": {"type": "boolean", "description": "청약 마감 전 상품만"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["metric"],
        },
    },
    {
        "name": "portfolio_facts",
        "description": "본인 보유 포트폴리오 사실.",
        "input_schema": {
            "type": "object",
            "properties": {
                "views": {"type": "array",
                          "items": {"type": "string", "enum": list(__import__(
                              "core.portfolio_facts", fromlist=["VIEWS"]).VIEWS)},
                          "description": "summary=건수·투자금, concentration_*=집중도, ki_buffer=낙인 여유, "
                                         "next_eval=다가오는 평가일, stress_test=하락 시나리오"},
                "sort": {"type": "string", "enum": ["amount", "buffer", "eval_date"],
                         "description": "정렬 기준. buffer면 낙인까지 여유가 적은 순"},
                "within_days": {"type": "integer", "description": "next_eval용. N일 이내만"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["views"],
        },
    },
]

ANSWER_TOOL = {
    "name": "answer",
    "description": "최종 답변(도구 display 값만 사용).",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "3~5문장. 수치는 도구 display 값 그대로"},
            "numbers_used": {"type": "array", "items": {"type": "string"},
                             "description": "text에 쓴 수치 문자열 전부. 사후검사가 도구 반환값과 대조한다."},
        },
        "required": ["text", "numbers_used"],
    },
}

REFUSE_TOOL = {
    "name": "refuse",
    "description": "답할 수 없을 때 사유 코드와 대안.",
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string",
                     "enum": ["OUT_OF_RANGE", "NO_ASSET", "NO_DATA", "OPINION_REQUESTED",
                              "OUT_OF_SCOPE", "NEEDS_LOGIN"]},
            "detail": {"type": "string", "description": "왜 안 되는지 한 문장"},
            "alternatives": {"type": "array", "items": {"type": "string"},
                             "description": "대신 답할 수 있는 것 2~3개"},
        },
        "required": ["code", "detail"],
    },
}

RULES = (
    "너는 ELS 레이더의 데이터 질의응답기다. 사실·통계만 답한다.\n\n"
    "[절대 규칙]\n"
    "1. 숫자를 만들지 않는다. 답변 수치는 도구가 돌려준 display 값 그대로.\n"
    "2. 도구가 안 준 것은 \"없다\"고 답한다.\n"
    "3. 추천·매수/매도 의견·전망 금지.\n"
    "4. 평가어 금지: 안전/위험/좋다/유리/추천/적합.\n"
    "5. 미래 단정 금지.\n"
    "6. 1~5에 걸리면 refuse를 호출한다.\n"
)


def system_interpret():
    """턴1(해석) 시스템 프롬프트. 커버리지는 하루 고정 문자열이라 캐시가 산다."""
    c = ask_tools.coverage()
    return (
        RULES + "\n[커버리지]\n"
        f"· {ask_tools.coverage_line()}\n"
        "· KOSPI200은 지수 계열이며 결측 구간은 ETF 환산 보완값 — 쓰이면 근거에 명시된다.\n"
        "· 포트폴리오: 본인 보유 기록만.\n"
        "· 없는 것: 뉴스·재무제표·의견·환율·금리·신용등급·세금·실시간 시세.\n\n"
        "[진행]\n"
        "질문에 답하는 데 필요한 도구를 **이번 한 번에 전부** 호출한다(여러 개 동시 호출 가능). "
        "자산명은 그대로 넘긴다(서버가 티커로 해석·검증한다). 기간이 없으면 전체 구간. "
        "조회로 답할 수 없는 질문이면 refuse를 호출한다.\n"
    )


def system_answer():
    """턴2(설명) 시스템 프롬프트. 데이터 도구 스키마는 싣지 않는다."""
    return (
        RULES + "\n"
        "[역할]\n"
        "아래에 질문과 도구 조회 결과가 함께 온다. 결과만 근거로 answer를 호출한다.\n"
        "· 한국어 평서문 3~5문장. 도구가 준 display 문자열을 그대로 옮긴다.\n"
        "· 계산을 다시 하지 않는다. 도구에 없는 수치는 쓰지 않는다.\n"
        "· 표시값을 다른 단위로 바꿔 쓰지 않는다. '+6,208.3%'를 '약 63배',\n"
        "  '연 51.4%'를 '월 4.3%'처럼 환산하면 안 된다 — 도구가 준 문자열만 쓴다.\n"
        "· 포트폴리오 질문은 사실 나열까지만 — 정리·처분·추가매수·비중조절 같은 "
        "행동 제안은 어떤 형태로도 쓰지 않는다.\n"
        "· 결과가 ok=false면 그 reason·detail을 근거로 refuse를 호출한다.\n"
        "· numbers_used에는 text에 쓴 수치 문자열을 빠짐없이 넣는다.\n"
    )


# ══════════════════════════════════════════════════════════════════
# API 호출
# ══════════════════════════════════════════════════════════════════

def _call(model, system, tools, messages, max_tokens, tool_choice=None):
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    body = {
        "model": model,
        "max_tokens": max_tokens,
        # 시스템 블록 끝에 캐시 분기점. 렌더 순서가 tools → system 이라
        # 여기 하나면 도구 스키마까지 함께 캐시된다.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "tools": tools,
        "messages": messages,
    }
    # ⚠ Claude 5 계열은 thinking을 생략하면 adaptive가 켜지고 max_tokens가
    #   thinking+본문 합계 상한이 된다. 3~5문장 답변에 사고 예산을 태우면
    #   본문이 잘리므로 명시적으로 끄고 effort도 low로 둔다.
    #   Haiku 4.5는 output_config(effort)를 지원하지 않아 400을 낸다
    #   (2026-08-07 첫 실호출에서 확인) — 5 계열 모델에만 붙인다.
    if model.startswith(("claude-sonnet-5", "claude-opus-5", "claude-fable-5")):
        body["thinking"] = {"type": "disabled"}
        body["output_config"] = {"effort": "low"}
    if tool_choice:
        body["tool_choice"] = tool_choice
    r = requests.post(
        API_URL,
        headers={"x-api-key": api_key, "anthropic-version": API_VERSION,
                 "content-type": "application/json"},
        json=body, timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        # 본문에 키가 실릴 일은 없지만, 로그에 요청 바디는 남기지 않는다.
        logger.error("ask API %s %s: %s", model, r.status_code, r.text[:400])
        r.raise_for_status()
    return r.json()


def _usage_of(resp):
    u = resp.get("usage") or {}
    return {k: u.get(k, 0) for k in
            ("input_tokens", "output_tokens",
             "cache_creation_input_tokens", "cache_read_input_tokens")}


# ══════════════════════════════════════════════════════════════════
# 사후검사
# ══════════════════════════════════════════════════════════════════

GUARD_PATTERNS = {
    # 1군 — 권유
    "ADVICE": re.compile(
        r"추천(합니다|드립|해)|권(합니다|해\s?드|장합니다|유)|사(세요|십시오|시는 게)|"
        r"매수하|매도하|담으(세요|시는)|투자하(세요|시는 게)|"
        r"하는 (게|것이) (좋|낫)|편이 (좋|낫)"),
    # 2군 — 미래 단정
    "FUTURE": re.compile(
        r"(오를|내릴|상승할|하락할|회복할|떨어질|반등할)\s?(것|겁니|거예|전망|가능성이 높)|"
        r"예상됩니다|예상된다|전망입니다|전망됩니다|할 것으로 보입니다|될 것입니다"),
    # 3군 — 평가어
    "EVAL": re.compile(
        r"안전(합니다|한 편|해 보|하다고)|위험(합니다|한 편|해 보|하다고)|"
        r"괜찮(습니다|은 편)|유리(합니다|한 편)|불리(합니다|한 편)|"
        r"적합(합니다|한)|부적합|바람직|훌륭|우수(합니다|한 편)"),
    # 4군 — 포트폴리오 전용 행동 동사 (사실 나열 밖으로 나가는 순간)
    "PORTFOLIO_ACTION": re.compile(
        r"정리(하|해|를 고려)|처분(하|해)|갈아타|비중을?\s?(줄|늘|조절|낮|높)|"
        r"추가\s?매수|더 담|빼(시는|는 게)|털어|재조정|리밸런싱|분산하(시|세)"),
}

# 답변에서 뽑아낼 수치 토큰.
# ⚠ '배'·'포인트'가 빠지면 "+6,208.3%는 약 63배"처럼 **단위를 바꿔 환산한**
#   숫자가 검사를 통째로 빠져나간다. 파생 표기가 가장 잘 새는 자리다.
_NUM_TOKEN = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?"
    r"|[+\-−]?\d[\d,]*(?:\.\d+)?\s?"
    r"(?:%p|%|퍼센트|원|만원|억원|억|배|포인트|건|거래일|회|년|개월|일)"
    r"|[+\-−]?\d[\d,]*\.\d+"
)
_CORE = re.compile(r"\d[\d,]*(?:\.\d+)?")

GUARD_REPLACEMENT = (
    "이 질문은 조회 결과를 문장으로 옮기는 과정에서 자체 검사를 통과하지 못해 "
    "설명 문장을 표시하지 않습니다. 아래 표와 계산 근거는 서버가 직접 계산한 "
    "값이므로 그대로 확인하실 수 있습니다."
)


def _grounded(token, allowed, haystack):
    t = token.replace(" ", "").replace("−", "-")
    if t in allowed:
        return True
    if t.lstrip("+-") in allowed:
        return True
    core = _CORE.search(t)
    if not core:
        return True
    c = core.group(0)
    # 도구 결과 원문(JSON)에 그 숫자가 그대로 있으면 인정한다. 콤마 유무는 무시.
    return c in haystack or c.replace(",", "") in haystack


def check_answer(text, tool_results, has_portfolio, question=""):
    """(위반코드 리스트, 근거없는 수치 리스트).

    question을 함께 받는 이유: 사용자가 질문에 쓴 수치를 답이 되받아 쓰는 것은
    지어낸 게 아니다. "최대하락폭 이후 100%상승까지"를 물으면 답에 '100%'가
    나오는데, 이를 근거없는 수치로 보고 문장을 통째로 지우던 것을 고친다
    (2026-08-07 조 팀장 실사용에서 bad=['100%']로 확인).
    """
    flags, bad = [], []
    allowed = set()
    for r in tool_results:
        allowed |= ask_tools.displays(r)
    allowed = {a.replace(" ", "") for a in allowed}
    haystack = ask_tools.to_json(tool_results).replace(" ", "")
    asked = (question or "").replace(" ", "")

    for tok in _NUM_TOKEN.findall(text):
        t = tok.strip()
        if t.replace(" ", "") in asked:      # 질문이 쓴 수치를 되받은 것
            continue
        if not _grounded(tok, allowed, haystack):
            bad.append(t)
    if bad:
        flags.append("NUMERIC_UNGROUNDED")

    for name, pat in GUARD_PATTERNS.items():
        if name == "PORTFOLIO_ACTION" and not has_portfolio:
            continue
        if pat.search(text):
            flags.append(name)
    return flags, bad


def highlight(text, tool_results):
    """답변 문장의 수치를 강조한 HTML. **도구가 준 표시값만** 감싼다.

    모델이 쓴 글에서 정규식으로 '숫자처럼 생긴 것'을 잡아 칠하면, 지어낸
    숫자까지 예쁘게 강조된다. 도구 반환값 집합에 있는 문자열만 대상으로 삼아
    강조 자체가 접지의 증거가 되게 한다.

    ⚠ 텍스트를 먼저 이스케이프한 뒤 우리 태그만 끼워 넣는다. 반환값은
      템플릿에서 |safe 로 쓰이므로 이 순서가 뒤집히면 안 된다.
    """
    from django.utils.html import escape

    if not text:
        return ""
    dmap = {}
    for r in tool_results:
        ask_tools.display_map(r, dmap)
    safe = escape(text)
    # 긴 문자열부터 치환해야 '-57.6%' 안의 '57.6'이 먼저 먹히지 않는다
    targets = sorted((escape(d) for d in dmap if d and len(d) >= 2),
                     key=len, reverse=True)
    if targets:
        pat = re.compile("|".join(re.escape(t) for t in targets))
        unescape = {escape(d): d for d in dmap}

        def wrap(m):
            raw = unescape.get(m.group(0), m.group(0))
            v = dmap.get(raw)
            cls = "hl-red" if isinstance(v, (int, float)) and v < 0 else "hl"
            return f'<span class="{cls}">{m.group(0)}</span>'

        safe = pat.sub(wrap, safe)
    paras = [p for p in safe.split("\n") if p.strip()]
    return "".join(f"<p>{p}</p>" for p in paras)


# ══════════════════════════════════════════════════════════════════
# 후속 질문 — 서버 생성 (출력 토큰 절감 + 환각 표면 제거)
# ══════════════════════════════════════════════════════════════════

def followups(tool_calls, results):
    asset = None
    kinds = set()
    for call, res in zip(tool_calls, results):
        kinds.add(call["name"])
        if isinstance(res, dict) and res.get("asset") and asset is None:
            asset = res["asset"]
    out = []
    if asset:
        out += [f"{asset}이(가) 지금 10년 고점 대비 몇 % 자리야",
                f"{asset}이(가) 들어간 ELS는 지금까지 몇 건 나왔어",
                f"내 포트폴리오에 {asset}이(가) 얼마나 있어"]
    if "portfolio_facts" in kinds:
        out += ["다음 달 평가일이 오는 상품 몇 건이야",
                "기초자산별 비중이랑 집중도 정리해줘",
                "스트레스 시나리오에서 손실이 가장 큰 자산은"]
    if "product_filter" in kinds or "product_stats" in kinds:
        out += ["연도별 ELS 실현수익률과 손실 건수",
                "낙인 구간별 발행 비중 5년 추이",
                "이번 주 마감인 노낙인 상품"]
    if not out:
        out = ["연도별 ELS 실현수익률과 손실 건수",
               "낙인 40 이하 지수형 중 수익률 높은 3개",
               "내 보유 중 낙인까지 여유가 가장 적은 건"]
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:4]


# ══════════════════════════════════════════════════════════════════
# 본체
# ══════════════════════════════════════════════════════════════════

def normalize_question(q):
    return re.sub(r"\s+", " ", (q or "").strip())


# ══════════════════════════════════════════════════════════════════
# 상대 기간 — 서버가 계산한다
# ══════════════════════════════════════════════════════════════════
# 2026-08-07 조 팀장 첫 실사용에서 "지난 10년"을 물었는데 해석턴이
# 2016-08-05~2024-08-06(8.00년)을 만들어 넘겼다. 틀린 구간의 숫자가
# 사실처럼 표에 나갔다. 날짜는 모델이 생성할 값이 아니라 오늘에서
# 기계적으로 나오는 값이라, 해석턴에 넘기기 전에 서버가 확정한다.

# "지난/최근"이 없어도 "10년 추이"는 오늘 기준 10년이다 — 그 표현이
# 실제로 가장 흔하고, 여기서 새면 모델이 다시 날짜를 지어낸다
_REL_YEARS = re.compile(r"(?:지난|최근|근)?\s*(\d{1,2})\s*년(?:간|동안|치)?")
_REL_MONTHS = re.compile(r"(?:지난|최근|근)?\s*(\d{1,3})\s*개월(?:간|동안|치)?")
_THIS_YEAR = re.compile(r"올해|금년")
_LAST_YEAR = re.compile(r"작년|지난해")
_ABS_DATE = re.compile(r"\d{4}\s*년|\d{4}-\d{2}")


def resolve_period(q, today=None):
    """질문의 상대 기간 표현 → (start, end, 라벨). 없으면 (None, None, None).

    절대 연도를 함께 적었으면(예: "2018년부터") 사용자가 구간을 직접
    지정한 것이므로 건드리지 않는다.
    """
    today = today or date.today()
    if not q or _ABS_DATE.search(q):
        return None, None, None
    end = today
    m = _REL_YEARS.search(q)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 30:
            return None, None, None
        try:
            start = end.replace(year=end.year - n)
        except ValueError:            # 2/29
            start = end.replace(year=end.year - n, day=28)
        return start.isoformat(), end.isoformat(), f"지난 {n}년"
    m = _REL_MONTHS.search(q)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 360:
            return None, None, None
        y, mo = end.year, end.month - n
        while mo <= 0:
            y, mo = y - 1, mo + 12
        d = min(end.day, 28)
        return date(y, mo, d).isoformat(), end.isoformat(), f"최근 {n}개월"
    if _THIS_YEAR.search(q):
        return date(end.year, 1, 1).isoformat(), end.isoformat(), "올해"
    if _LAST_YEAR.search(q):
        return (date(end.year - 1, 1, 1).isoformat(),
                date(end.year - 1, 12, 31).isoformat(), "작년")
    return None, None, None


# "기초자산 전체"처럼 대상을 안 좁힌 질문 — 조용히 하나 고르면 안 된다
_ALL_ASSETS = re.compile(r"(모든|전체|여러|각|주요)\s*기초자산|기초자산\s*(전체|별|들)")

# 기능·사용법 질문 — 거절이 아니라 안내다. 모델까지 갈 일이 아니다
_HELP_Q = re.compile(
    r"(뭘|멀|무엇을|뭐를?|어떤\s*(걸|것|질문))\s*(할\s*수\s*있|물어|답)"
    r"|사용법|어떻게\s*(쓰|써|사용|물어)|도움말|기능\s*(설명|소개|알려)"
    r"|넌\s*(뭐|누구)|너는\s*(뭐|누구)|할\s*수\s*있는\s*(거|것)"
)


def help_answer():
    """능력·사용법 질문에 대한 정형 안내. 모델을 부르지 않아 비용이 0이다."""
    try:                                  # 예시는 화면과 같은 출처를 쓴다
        from .views import ASK_PRESETS as groups
    except Exception:
        groups = []
    lines = []
    for g in groups:
        items = (g.get("items") or [])[:2]
        if items:
            lines.append(f"{g.get('label', '')}: " + " / ".join(items))
    body = "\n".join(lines)
    return (
        "저장된 데이터를 조회해 사실과 통계로 답합니다. "
        "추천, 매수·매도 의견, 시세 전망은 제공하지 않습니다.\n\n"
        "이런 것을 물어보실 수 있습니다.\n" + body
    )


def question_key(q, stamp=None):
    """당일 캐시 키. 데이터 갱신일을 섞어 배치 전후 답이 섞이지 않게 한다.

    같은 날이라도 09:30 시세 배치를 지나면 같은 질문의 답이 달라진다.
    질문 문자열만으로 키를 잡으면 배치 전 답이 배치 뒤에도 재사용된다.
    """
    stamp = ask_tools.data_stamp() if stamp is None else stamp
    raw = f"{normalize_question(q)}|{stamp}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def run(user, question):
    """질문 하나 처리. 반환은 뷰가 그대로 템플릿에 넘길 수 있는 dict.

    호출 실패·거절도 예외를 던지지 않고 dict로 돌려준다 — 뷰가 분기 하나로
    끝나야 한도 차감 규칙이 흩어지지 않는다.
    """
    from . import ask_blocks

    t0 = time.monotonic()
    q = normalize_question(question)[: getattr(settings, "ASK_MAX_QUESTION_CHARS", 300)]
    limits = getattr(settings, "ASK_MAX_TOKENS", {"interpret": 700, "answer": 900})
    m_interp = getattr(settings, "ASK_MODEL_INTERPRET", "claude-haiku-4-5")
    m_answer = getattr(settings, "ASK_MODEL_ANSWER", "claude-sonnet-5")
    usage_by_model, total_cost = {}, 0.0

    def _acc(model, resp):
        nonlocal total_cost
        u = _usage_of(resp)
        prev = usage_by_model.setdefault(model, {k: 0 for k in u})
        for k, v in u.items():
            prev[k] += v
        total_cost += cost_usd(model, u)
        return u

    out = {
        "question": q, "answer": "", "answer_html": "", "blocks": [],
        "basis": None, "followups": [],
        "tools": [], "error": None, "status": "ok", "guard_flags": [],
        "ungrounded": [], "usage": usage_by_model, "cost_usd": 0.0, "elapsed_ms": 0,
    }

    # ── 능력·사용법 질문은 모델을 부르지 않는다 ────────────
    # "넌 뭘 할 수 있어?"가 OPINION_REQUESTED로 거절되던 것을 고친다
    # (2026-08-07 조 팀장 첫 실사용). 첫 사용자가 가장 먼저 던지는 질문이다.
    if _HELP_Q.search(q):
        txt = help_answer()
        out.update(answer=txt, answer_html=escape(txt).replace("\n", "<br>"),
                   status="ok", tools=["help"])
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return out

    # ── 상대 기간은 서버가 확정해 사실로 넘긴다 ─────────────
    p_start, p_end, p_label = resolve_period(q)
    hint = ""
    if p_start:
        hint = (f"\n[확정 기간] 이 질문의 '{p_label}'은 {p_start} ~ {p_end}이다. "
                "도구의 start·end에 이 값을 그대로 쓴다. 다른 날짜를 만들지 않는다.\n")
    if _ALL_ASSETS.search(q):
        hint += ("\n[대상] 질문이 기초자산을 특정하지 않았다. 임의로 하나를 고르지 말고, "
                 "여러 자산을 비교하려면 asset에 쉼표로 나열하거나, 어느 자산인지 "
                 "되물어야 하면 refuse(code=NO_ASSET)로 후보를 제시한다.\n")

    # ── 턴 1: 해석 (Haiku) ─────────────────────────────
    try:
        r1 = _call(m_interp, system_interpret() + hint, DATA_TOOLS + [REFUSE_TOOL],
                   [{"role": "user", "content": q}], limits["interpret"],
                   tool_choice={"type": "any"})
    except Exception:
        logger.exception("ask 해석턴 실패")
        out.update(status="error",
                   error={"code": "API_ERROR", "detail": "모델 호출이 실패했습니다.",
                          "message": "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."})
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return out
    _acc(m_interp, r1)

    calls = [b for b in r1.get("content", []) if b.get("type") == "tool_use"]
    if not calls:
        out.update(status="error",
                   error={"code": "NO_TOOL", "detail": "질문을 조회 조건으로 옮기지 못했습니다.",
                          "message": "질문을 조회 조건으로 옮기지 못했습니다. "
                                     "질문을 조금 더 구체적으로 적어 주세요."})
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return out

    # 모델이 곧장 거절한 경우 — 도구를 돌리지 않으므로 비용이 거의 없다
    refusal = next((c for c in calls if c["name"] == "refuse"), None)
    if refusal:
        inp = refusal.get("input") or {}
        out.update(status="refused",
                   error={"code": inp.get("code", "OUT_OF_SCOPE"),
                          "message": inp.get("detail", "답변할 수 없는 질문입니다."),
                          "detail": inp.get("detail", ""),
                          "alternatives": inp.get("alternatives") or []})
        out["cost_usd"] = round(total_cost, 6)
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return out

    # ── 도구 실행 ──────────────────────────────────────
    # 확정 기간이 있으면 모델이 준 날짜를 신뢰하지 않고 덮어쓴다. 프롬프트
    # 지시만으로는 다시 샌다 — 실제로 "지난 10년"에 8년치를 만들어 넣었다.
    for c in calls:
        if p_start and c["name"] in ("metric_calc", "price_series"):
            inp = c.setdefault("input", {}) or {}
            if inp.get("start") != p_start or inp.get("end") != p_end:
                out["guard_flags"].append("PERIOD_CORRECTED")
            inp["start"], inp["end"] = p_start, p_end
            c["input"] = inp
    results = []
    for c in calls:
        res = ask_tools.run(c["name"], user, c.get("input") or {})
        results.append(res)
    out["tools"] = [c["name"] for c in calls]
    has_portfolio = any(c["name"] == "portfolio_facts" for c in calls)

    # ── 조건검색만인 질문은 설명턴을 아예 돌리지 않는다 ──────────
    # 상품 목록 위에 자유 서술을 얹으면 "이 중 X가 가장 잘 맞습니다"류가
    # 반드시 샌다. 사후검사로 잡는 것보다 구조로 못 쓰게 하는 게 확실하다.
    # 덤으로 설명턴 비용이 통째로 빠진다.
    if calls and all(c["name"] == "product_filter" for c in calls):
        blocks, basis = ask_blocks.build(calls, results)
        ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
        if ok:
            chips = ", ".join(ok[0].get("chips") or []) or "지정한 조건"
            txt = (f"{chips} 조건으로 {ok[0]['n']}건을 찾았습니다. "
                   f"목록은 아래 표에 있으며, 원금지급형(ELB·DLB)은 제외했습니다.")
            out.update(answer=txt, answer_html=highlight(txt, results), status="ok")
        else:
            first = results[0] if results else {}
            out.update(status="refused",
                       error={"code": first.get("reason", "NO_DATA"),
                              "message": first.get("detail", "조건에 맞는 상품이 없습니다."),
                              "detail": first.get("detail", ""),
                              "alternatives": []})
        out["blocks"] = blocks
        out["basis"] = basis
        out["followups"] = followups(calls, results)
        out["cost_usd"] = round(total_cost, 6)
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return out

    # ── 턴 2: 설명 (Sonnet) ────────────────────────────
    # 차트 좌표 같은 덩어리는 빼고 보낸다 — 문장을 쓰는 데 안 쓰이는데 입력의
    # 절반 넘게 먹는다. 블록은 아래에서 원본(results)으로 만든다.
    payload = "\n".join(
        f"[{c['name']} 입력] {ask_tools.to_json(c.get('input') or {})}\n"
        f"[{c['name']} 결과] {ask_tools.to_json(ask_tools.slim_for_model(c['name'], r))}"
        for c, r in zip(calls, results))
    try:
        r2 = _call(m_answer, system_answer(), [ANSWER_TOOL, REFUSE_TOOL],
                   [{"role": "user", "content": f"[질문]\n{q}\n\n{payload}"}],
                   limits["answer"], tool_choice={"type": "any"})
    except Exception:
        logger.exception("ask 설명턴 실패")
        out.update(status="error",
                   error={"code": "API_ERROR", "detail": "모델 호출이 실패했습니다.",
                          "message": "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."})
        out["cost_usd"] = round(total_cost, 6)
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return out
    _acc(m_answer, r2)

    blocks, basis = ask_blocks.build(calls, results)
    out["blocks"] = blocks
    out["basis"] = basis
    out["followups"] = followups(calls, results)

    final = next((b for b in r2.get("content", []) if b.get("type") == "tool_use"), None)
    if final is None or final["name"] == "refuse":
        inp = (final or {}).get("input") or {}
        out.update(status="refused",
                   error={"code": inp.get("code", "NO_DATA"),
                          "message": inp.get("detail", "요청한 자료가 없습니다."),
                          "detail": inp.get("detail", ""),
                          "alternatives": inp.get("alternatives") or []})
        out["cost_usd"] = round(total_cost, 6)
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return out

    text = (final.get("input") or {}).get("text", "").strip()
    flags, bad = check_answer(text, results, has_portfolio, question=q)
    if flags:
        # ⚠ 숫자는 사실이라 지울 이유가 없다. 설명 문장만 정형 문구로 바꾸고
        #   표·차트·계산 근거는 그대로 노출한다.
        logger.warning("ask 사후검사 탈락 flags=%s bad=%s", flags, bad)
        out.update(answer=GUARD_REPLACEMENT, status="guarded",
                   answer_html=f"<p>{escape(GUARD_REPLACEMENT)}</p>",
                   guard_flags=flags, ungrounded=bad, raw_answer=text)
    else:
        out["answer"] = text
        out["answer_html"] = highlight(text, results)

    out["cost_usd"] = round(total_cost, 6)
    out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return out
