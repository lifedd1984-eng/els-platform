from datetime import date, timedelta

from django.conf import settings
from django.db import models

# ELS/ELB 수익은 배당소득으로 과세: 소득세 14% + 지방소득세 1.4% = 15.4%
# (원금 제외, 수익분에만 부과)
DIVIDEND_TAX_RATE = 0.154

# 금융소득 종합과세 기준 (연 2천만원 초과 시 다른 소득과 합산)
FINANCIAL_INCOME_THRESHOLD = 20_000_000

# ══════════════════════════════════════════════════════════════════
# 레이더 신호 (v6) — 상품이 속한 "청약 주차 × 유형(지수형/종목형)" 그룹 안에서
#   ① 게이트로 통과 상품을 선별:
#      1) 낙인 있는 상품만(노낙인·낙인없음 제외)
#      2) 낙인 < 35(종목)/45(지수)
#      3) 1년내 조기상환 ≥ 80%
#      4) 손실확률 < 5%
#      5) 막차 배리어 ≤ 65(종목)/75(지수)
#      6) (게이트 통과분 중) 수익률 상위 50%
#   ② 점수 = 수익률 − 낙인 + 20, 내림차순 순위 →
#      상위 5위 = 아주 강한 신호, 6~15위 = 강한 신호, 나머지는 배지 없음.
# 주차별 상대평가라 과거 주차 결과는 고정 → 지난 상품도 동일하게 배지가 붙는다.
#
# ┌────────────────────────────────────────────────────────────────┐
# │ ⚙️  신호 로직 튜닝 파라미터 — 로직 변경은 아래 상수만 고치면 됨    │
# │     (계산 흐름은 _compute_radar_pool 함수, 이 상수들만 참조함)     │
# └────────────────────────────────────────────────────────────────┘
# 게이트 임계값 (그룹별)
RADAR_KI_EXCL = {"종목형": 35, "지수형": 45}    # 낙인 이 값 '이상'이면 제외
RADAR_LAST_MAX = {"종목형": 65, "지수형": 75}   # 막차 배리어 이 값 '이하'만 통과
RADAR_EARLY_MIN = 80           # 1년내 조기상환 % 이상만 통과
RADAR_LOSS_MAX = 5             # 손실확률 % '이상'이면 제외
RADAR_YIELD_TOP_PCT = 0.5      # 게이트 통과분 중 수익률 상위 이 비율만 (0.5 = 상위 50%)
RADAR_SCORE_SHIFT = 20         # 점수 = 수익률 − 낙인 + 이 값 (음수 방지)
# 등급 컷 (점수 순위 기준) — v6 유산. verify_historical 재현용으로 상수는 유지
RADAR_TOP_STRONG = 5    # 1 ~ 이 순위 = 아주 강한 신호
RADAR_TOP_WEAK = 15     # (상위)+1 ~ 이 순위 = 강한 신호, 그 외 배지 없음

# ── 레이더 v7 (2026-07-30 확정, 10개년 69,903건 백테스트 근거) ──────────
# 유형별 3중 게이트. 통과자 전원 배지, 순위는 수익률 내림차순.
#   최초 확정 당시 근거(구 규칙): 지수형 손실 0/2,607 · 종목형 3.11%
#   → 아래 두 차례 강화·완화를 거친 뒤의 확정치는 47행을 볼 것.
#   (2026-07-31에 뽑은 백테스트 엑셀은 이 구 규칙 기준이라 타겟 4,149건으로
#    나온다. 현행 규칙 기준은 5,392건이다 — 엑셀을 근거로 쓰지 말 것.)
# 종목형 85→80 강화 (2026-08-01 태훈님 B안 채택): 10년 손실률 3.11%→1.82%,
# 1년 성공률 87.7%→95.4%, 공급 주당 3.0→1.6개 — 안전 우선 결정.
# 2026-08-02 고점 게이트에 완만한 우상향 예외(1년 상승률 ≤15%) 추가 →
# 최종 성적: 타겟 합산 5,392건·정상상환 99.68%·주당 10.4개 (검증 표본 69,903건)
# 2026-08-05 고점 게이트를 '발행 시점' 기준으로 되돌림. 검증 스크립트는 처음부터
#   기준가 ÷ 발행 직전 1년 최고였는데 서비스 코드만 오늘 종가를 쓰고 있었다.
#   → 규칙 변경이 아니라 검증 복원. 되돌린 코드로 10년을 다시 돌려
#     5,392건·99.68%·손실 17건(지수형 4,566 / 종목형 826)을 그대로 재현 확인.
RADAR_V7_B0_MAX = {"종목형": 80, "지수형": 90}   # ① 1차 조기상환 배리어 이하
RADAR_V7_PEAK_MAX = 95    # ② 발행 시점 기준가의 직전 52주 최고 대비 위치(%) 미만
RADAR_V7_RELAX_RET1Y = 15  # ②-완화: 고점 95% 이상이어도 직전 1년 상승률이 이 값(%) 이하면 통과
RADAR_V7_KI_PCT = 0.30    # ③ 낙인 < 직전 연도 같은 유형 분포 하위 30% 값 (미만)
RADAR_V7_KI_MIN_SAMPLE = 100   # 직전 연도 표본이 이보다 적으면 v6 고정 컷으로 폴백
RADAR_V7_TIER = "타겟 신호"   # 두 유형 공통 배지명 (유형 구분은 지수형/종목형 배지가 담당)

RADAR_COLORS = {
    "타겟 신호": "#1B64DA",
    # v6 유산 — 과거 검증 기록(RadarVerdict) 표시용
    "아주 강한 신호": "#1B64DA", "강한 신호": "#3182F6",
}
# 별점 절대 컷 (★5,★4,★3,★2 경계 — 미달은 ★1). 수익성만 그룹 백분위 상대평가.
RADAR_STAR_EARLY = (95, 90, 85, 80)                 # 1년내 조기상환 % 이상
RADAR_STAR_LOSS = (0.0, 0.5, 1.0, 2.0)              # 손실확률 % 이하(0은 =0)
RADAR_STAR_KI = {"종목형": (20, 25, 30, 35),         # 낙인 % 이하
                 "지수형": (30, 35, 40, 45)}         # 지수형은 +10 완화
# ── 튜닝 파라미터 끝 ────────────────────────────────────────────────
_RADAR_POOL_CACHE = {}   # (monday_iso, asset_type) -> {"day": date|None, "map": {pid: result}}


def _radar_mini_points(ax):
    """4축 백분위 → 파비콘(24px) 폴리곤 좌표. 중심 12,12, 반경 9.5."""
    cx, cy, r = 12.0, 12.0, 9.5
    return (f"{cx:g},{cy - ax['yield'] / 100 * r:.1f} "
            f"{cx + ax['safe'] / 100 * r:.1f},{cy:g} "
            f"{cx:g},{cy + ax['early'] / 100 * r:.1f} "
            f"{cx - ax['defense'] / 100 * r:.1f},{cy:g}")


def _radar_pct(values, v):
    """정렬 없이 값 v의 그룹 내 백분위(0~100). 최상위 100, 최하위 0."""
    n = len(values)
    if n <= 1:
        return 100.0
    return (sum(1 for x in values if x <= v) - 1) / (n - 1) * 100


def _radar_early(p):
    sr = p.sim_result or {}
    e = sr.get("early_1y_pct")
    if e is None:
        e = sr.get("early_redemp_pct")
    return e or 0


def _radar_defense_metric(p):
    if p.is_no_ki or p.ki is None:
        return -1          # 노낙인 = 위험(배리어 이하 손실) → 방어 최하위
    return 100 - p.ki      # 낙인 낮을수록 buffer 큼


def _radar_stars(v):
    return max(1, min(5, int(v / 20 + 0.5)))


def _stars_early(e):
    """1년내 조기상환 % → 절대 별점."""
    for i, cut in enumerate(RADAR_STAR_EARLY):
        if e >= cut:
            return 5 - i
    return 1


def _stars_loss(loss):
    """손실확률 % → 절대 별점 (0%=★5)."""
    if loss <= RADAR_STAR_LOSS[0]:
        return 5
    for i, cut in enumerate(RADAR_STAR_LOSS[1:], start=1):
        if loss < cut:
            return 5 - i
    return 1


def _stars_ki(p):
    """낙인 % → 절대 별점 (지수형 완화 컷)."""
    if p.is_no_ki or p.ki is None:
        return 1
    cuts = RADAR_STAR_KI.get(p.asset_type, RADAR_STAR_KI["종목형"])
    for i, cut in enumerate(cuts):
        if p.ki <= cut:
            return 5 - i
    return 1


def _radar_points(ax):
    """4축 백분위 → SVG 폴리곤 좌표 (viewBox 150x130, 중심 75,64, 반경 38).
    수익성(위)·안전성(오른쪽)·조기상환(아래)·방어력(왼쪽)."""
    cx, cy, rad = 75.0, 64.0, 38.0
    top = f"{cx:g},{cy - ax['yield'] / 100 * rad:.1f}"
    right = f"{cx + ax['safe'] / 100 * rad:.1f},{cy:g}"
    bottom = f"{cx:g},{cy + ax['early'] / 100 * rad:.1f}"
    left = f"{cx - ax['defense'] / 100 * rad:.1f},{cy:g}"
    return f"{top} {right} {bottom} {left}"


def _radar_display_ax(p, yield_pct):
    """폴리곤·별점용 표시 점수(0~100).
    안전성·조기상환·방어력은 절대 컷 별점×20(값과 별이 항상 일치),
    수익성만 그룹 내 백분위 상대평가."""
    return {
        "yield": yield_pct,
        "safe": _stars_loss(p.loss_prob or 0) * 20,
        "early": _stars_early(_radar_early(p)) * 20,
        "defense": _stars_ki(p) * 20,
    }


def _radar_axes(p, ax):
    early = _radar_early(p)
    return [
        {"name": "수익성", "val": f"연 {p.yield_rate:g}%" if p.yield_rate else "-",
         "score": ax["yield"], "stars": _radar_stars(ax["yield"])},
        {"name": "안전성",
         "val": f"손실확률 {p.loss_prob:g}%" if p.loss_prob is not None else "-",
         "score": ax["safe"], "stars": _stars_loss(p.loss_prob or 0)},
        {"name": "조기상환", "val": f"1년내 {early:g}%" if early else "-",
         "score": ax["early"], "stars": _stars_early(early)},
        {"name": "방어력",
         "val": "노낙인 (배리어 이하 손실)" if p.is_no_ki else
                (f"낙인 {p.ki}% ({100 - p.ki}% 하락까지 수익상환)" if p.ki is not None else "-"),
         "score": ax["defense"], "stars": _stars_ki(p)},
    ]


_V7_KI_CUT_CACHE = {}   # (오늘, 유형) → 컷 값


def v7_ki_cut(asset_type):
    """낙인 컷 = 직전 연도 같은 유형 발행 분포의 하위 30% 값 (조건: ki < 컷).

    SEIBro 발행이력(HistoricalIssue)에서 산출하며 하루 단위 캐시.
    표본 부족 시 v6 고정 컷(RADAR_KI_EXCL)으로 폴백한다.
    """
    today = date.today()
    key = (today, asset_type)
    if key in _V7_KI_CUT_CACHE:
        return _V7_KI_CUT_CACHE[key]
    qs = HistoricalIssue.objects.filter(
        issue_date__year=today.year - 1, detail_fetched=True, ki__isnull=False)
    if asset_type == "지수형":
        qs = qs.filter(basset_sort="지수")
    else:
        qs = qs.exclude(basset_sort="지수")
    vals = sorted(qs.values_list("ki", flat=True))
    cut = (vals[int(len(vals) * RADAR_V7_KI_PCT)]
           if len(vals) >= RADAR_V7_KI_MIN_SAMPLE else RADAR_KI_EXCL[asset_type])
    _V7_KI_CUT_CACHE[key] = cut
    return cut


# ══ 고점대비 (고점 발행 회피 지표) ══════════════════════════════════
# 정의: 기준가 ÷ (그 시점 직전 52주 최고) × 100.
# ⚠ '평가 시점'이 두 가지이고, 둘을 섞으면 뜻이 정반대가 된다.
#   · 발행 시점 = 최초기준가격(공시값 우선, 없으면 산정일 종가)
#                → "고점에서 발행됐나". 발행이 끝나면 영원히 안 변한다.
#   · 현재 시점 = 최근 종가 → "지금 얼마나 빠져 있나". 매일 변한다.
# **게이트(v7_peak_gate)와 화면의 '발행 시점' 값은 둘 다 앞쪽**을 쓴다.
# 10년 검증 스크립트(EC2 sweep_peak_relax.py)가 쓴 식이 바로 이것이고
#   past = s[(s.index >= 발행일−365일) & (s.index < 발행일)];  ratio = 기준가 ÷ past.max()
# 되돌린 코드로 69,903건을 다시 돌려 5,392건·정상상환 99.68%·손실 17건을 재현했다.
# (2026-08-05. 그전 코드는 '오늘 종가'를 써서, 발행 후 폭락한 상품일수록 게이트를
#  통과하는 정반대 동작을 했고 과거 주차 배지가 워커 재활용마다 바뀌었다.)
#
# 창은 '캘린더 365일'이다. fetch_history(days=N)의 N은 yfinance가 **거래일** 수로
# 해석하므로(실측: 370d = 557캘린더일 ≈ 1.53년) 넉넉히 받아 날짜로 잘라 쓴다.
RADAR_PEAK_FETCH_DAYS = 750    # 약 3년치 거래일 — 2년 전 발행분까지 창을 덮는다
RADAR_PEAK_WINDOW_DAYS = 365   # 고점 산정 창(캘린더일) = 52주
_PEAK_MIN_ROWS = 30            # 창 안 거래일이 이보다 적으면 값을 내지 않는다
# 370거래일 = 실측 536~557캘린더일. 500일까지는 안전 마진을 두고 370을 재사용한다 —
# 화면과 게이트가 같은 창을 보게 돼 티커당 시세 조회가 한 번으로 끝난다.
_PEAK_SHORT_FETCH = 370
_PEAK_SHORT_COVER = 500


def _peak_fetch_days(as_of):
    """as_of 기준 52주 창을 덮는 가장 작은 fetch_history 창(거래일 수)."""
    need = (date.today() - as_of).days + RADAR_PEAK_WINDOW_DAYS
    return _PEAK_SHORT_FETCH if need <= _PEAK_SHORT_COVER else RADAR_PEAK_FETCH_DAYS


def _peak_from_series(hist, as_of, back=0, include_asof=True, disclosed=None):
    """[(날짜, 종가)] → (고점대비%, 직전 1년 상승률%). 못 구하면 (None, None).

    분자는 기준가 — disclosed(SEIBro 공시 최초기준가격)를 주면 그 값, 없거나
    기각되면 as_of 시점 종가(back거래일 보정 = 기준가 산정일 규칙과 같은 뜻).
    분모는 as_of 직전 52주 최고가.

    include_asof=False면 창에서 as_of 당일을 뺀다 — 10년 검증 스크립트의
    `s.index < str(h.issue_date)`와 같은 뜻이라 **게이트는 반드시 False**로 쓴다.
    True(기본)는 화면용: 당일이 최고가일 때 100%를 넘겨 띄우지 않으려는 것뿐이고,
    그 경우도 어차피 둘 다 95 이상이라 게이트 판정은 어느 쪽이든 같다.
    """
    if not hist or as_of is None:
        return None, None
    upto = [(d, c) for d, c in hist if c and d <= as_of]
    if len(upto) <= back:
        return None, None
    idx = len(upto) - 1 - back
    # 분자는 '기준가'다. SEIBro 공시 최초기준가격을 주면 그쪽이 우선(검증 스크립트와 동일),
    # 정규화 상수·시세 괴리로 기각되면 as_of 종가로 폴백한다 — 판단은 market이 한다.
    ref_c = upto[idx][1]
    if disclosed:
        from core import market as _m
        try:
            d = float(str(disclosed).replace(",", "").strip())
        except (TypeError, ValueError):
            d = 0.0
        if d > 0:
            ref_c = _m.pick_ref_price(d, ref_c)[0] or ref_c
    # 창의 기준점은 ref 종가가 찍힌 거래일이 아니라 as_of(달력 날짜)다 —
    # 검증 스크립트가 `issue_date - 365일 ~ issue_date`로 자르는 것과 같게 하려는 것.
    # as_of가 휴장일이면 ref 종가 자체가 창 안에 들어오는 것까지 검증과 동일하다.
    lo = as_of - timedelta(days=RADAR_PEAK_WINDOW_DAYS)
    win = [c for d, c in upto[:idx + 1] if d >= lo and (include_asof or d < as_of)]
    if len(win) < _PEAK_MIN_ROWS or not ref_c:
        return None, None
    ret1y = (ref_c / win[0] - 1) * 100 if win[0] else None
    return ref_c / max(win) * 100, ret1y


# 화면의 색·문구는 찍어 주는 **반올림값이 아니라 반올림 전 원값**으로 갈라야 한다.
# round(94.98) = 95라, 게이트가 통과시킨 상품에 "고점 부근에서 발행됐습니다"라는
# 정반대 문구와 빨간 숫자가 붙었다(2026-08-06 실측 14건). 표시 숫자는 그대로 두고
# 판단 근거만 원값으로 되돌린다 — 게이트 판정 자체는 건드리지 않는다.
PEAK_LEVEL_MID = 90     # 화면 색 구분용 '주의' 경계. 게이트 기준(95)과는 별개다.


def peak_level(ratio):
    """고점대비 원값(%) → 화면 색 단계 'high' | 'mid' | 'low'. 값이 없으면 None.

    경계가 정수라 반올림한 뒤 비교하면 94.98%가 'high'로 넘어간다. 반드시
    반올림 전 값으로 가른다.
    """
    if ratio is None:
        return None
    if ratio >= RADAR_V7_PEAK_MAX:
        return "high"
    return "mid" if ratio >= PEAK_LEVEL_MID else "low"


def peak_tone(ratio, gate_block):
    """상품상세 고점대비 숫자 색 'warn' | 'watch' | 'ok'. 값이 없으면 None.

    경고색(warn)은 **게이트가 실제로 걸러낸 상품에만** 쓴다. 문구는 "통과"라고
    하면서 숫자만 빨갛게 두면 그게 또 어긋나기 때문이다 (2026-08-06 태훈님 확정).
    통과·판정불가는 원값이 PEAK_LEVEL_MID 이상이면 watch, 아니면 ok.
    """
    if ratio is None:
        return None
    if gate_block:
        return "warn"
    return "ok" if peak_level(ratio) == "low" else "watch"


def peak_gate_verdict(product, refs=None):
    """화면 문구용 — 고점 게이트의 판정을 그대로 네 갈래로 돌려준다.

    반환 (통과, 완화예외로_통과, 규칙위반, 게이트가_본_원값)
      · 통과 + 원값 95 미만 → 고점 발행이 아니다
      · 통과 + 원값 95 이상 → 완만한 상승 예외로 통과 (= 완화 예외)
      · 탈락 + 원값 있음    → 고점 부근 발행
      · 원값 None           → 시세 결측이라 판정 못 함 (앞의 셋 모두 False)

    ⚠ 완화 예외는 **내부 구분일 뿐 화면에서는 갈라 보이지 않는다.** 어느 경로로
    통과했든 사용자에게는 "고점 발행 기준을 통과했다" 하나면 된다는 것이
    2026-08-06 태훈님 확정. 화면은 gate_pass만 보고, gate_relaxed는 로그·분석용.

    판정은 v7_peak_gate가 하고 여기서 되풀이하지 않는다. 문구가 게이트와
    어긋나지 않게 하는 것이 이 함수의 전부다.
    """
    ok, graw = v7_peak_gate(product, refs)
    if graw is None:
        return False, False, False, None
    return ok, bool(ok and graw >= RADAR_V7_PEAK_MAX), not ok, graw


def peak_as_of(product):
    """상품의 고점대비 평가 시점 → (기준일, 거래일오프셋, 발행완료여부).

    발행이 끝났으면 최초기준가격 산정일, 아직이면 오늘(= 발행 예정 시점의 대용).
    """
    from core import market as _m
    base, back = _m.base_price_date(product)
    today = date.today()
    if base and base <= today:
        return base, back, True
    return today, 0, False


def peak_ratios(product):
    """상품 고점대비를 '발행 시점'과 '현재 시점' 두 갈래로 계산한다.

    반환 {"rows": [{"asset", "issue", "now"}], "issued": bool, "base_date": date|None,
          "issue_max": %|None, "now_max": %|None, "issue_tone": 'warn'|'watch'|'ok'|None,
          "gate_pass": bool, "gate_relaxed": bool, "gate_block": bool, "gate_peak": %|None}
    자산 하나라도 시세를 못 구하면 그 갈래의 max는 None (오판보다 결측 — 게이트와 동일).

    gate_* 는 화면 문구·색 전용이다. 숫자는 반올림해 보여주되 "고점 발행이냐"는
    판단만은 게이트가 실제로 쓴 원값으로 갈라, 경계에서 표시와 판정이 어긋나지
    않게 한다 (2026-08-06).
    """
    from core import market as _m
    base, back, issued = peak_as_of(product)
    days = _peak_fetch_days(base)
    refs = peak_ref_prices([product]).get(product.id) or {}
    rows = []
    for name in _m.split_assets(product.assets_raw or ""):
        tk = _m.resolve_ticker(name)
        hist = _m.fetch_history(tk, days=days) if tk else None
        # 발행 시점은 게이트와 같은 기준가(공시 우선), 현재 시점은 언제나 최근 종가
        pi = (_peak_from_series(hist, base, back, disclosed=refs.get(name))[0]
              if (hist and issued) else None)
        pn = _peak_from_series(hist, hist[-1][0], 0)[0] if hist else None
        rows.append({"asset": name,
                     "issue": round(pi) if pi is not None else None,
                     "now": round(pn) if pn is not None else None,
                     "issue_raw": pi, "now_raw": pn})

    def _worst(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return max(vals) if (rows and len(vals) == len(rows)) else None

    # round는 단조라 max(round(...)) == round(max(...)) — 보이는 숫자는 그대로다.
    iraw = _worst("issue_raw") if issued else None
    nraw = _worst("now_raw")
    ok, relaxed, blocked, graw = peak_gate_verdict(product, refs)
    return {"rows": rows, "issued": issued,
            "base_date": base if issued else None,
            "issue_max": round(iraw) if iraw is not None else None,
            "issue_tone": peak_tone(iraw, blocked),
            "now_max": round(nraw) if nraw is not None else None,
            "gate_pass": ok, "gate_relaxed": relaxed, "gate_block": blocked,
            "gate_peak": graw}


def attach_peak_ratios(products):
    """상품 목록에 고점대비(%)를 붙인다 — p.peak_ratio / p.peak_is_issue.

    p.peak_ratio  : 발행 완료분은 **발행 시점** 값, 청약 중(미발행)은 현재 값.
    p.peak_is_issue: 위 값이 발행 시점 기준인지 여부(화면 설명 문구 분기용).
    p.peak_level  : 색 단계 'high'/'mid'/'low' — 반올림 전 원값으로 가른다.
                    (템플릿이 반올림된 peak_ratio를 95와 비교하면 94.98%가
                     'high'로 넘어가 게이트 통과분이 경고색을 뒤집어썼다.)

    티커 시세는 한 번만 받고, 값은 (티커, 기준일, 오프셋, 기준가)로 캐시한다 —
    한 주 상품 283건이 구분 티커 23개·기준일 5개뿐이라 사실상 공짜다.
    공시 기준가도 상품별 왕복 대신 peak_ref_prices로 한 번에 받는다.
    """
    from core import market as _m
    hist_cache, val_cache = {}, {}
    refs = peak_ref_prices(products)
    for p in products:
        as_of, back, issued = peak_as_of(p)
        days = _peak_fetch_days(as_of)
        pref = refs.get(p.id) or {}
        peak = None
        for name in _m.split_assets(p.assets_raw or ""):
            tk = _m.resolve_ticker(name)
            if not tk:
                peak = None
                break
            if (tk, days) not in hist_cache:
                hist_cache[(tk, days)] = _m.fetch_history(tk, days=days)
            ref = pref.get(name) if issued else None
            key = (tk, as_of, back, ref)
            if key not in val_cache:
                # 캐시에는 원값을 담는다 — 색 단계를 반올림 전 값으로 갈라야 해서다.
                val_cache[key] = _peak_from_series(
                    hist_cache[(tk, days)], as_of, back, disclosed=ref)[0]
            r = val_cache[key]
            if r is None:
                peak = None
                break
            peak = r if peak is None else max(peak, r)
        # round는 단조라 max를 먼저 잡고 반올림해도 보이는 숫자는 종전과 같다.
        p.peak_ratio = round(peak) if peak is not None else None
        p.peak_level = peak_level(peak)
        p.peak_is_issue = issued


def peak_ref_prices(products):
    """상품들의 SEIBro 공시 최초기준가격을 한 번에 조회 → {product_id: {자산명: 기준가|None}}.

    연결 키는 Product.product_code == HistoricalIssue.isin. 상품마다 따로 조회하면
    한 주 목록에서 수백 번 왕복하므로 코드 전체를 한 번에 받아 나눈다.
    대응이 모호하면(자산 개수 불일치·티커 중복 등) market이 그 상품 전체를 폴백으로
    돌려주므로 여기서는 값만 꺼내 쓴다.
    """
    from core import market as _m
    codes = {(getattr(p, "product_code", "") or "").strip() for p in products}
    codes.discard("")
    amap = (dict(HistoricalIssue.objects.filter(isin__in=codes)
                 .values_list("isin", "assets")) if codes else {})
    out = {}
    for p in products:
        sb = amap.get((getattr(p, "product_code", "") or "").strip())
        try:
            out[p.id] = {k: v[0] for k, v in
                         _m.disclosed_asset_prices(p.assets_raw or "", sb).items()} if sb else {}
        except Exception:
            out[p.id] = {}
    return out


def v7_peak_gate(p, refs=None):
    """고점 발행 회피 게이트 판정 → (통과 여부, 표시용 고점위치%).

    **발행 시점 기준**이다 (2026-08-05 태훈님 확정). 최초기준가격을 그 직전 52주
    (기준일 당일 제외) 최고가로 나눈다. 10년 검증 스크립트(sweep_peak_relax.py)의
        past = s[(s.index >= 발행일-365일) & (s.index < 발행일)];  ref / past.max()
    와 같은 식이다 — 규칙 변경이 아니라 검증 복원이다.
    발행 전(청약 중)이면 기준가가 아직 없으므로 최근 종가를 쓴다. 발행이 끝나는
    순간 값이 고정되므로 과거 주차를 언제 다시 계산해도 답이 같다.

    자산별로: 고점 대비 95% 미만이면 통과. 95% 이상이면 '급등 후 고점'만
    걸러내기 위해 직전 1년 상승률을 본다 — 상승률이 RADAR_V7_RELAX_RET1Y
    이하인 완만한 우상향 자산은 고점 부근이어도 통과시킨다.
    (2026-08-01 채택. 10년 검증: 완화 15%에서 공급 주당 6.5→10.4개,
     손실 15→17건. 20% 이상으로 열면 2021 HSCEI형이 14건 되살아나 기각.)

    refs: peak_ref_prices()가 준 {자산명: 공시 기준가} — 안 주면 이 상품 것만 조회한다.
    자산 하나라도 위반이면 탈락. 시세를 못 구하면 (False, None) — 오판보다 결측.
    """
    from core import market as _m
    as_of, back, issued = peak_as_of(p)
    days = _peak_fetch_days(as_of)
    if refs is None:
        refs = peak_ref_prices([p]).get(p.id) or {}
    peak_disp = None
    for name in _m.split_assets(p.assets_raw or ""):
        tk = _m.resolve_ticker(name)
        if not tk:
            return False, None
        ratio, ret1y = _peak_from_series(
            _m.fetch_history(tk, days=days), as_of, back, include_asof=False,
            disclosed=refs.get(name) if issued else None)
        if ratio is None:
            return False, None
        peak_disp = ratio if peak_disp is None else max(peak_disp, ratio)
        if ratio >= RADAR_V7_PEAK_MAX and (ret1y is None or ret1y > RADAR_V7_RELAX_RET1Y):
            return False, peak_disp
    return True, peak_disp


def _compute_radar_pool(monday, asset_type):
    """(주차, 유형) 그룹의 {product_id: radar_result} 계산 — v7 3중 게이트.

    ① 1차 배리어 ≤ 유형별 상한 (지수 90 / 종목 80)
    ② 낙인 < 직전 연도 같은 유형 분포 하위 30% (미만)
    ③ 고점 발행 회피 — **발행 시점** 기준가가 그 직전 52주 최고 대비 95% 미만
       (95% 이상이어도 직전 1년 상승률 15% 이하면 통과 — 완만한 우상향 예외)
    통과자 전원 타겟 신호 배지, 순위는 수익률순.

    ③이 발행 시점 기준이라 발행이 끝난 주차의 결과는 오늘 시세와 무관하다 →
    같은 주차를 언제 다시 계산해도 답이 같다(_radar_pool 캐시가 날아가도 안전).
    """
    sunday = monday + timedelta(days=6)
    group = list(Product.objects.filter(
        sub_end__gte=monday, sub_end__lte=sunday, asset_type=asset_type))
    if not group:
        return {}
    b0_max = RADAR_V7_B0_MAX[asset_type]
    ki_cut = v7_ki_cut(asset_type)

    # 값싼 게이트(배리어·낙인) 먼저, 시세가 필요한 고점 게이트는 생존자에만
    cheap = [p for p in group if (
        (not p.is_no_ki and p.ki is not None) and p.ki < ki_cut       # ② 낙인
        and p.barrier_first is not None and p.barrier_first <= b0_max  # ① 1차 배리어
    )]
    refs = peak_ref_prices(cheap)          # 공시 기준가 한 번에 (상품별 왕복 방지)
    survivors = []
    for p in cheap:
        ok, peak = v7_peak_gate(p, refs.get(p.id))                     # ③ 고점 회피(완화 포함)
        if ok and peak is not None:
            survivors.append((p, peak))

    ranked = sorted(survivors, key=lambda t: -(t[0].yield_rate or 0))
    eligible_n = len(ranked)
    tier = RADAR_V7_TIER
    yield_col = [p.yield_rate or 0 for p in group]

    result = {}
    for i, (p, peak) in enumerate(ranked):
        y_pct = round(_radar_pct(yield_col, p.yield_rate or 0))
        ax = _radar_display_ax(p, y_pct)
        result[p.id] = {
            "tier": tier, "color": RADAR_COLORS[tier],
            "srank": i + 1, "group_n": eligible_n,
            "score": round(p.yield_rate or 0, 1),
            "peak": round(peak),
            "reasons": [],
            "points": _radar_points(ax), "mini_points": _radar_mini_points(ax),
            "axes": _radar_axes(p, ax),
        }
    return result


def _radar_pool(monday, asset_type):
    """(주차, 유형) 풀을 캐시와 함께 반환. 발행이 다 끝난 주차는 영구 캐시,
    아직 청약·발행이 걸쳐 있는 주차는 하루 단위로 갱신.

    게이트가 발행 시점 기준이 된 뒤로 이 캐시는 순전히 속도용이다 — 날아가서
    다시 계산해도 발행 완료분은 같은 답이 나온다(2026-08-05 전량 대조 0건 차이).
    다만 **미발행 상품이 남은 주차**는 아직 오늘 종가로 재는 중이라 값이 움직인다.
    청약 마감 다음 영업일이 발행일이므로 일요일 기준 +3일이면 전부 발행이 끝난다.
    그 전까지는 영구 캐시로 굳히지 않는다 — 굳히면 워커마다 다른 날의 시세가
    박제돼 예전의 '워커 재활용마다 배지가 바뀌는' 문제가 그 주에서만 되살아난다.
    """
    key = (monday.isoformat(), asset_type)
    today = date.today()
    ent = _RADAR_POOL_CACHE.get(key)
    if ent is not None and (ent["day"] is None or ent["day"] == today):
        return ent["map"]
    m = _compute_radar_pool(monday, asset_type)
    settled = monday + timedelta(days=6 + 3) < today       # 그 주 발행이 다 끝났나
    _RADAR_POOL_CACHE[key] = {"day": (None if settled else today), "map": m}
    return m


def after_tax_amount(principal: int, gross_redeem: int) -> int:
    """세전 상환금 → 세후 상환금 (수익분에만 15.4% 과세)."""
    if gross_redeem is None or principal is None:
        return None
    profit = gross_redeem - principal
    if profit <= 0:
        return gross_redeem  # 손실이면 과세 없음
    return round(principal + profit * (1 - DIVIDEND_TAX_RATE))


def _add_months(d: date, months: int) -> date:
    """d에 months개월을 더한 날짜 (말일 보정)."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = d.day
    while True:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1


# 발행사별 ELS 안내 페이지 — 상품별 broker_url이 없을 때의 대체 링크.
# KOFIA는 '청약중' API 응답에만 상품별 URL을 실어 주므로 2026-07-18(수집 시작)
# 이전 상품은 개별 링크를 영영 구할 수 없다. 그 상품들에서 "증권사 ELS 목록"으로
# 보내 사용자가 직접 찾아볼 수 있게 한다. 2026-07-31 전 주소 응답 확인.
# 에스케이증권은 공개 ELS 페이지를 찾지 못해 제외(대체 링크 없이 버튼 숨김).
BROKER_SITE_URLS = {
    "한국투자증권": "https://www.truefriend.com/main/mall/openels/EdlsGuide.jsp?cmd=TF02ce000000",
    "신한투자증권": "https://www.shinhaninvest.com/siw/wealth-management/els/els-info/view.do",
    "키움증권": "https://www.kiwoom.com/wm/edl/es010/edlElsView?_reqAgent=form",
    "미래에셋증권": "https://securities.miraeasset.com/hks/hks4023/p02.do",
    "삼성증권": "https://www.samsungpop.com/ux/kor/finance/els/saleGoods/ingDetailTab1.do",
    "NH투자증권": "https://www.nhsec.com/finance/els/sellingProductList.action",
    "KB증권": "https://www.kbsec.com/go.able?linkcd=s010201000000",
    "메리츠증권": "https://home.imeritz.com/drvtlnkdprod/SbscCmptProd.do",
    "하나증권": "https://www.hanaw.com/main/finance/els/FP_050100_P1.cmd",
    "한화투자증권": "https://www.hanwhawm.com/main/finance/info/FI310_1.cmd",
    "유안타증권": "http://www.myasset.com/myasset/mall/els/elsDls/MA_0401001_P1.cmd",
    "비엔케이투자증권": "http://www.bnkfn.co.kr/els/elsdls2.jspx?cmd=list&subTp=A00",
    "DB증권": "https://www.dbsec.co.kr/product/els/pr_ElsDetail_viw.do",
    "교보증권": "https://www.iprovest.com/main.jsp",
    "신영증권": "https://www.shinyoung.com/?page=10070",
    "현대차증권": "https://www.hmsec.com/product/els/subscr_open/ol4706q_1.to?gdsTp=7",
    "유진투자증권": "https://www.eugenefn.com/ingo/iged/iged200r.do",
    "대신증권": "https://www.daishin.com/content/w/fnmall/els/elsDlsItmInfo.ds",
    "아이비케이투자증권": "https://www.ibks.com/fundproduct/els/elsInfo_popsearch.do",
}


class Product(models.Model):
    """수집된 ELS 상품 — 이력 축적용, 삭제하지 않음."""
    PRODUCT_TYPES = [("ELS", "ELS"), ("DLS", "DLS"), ("ELB", "ELB"), ("DLB", "DLB")]
    ASSET_TYPES = [("지수형", "지수형"), ("종목형", "종목형")]

    issuer = models.CharField("발행사", max_length=50)
    product_no = models.CharField("상품번호", max_length=30, blank=True)
    product_code = models.CharField(  # KOFIA 표준코드 (예: KR6MZ0006074) — 있으면 최우선 고유키
        "상품코드", max_length=20, blank=True, db_index=True
    )
    name = models.CharField("상품명", max_length=200, blank=True)
    product_type = models.CharField("상품유형", max_length=5, choices=PRODUCT_TYPES, default="ELS")

    yield_rate = models.FloatField("연수익률(%)", null=True, blank=True)
    max_loss = models.FloatField("최대손실률(%)", null=True, blank=True)

    ki = models.IntegerField("낙인 배리어(%)", null=True, blank=True)
    is_no_ki = models.BooleanField("노낙인 여부", default=False)
    barrier_first = models.IntegerField("1차 조기상환(%)", null=True, blank=True)
    barrier_last = models.IntegerField("마지막 조기상환(%)", null=True, blank=True)
    barriers_raw = models.JSONField("배리어 전체", null=True, blank=True)
    period_months = models.IntegerField("조기상환주기(개월)", null=True, blank=True)  # 이후(2차~) 조기상환 간격
    first_eval_months = models.IntegerField(  # 1차 조기상환까지 개월(비균등 대응). None이면 period_months와 동일(균등)
        "1차상환까지(개월)", null=True, blank=True
    )
    schedule_estimated = models.BooleanField(  # 주기 판정 실패로 임의 추정한 경우 True
        "스케줄 추정여부", default=False
    )

    asset_type = models.CharField("기초자산유형", max_length=5, choices=ASSET_TYPES, blank=True)
    assets_raw = models.CharField("기초자산", max_length=200, blank=True)

    # 주의: issue_date는 이름과 달리 실제로는 '청약종료일'이다.
    # KOFIA 응답에 발행일 필드가 없어 sub_end와 같은 값(index 17)을 넣어 왔고,
    # 기존 코드가 대량으로 이 필드에 의존하므로 이름은 바꾸지 않는다.
    # 진짜 발행일·최초기준가격평가일은 아래 real_issue_date / base_eval_date를 쓴다
    # (parse_prospectus_dates 커맨드가 간이투자설명서 PDF에서 채운다).
    issue_date = models.DateField("발행일", null=True, blank=True)
    expiry_date = models.DateField("만기일", null=True, blank=True)
    sub_start = models.DateField("청약시작일", null=True, blank=True)
    sub_end = models.DateField("청약마감일", null=True, blank=True)

    # 간이투자설명서에서 추출한 정확한 날짜 (없으면 None → issue_date 폴백)
    # 표시용 발행일은 아래 issued_on 프로퍼티를 쓸 것 (2026-07-31 날짜 라벨 통일)
    base_eval_date = models.DateField("최초기준가격평가일", null=True, blank=True)
    real_issue_date = models.DateField("실제 발행일", null=True, blank=True)
    # 실제 조기상환 평가일 목록 (SEIBro 확정값). 비어 있으면 '기준일+N개월' 근사로 폴백.
    # 근사는 실측과 -7~+3일까지 벌어진다(보유 153종 대조: 정확 일치 8종뿐) —
    # 평가일은 설명서에 개별 지정되는 값이라 공식으로 계산할 수 없다. (2026-08-03)
    eval_dates = models.JSONField("조기상환 평가일(확정)", null=True, blank=True)

    currency = models.CharField("통화", max_length=5, default="KRW")
    description = models.TextField("상품설명 원문", blank=True)
    # KOFIA 응답의 부가 링크 (증권사 상세페이지 · 간이투자설명서 PDF)
    broker_url = models.TextField("증권사 상품페이지 URL", blank=True, default="")
    prospectus_url = models.TextField("간이투자설명서 URL", blank=True, default="")
    collected_at = models.DateTimeField("수집일시", auto_now_add=True)

    # 수익률 모의실험(백테스트) 캐시 — simulate_products 배치가 채움
    loss_prob = models.FloatField("만기손실확률(%)", null=True, blank=True)
    sim_samples = models.IntegerField("시뮬 표본수", null=True, blank=True)
    sim_result = models.JSONField("시뮬 상세결과", null=True, blank=True)
    sim_updated = models.DateTimeField("시뮬 갱신일시", null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["issuer", "product_no", "sub_end"], name="uniq_product"
            )
        ]
        ordering = ["sub_end", "-yield_rate"]

    def __str__(self):
        return f"{self.issuer} {self.product_no}"

    @property
    def ki_display(self):
        if self.is_no_ki:
            return "노낙인"
        return str(self.ki) if self.ki is not None else "-"

    @property
    def d_day(self):
        if not self.sub_end:
            return None
        return (self.sub_end - date.today()).days

    @property
    def broker_site_url(self):
        """상품별 broker_url이 없을 때 쓸 발행사 ELS 페이지. 없으면 None."""
        if self.broker_url:
            return None
        return BROKER_SITE_URLS.get(self.issuer)

    @property
    def issued_on(self):
        """표시용 발행일 — 간이투자설명서 추출값 우선, 없으면 issue_date(≈청약종료일) 폴백."""
        return self.real_issue_date or self.issue_date

    @property
    def fixed_eval_dates(self):
        """확정 평가일 [date, ...]. 쓸 수 없으면 None (= 근사 폴백해야 한다는 뜻).

        Investment.schedule과 schedule_badge가 **반드시 이 하나만** 보도록 만든 헬퍼다.
        예전엔 schedule은 파싱까지 해 보고 실패하면 근사로 떨어졌는데 schedule_badge는
        개수만 세서 '확정'(배지 없음)을 냈다. 파싱이 깨지는 값
        (제로패딩 없는 "2025-7-10", 원소가 None, 리스트가 아닌 문자열)이 들어오면
        화면·엑셀에 근사 스케줄이 '확정'으로 찍혔다. (2026-08-04 검수)

        개수가 배리어와 맞아야 한다 — 부분 일치는 회차 정렬이 어긋나 신뢰할 수 없다.
        """
        raw = self.eval_dates
        # JSONField에 문자열이 그대로 들어간 경우 len()이 글자수라 개수 검사를 통과할 수 있다
        if not isinstance(raw, (list, tuple)) or not raw:
            return None
        if len(raw) != len(self.barriers_raw or []):
            return None
        try:
            return [date.fromisoformat(str(d)[:10]) for d in raw]
        except (ValueError, TypeError):
            return None

    @property
    def term_months(self):
        """상품기간(발행일→만기일) 총 개월수. 둘 중 하나라도 없으면 None.

        일 단위까지 반영해 반올림 — 예: 발행 7/31 → 만기 3년 뒤 8/3처럼
        주말·영업일 사정으로 며칠 넘친 경우 '3년 1개월'이 아니라 '3년'."""
        if not self.issue_date or not self.expiry_date:
            return None
        months = ((self.expiry_date.year - self.issue_date.year) * 12
                  + (self.expiry_date.month - self.issue_date.month))
        frac = (self.expiry_date.day - self.issue_date.day) / 30
        return int(months + frac + 0.5)

    @property
    def term_display(self):
        """상품기간 표시: 12개월 미만→'9개월', 12배수→'3년', 그 외→'2년6개월'. 없으면 '-'."""
        m = self.term_months
        if m is None:
            return "-"
        if m < 12:
            return f"{m}개월"
        years, months = divmod(m, 12)
        if months == 0:
            return f"{years}년"
        return f"{years}년{months}개월"

    @property
    def period_display(self):
        """조기상환 주기 표시. 균등→'6개월', 비균등(첫평가 다름)→'3+1개월'. 없으면 '-'."""
        if not self.period_months:
            return "-"
        first = self.first_eval_months
        if first and first != self.period_months:
            return f"{first}+{self.period_months}개월"
        return f"{self.period_months}개월"

    @property
    def confirm_date(self):
        """숙려대상자 청약 마감 = 일반 마감 - 2영업일 (주말 제외, 공휴일 미반영 근사치).

        고령(65세+)·부적합 투자자는 2영업일 숙려기간이 필요하므로
        일반 마감보다 2영업일 먼저 청약을 넣어야 한다.
        """
        if not self.sub_end:
            return None
        d = self.sub_end
        subtracted = 0
        while subtracted < 2:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                subtracted += 1
        return d

    @property
    def radar(self):
        """레이더 신호 — 상품이 속한 (청약 주차 × 유형) 그룹에서 상위 15위 안에
        든 경우에만 배지 정보를 반환. 그 외(게이트 탈락·순위 밖)는 None.

        반환: {tier, color, srank, group_n, score, points, mini_points, axes[4]}
        상세 산식은 모듈 상단 _compute_radar_pool 참고.
        """
        if not self.sub_end:
            return None
        if self.asset_type not in RADAR_V7_B0_MAX:
            return None
        monday = self.sub_end - timedelta(days=self.sub_end.weekday())
        r = _radar_pool(monday, self.asset_type).get(self.id)
        return r if r and r["tier"] else None

    @property
    def structure_label(self):
        """상품 구조 특이사항 라벨. 정상 스텝다운(배리어 있고 특이사항 없음)은 None.

        - 리자드: 배리어가 있어도 구조 특징이라 표시(스텝다운 + 리자드 조기상환)
        - 원금보장/하이파이브/기타: 배리어 없는 비스텝다운 상품의 구조 안내
        """
        import re
        d = self.description or ""
        # 리자드는 배리어 유무와 무관하게 표시 (텍스트가 주 신호, (Lxx) 마커는 보조)
        if re.search(r"Lizard|리자드|리쟈드", d, re.I) or re.search(r"\(L\d+\)", d):
            return "리자드"
        if self.barriers_raw:
            return None  # 정상 스텝다운 → 별도 라벨 없음
        if re.search(r"하이파이브|Hi-Five", d, re.I):
            return "하이파이브"
        if self.is_no_ki:
            return None   # 노낙인은 낙인 컬럼의 NoKI 배지로 이미 표시 → 유형 중복 제거
        return "기타"


def radar_tracks(monday=None, sunday=None, limit=5):
    """v7 유형별 타겟 신호 TOP 리스트 — {"지수형": [...], "종목형": [...]}.

    각 유형 = 3중 게이트 통과자(타겟 신호) 전원 중 수익률 상위 limit개.
    오늘 이후 마감 상품만 (지난 주 조회 시엔 그 주 전체).
    """
    today = date.today()
    if monday is None:
        monday = today - timedelta(days=today.weekday())
    if sunday is None:
        sunday = monday + timedelta(days=6)
    pool = Product.objects.filter(
        sub_end__gte=max(monday, today) if sunday >= today else monday,
        sub_end__lte=sunday, yield_rate__isnull=False)
    tracks = {"지수형": [], "종목형": []}
    for p in pool:
        r = p.radar
        if r and p.asset_type in tracks:
            tracks[p.asset_type].append(p)
    for t in tracks:
        tracks[t].sort(key=lambda p: -(p.yield_rate or 0))
        tracks[t] = tracks[t][:limit]
    return tracks


def radar_top5(monday=None, sunday=None):
    """(v6 호환용) 지수형 타겟 신호 TOP5. 신규 코드는 radar_tracks()를 쓸 것."""
    return radar_tracks(monday, sunday)["지수형"]


class Preset(models.Model):
    """조건 프리셋 — 계정별 소유(7/20 분리). user null=과거 공용(가족)."""
    ASSET_CHOICES = [("전체", "전체"), ("지수형", "지수형"), ("종목형", "종목형")]
    CURRENCY_CHOICES = [("전체", "전체"), ("KRW", "KRW"), ("USD", "USD")]

    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, null=True, blank=True,
                             related_name="presets")
    name = models.CharField("프리셋명", max_length=50)
    is_default = models.BooleanField("기본 프리셋", default=False)

    issuers = models.JSONField("발행사 목록", default=list, blank=True)  # 빈 리스트=전체
    ki_min = models.IntegerField("낙인 최소", null=True, blank=True)
    ki_max = models.IntegerField("낙인 최대", null=True, blank=True)
    include_no_ki = models.BooleanField("노낙인 포함", default=True)
    asset_type = models.CharField("자산유형", max_length=5, choices=ASSET_CHOICES, default="전체")
    yield_min = models.FloatField("최소 수익률(%)", null=True, blank=True)
    period_max = models.IntegerField("최대 주기(개월)", null=True, blank=True)
    currency = models.CharField("통화", max_length=5, choices=CURRENCY_CHOICES, default="전체")
    notify = models.BooleanField("텔레그램 알림", default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "id"]

    def __str__(self):
        return self.name

    def match_queryset(self, qs=None):
        """이 프리셋 조건에 맞는 Product queryset."""
        if qs is None:
            qs = Product.objects.all()
        if self.issuers:
            qs = qs.filter(issuer__in=self.issuers)
        if self.asset_type != "전체":
            qs = qs.filter(asset_type=self.asset_type)
        if self.currency != "전체":
            qs = qs.filter(currency=self.currency)
        if self.yield_min is not None:
            qs = qs.filter(yield_rate__gte=self.yield_min)
        if self.period_max is not None:
            qs = qs.filter(period_months__lte=self.period_max)

        ki_q = models.Q()
        has_ki_cond = False
        if self.ki_max is not None or self.ki_min is not None:
            cond = models.Q(is_no_ki=False)
            if self.ki_min is not None:
                cond &= models.Q(ki__gte=self.ki_min)
            if self.ki_max is not None:
                cond &= models.Q(ki__lte=self.ki_max)
            ki_q |= cond
            has_ki_cond = True
        if self.include_no_ki:
            ki_q |= models.Q(is_no_ki=True)
            has_ki_cond = True
        if has_ki_cond:
            qs = qs.filter(ki_q)
        return qs


class WatchItem(models.Model):
    """관심 목록 — 계정별 소유(7/20 분리)."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, null=True, blank=True,
                             related_name="watch_items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="watch")
    memo = models.CharField("메모", max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "product"], name="uniq_watch_user_product")
        ]


class Investment(models.Model):
    """실제 투자 기록."""
    STATUS_CHOICES = [
        ("보유중", "보유중"),
        ("조기상환", "조기상환"),
        ("만기상환", "만기상환"),
        ("낙인후상환", "낙인후상환"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="investments")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="investments")
    amount = models.BigIntegerField("투자금액(원)")
    invested_at = models.DateField("청약일")
    broker_account = models.CharField("증권사/계좌 메모", max_length=100, blank=True)
    status = models.CharField("상태", max_length=10, choices=STATUS_CHOICES, default="보유중")
    redeemed_at = models.DateField("상환일", null=True, blank=True)
    redeemed_amount = models.BigIntegerField("상환금액(원)", null=True, blank=True)
    memo = models.CharField("메모", max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.product} / {self.amount:,}원"

    @property
    def schedule(self):
        """조기상환 평가 스케줄 [{n, date, barrier, expected}] — 만기까지.

        평가일은 product.eval_dates(SEIBro 확정값)를 최우선으로 쓴다.
        없을 때만 '기준일 + N개월' 근사로 폴백 — 근사는 실측과 -7~+3일 벌어진다.

        비균등 스케줄 지원: first_eval_months(1차까지) + period_months(이후 간격).
        first_eval_months가 None이면 period_months와 동일(균등)해 기존과 결과 동일.
        회차 수는 배리어 개수로 확정한다(마지막 회차 = 만기).
        """
        p = self.product
        base = p.issue_date or self.invested_at
        if not base or not p.period_months:
            return []
        barriers = p.barriers_raw or []
        n_barriers = len(barriers)
        if n_barriers == 0:
            return []
        first = p.first_eval_months if p.first_eval_months else p.period_months
        interval = p.period_months
        # 확정 평가일 — 판정은 Product.fixed_eval_dates 한 곳에서만 한다.
        # 여기서 따로 파싱하면 schedule_badge와 결과가 갈린다(과거 사고).
        fixed = p.fixed_eval_dates
        rows = []
        for n in range(1, n_barriers + 1):
            months = first + (n - 1) * interval
            eval_date = fixed[n - 1] if fixed else _add_months(base, months)
            barrier = barriers[n - 1]
            expected = expected_after_tax = None
            if p.yield_rate is not None:
                expected = round(self.amount * (1 + p.yield_rate / 100 * months / 12))
                expected_after_tax = after_tax_amount(self.amount, expected)
            rows.append({
                "n": n, "date": eval_date, "barrier": barrier,
                "expected": expected, "expected_after_tax": expected_after_tax,
            })
        return rows

    @property
    def schedule_badge(self):
        """스케줄 신뢰도 배지 라벨. 확정이면 None.

        - 배리어/주기가 없어 스케줄을 못 만들면 '확인필요'
        - 실제 평가일(eval_dates)이 없어 '기준일+N개월'로 근사한 경우 '추정'
          (근사는 실측과 -7~+3일 벌어진다 — 2026-08-03 보유 153종 대조)
        - 주기를 판정 못해 임의 추정한 경우도 '추정'

        eval_dates가 있어도 날짜로 못 읽으면 schedule은 근사로 떨어진다.
        그래서 개수만 세지 말고 schedule과 같은 헬퍼를 봐야 한다. (2026-08-04)
        """
        p = self.product
        if not p.barriers_raw or not p.period_months:
            return "확인필요"
        if p.fixed_eval_dates:
            return None
        return "추정"

    @property
    def next_evaluation(self):
        """다음 평가 회차 (오늘 이후 첫 번째)."""
        today = date.today()
        for row in self.schedule:
            if row["date"] >= today:
                return row
        return None

    @property
    def realized_return_pct(self):
        """상환 완료 시 실현수익률(%)."""
        if self.redeemed_amount is None or not self.amount:
            return None
        return round((self.redeemed_amount - self.amount) / self.amount * 100, 2)

    @property
    def first_eval_after_tax(self):
        """1차 평가 시 세후 실수령액 (조기상환 가정)."""
        sched = self.schedule
        return sched[0]["expected_after_tax"] if sched else None

    @property
    def maturity_after_tax(self):
        """만기(최종 회차)까지 보유 시 세후 실수령액."""
        sched = self.schedule
        return sched[-1]["expected_after_tax"] if sched else None

    def _latest_verdict(self):
        """최신 판정. prefetch된 캐시를 그대로 쓴다(.first()는 매번 쿼리를 새로 낸다)."""
        return next(iter(self.verdicts.all()), None)  # ordering=-eval_date → 최신

    @property
    def redemption_pending(self):
        """직전 회차 배리어 충족 판정(check_redemptions 기록). 충족 시 verdict, 아니면 None."""
        v = self._latest_verdict()
        return v if (v and v.met) else None

    @property
    def missed_redemption(self):
        """직전 회차 배리어 미달 판정. 놓쳤으면 verdict, 아니면 None.

        met=None(시세 미확보로 판정 불가)은 놓친 것으로 보지 않는다 —
        확정되지 않은 건을 실패로 표시하지 않기 위함.
        """
        v = self._latest_verdict()
        return v if (v and v.met is False) else None

    @property
    def worst_ki_status(self):
        """워스트오브: 레벨이 가장 낮은(위험한) 기초자산 상태."""
        statuses = [s for s in self.ki_status.all() if s.level_pct is not None]
        if not statuses:
            return None
        return min(statuses, key=lambda s: s.level_pct)

    @property
    def ki_buffer(self):
        """워스트오브 기준 KI까지 남은 여유(%p). None이면 계산 불가."""
        worst = self.worst_ki_status
        return worst.buffer_to_ki if worst else None


class KnockInStatus(models.Model):
    """보유 투자별 기초자산 낙인 거리 (update_prices 배치가 갱신)."""
    investment = models.ForeignKey(Investment, on_delete=models.CASCADE, related_name="ki_status")
    asset_name = models.CharField(max_length=50)
    ticker = models.CharField(max_length=20, blank=True)
    ref_price = models.FloatField("발행일 기준가", null=True, blank=True)
    current_price = models.FloatField("현재가", null=True, blank=True)
    level_pct = models.FloatField("현재 레벨(%)", null=True, blank=True)  # 현재가/기준가×100
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["investment", "asset_name"], name="uniq_ki_status")
        ]
        ordering = ["level_pct"]

    @property
    def buffer_to_ki(self):
        """KI 배리어까지 남은 여유(%p). 낮을수록 위험. None이면 계산 불가."""
        ki = self.investment.product.ki
        if self.level_pct is None or ki is None:
            return None
        return round(self.level_pct - ki, 1)


class KnockInAlert(models.Model):
    """낙인 경보 발송 이력 — 같은 위험구간 중복 발송 방지."""
    investment = models.ForeignKey(Investment, on_delete=models.CASCADE)
    level_band = models.CharField(max_length=10)  # 위험구간 라벨 (예: '위험', '경고')
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["investment", "level_band"], name="uniq_ki_alert")
        ]


class ImportLog(models.Model):
    """엑셀 임포트 처리 이력 — 동일 파일 재처리 방지."""
    filename = models.CharField(max_length=200, unique=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    row_count = models.IntegerField(default=0)
    new_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self):
        return self.filename


class NotifiedMatch(models.Model):
    """프리셋 매칭 알림 발송 이력 — 중복 알림 방지."""
    preset = models.ForeignKey(Preset, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["preset", "product"], name="uniq_notified")
        ]


class RedemptionAlert(models.Model):
    """상환 평가일 알림 발송 이력 — 같은 회차 중복 발송 방지."""
    investment = models.ForeignKey(Investment, on_delete=models.CASCADE)
    round_no = models.IntegerField("회차")
    alert_type = models.CharField(max_length=5)  # D-7 / D-1
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["investment", "round_no", "alert_type"], name="uniq_redemption_alert"
            )
        ]


class PushSubscription(models.Model):
    """브라우저 웹 푸시 구독 — 사용자당 기기(브라우저)별 1행.

    endpoint는 브라우저 푸시 서비스가 발급한 고유 수신 주소.
    발송 시 410/404 응답(만료·해지)이 오면 core.push가 행을 삭제한다.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="push_subscriptions")
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=255)   # 메시지 암호화용 클라이언트 공개키
    auth = models.CharField(max_length=255)     # 인증 시크릿
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} · …{self.endpoint[-16:]}"


class RedemptionVerdict(models.Model):
    """지난 평가일 조기상환 판정 (check_redemptions 배치가 기록).

    평가일 종가 기준 워스트 레벨 >= 배리어 → met=True(상환 예정).
    실제 상환 처리(상태 변경)는 사용자가 증권사 확인 후 수동으로 한다.
    """
    investment = models.ForeignKey(
        Investment, on_delete=models.CASCADE, related_name="verdicts"
    )
    round_no = models.IntegerField("회차")
    eval_date = models.DateField("평가일")
    barrier = models.FloatField("배리어(%)", null=True, blank=True)
    worst_level = models.FloatField("워스트 레벨(%)", null=True, blank=True)
    worst_asset = models.CharField(max_length=50, blank=True)
    met = models.BooleanField("충족 여부", null=True)  # None=시세 미확보로 판정불가
    checked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["investment", "round_no"], name="uniq_redemption_verdict"
            )
        ]
        ordering = ["-eval_date"]


class RadarVerdict(models.Model):
    """레이더 신호 성과 검증 (verify_radar 배치가 기록).

    과거 주차에 배지(아주 강한/강한)를 받은 상품과 대조군(배지 없음)의
    실제 1차 조기상환 결과를 시세로 판정해 신호 적중률을 산출한다.
    판정 방식: 1차 평가일 종가 기준 워스트 레벨 >= 1차 배리어 → met=True.
    """
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="radar_verdicts"
    )
    tier = models.CharField("등급", max_length=20)  # 아주 강한 신호 / 강한 신호 / 없음
    week_monday = models.DateField("청약주차(월)")
    eval_date = models.DateField("1차 평가일")
    barrier = models.IntegerField("1차 배리어(%)", null=True, blank=True)
    worst_level = models.FloatField("워스트 레벨(%)", null=True, blank=True)
    met = models.BooleanField("충족 여부", null=True)  # None=평가 전 or 시세 미확보
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product"], name="uniq_radar_verdict")
        ]
        ordering = ["-eval_date"]


class HistoricalIssue(models.Model):
    """SEIBro(한국예탁결제원 증권정보포털) 발행종목조회 이력 — 자체 백테스팅 연구용.

    현재 서비스(Product)와는 별개 테이블. 배리어/쿠폰 등 상세 지급조건은 없고
    발행사·기초자산·발행/만기일·발행금액 수준의 요약 정보만 담는다.
    """
    isin = models.CharField("ISIN", max_length=20, unique=True, db_index=True)
    shotn_isin = models.CharField("단축코드", max_length=15, blank=True)
    name = models.CharField("종목명", max_length=100, blank=True)
    issuer = models.CharField("발행사", max_length=50, db_index=True)
    product_type = models.CharField("상품유형", max_length=5)  # ELS/ELB
    recu_whcd = models.CharField("발행구분", max_length=10, blank=True)  # 공모/사모
    currency_name = models.CharField("통화", max_length=10, blank=True)

    issue_date = models.DateField("발행일", null=True, blank=True, db_index=True)
    expiry_date = models.DateField("만기일", null=True, blank=True)

    basset_sort = models.CharField("기초자산유형", max_length=20, blank=True)
    basset_count = models.IntegerField("기초자산개수", null=True, blank=True)
    assets = models.JSONField("기초자산 목록", default=list, blank=True)  # [{name, isin, std_price}]

    issue_amount = models.BigIntegerField("발행금액", null=True, blank=True)

    # SEIBro 상세조회로 채우는 낙인/스텝다운 (표본조사, 공모 ELS만)
    ki = models.IntegerField("낙인배리어(%)", null=True, blank=True, db_index=True)
    stepdown_barriers = models.JSONField("스텝다운 배리어", null=True, blank=True)
    detail_fetched = models.BooleanField("상세조회완료", default=False, db_index=True)

    # SEIBro 상세 전수수집(collect_seibro_detail)으로 채우는 상환스케줄·수익률
    eval_dates = models.JSONField("중간평가일 목록", null=True, blank=True)  # ["2019-06-28", ...]
    step_yields = models.JSONField("회차별 누적수익률(%)", null=True, blank=True)  # [3.5, 7.0, ...]
    yield_rate = models.FloatField("연환산 수익률(%)", null=True, blank=True)
    period_months = models.IntegerField("평가주기(개월, 중앙값)", null=True, blank=True)
    first_eval_months = models.IntegerField("1차 평가까지(개월)", null=True, blank=True)
    parse_error = models.CharField("상세 결측사유", max_length=60, blank=True)

    # ── 과거 레이더 재현·성과검증 파이프라인 (simulate_historical / verify_historical) ──
    # 백테스트는 배지 후보에만 필요 → 값싼 사전 게이트를 통과한 건만 채워진다.
    sim_loss_prob = models.FloatField("시뮬 손실확률(%)", null=True, blank=True)
    sim_early_1y = models.FloatField("시뮬 1년내 조기상환(%)", null=True, blank=True)
    sim_skip = models.CharField("시뮬 불가 사유", max_length=40, blank=True)
    # 발행일 주차×유형 그룹 안에서 재현한 당시 레이더 결과
    radar_tier = models.CharField("레이더 등급", max_length=12, blank=True)  # 빈값=배지없음
    radar_rank = models.IntegerField("레이더 점수순위", null=True, blank=True)
    # 1차 평가일 실제 결과 (null=미판정/평가일 미도래/시세 미확보)
    verdict_met = models.BooleanField("1차 조기상환 충족", null=True)
    verdict_level = models.FloatField("1차 평가일 워스트 레벨(%)", null=True, blank=True)

    collected_at = models.DateTimeField("수집일시", auto_now_add=True)

    class Meta:
        ordering = ["-issue_date"]

    def __str__(self):
        return f"{self.issuer} {self.name} ({self.isin})"


class HistoricalRedemption(models.Model):
    """SEIBro 상환종목조회 이력 — 실제 조기/만기상환 결과(연구용, HistoricalIssue와 별개).

    수익률/손실금액 필드는 SEIBro 대량조회 API에 없음(상환유형·시점만 제공).
    """
    isin = models.CharField("ISIN", max_length=20, db_index=True)
    name = models.CharField("종목명", max_length=100, blank=True)
    issuer = models.CharField("발행사", max_length=50, db_index=True)
    product_type = models.CharField("상품유형", max_length=5, blank=True)
    recu_whcd = models.CharField("발행구분", max_length=10, blank=True)

    issue_date = models.DateField("발행일", null=True, blank=True)
    expiry_date = models.DateField("만기일", null=True, blank=True)
    redemption_date = models.DateField("상환일", null=True, blank=True, db_index=True)
    exercise_type = models.CharField("상환유형", max_length=10, blank=True)  # 조기상환/만기상환

    planned_term_months = models.IntegerField("예정만기(개월)", null=True, blank=True)
    held_months = models.IntegerField("실제보유(개월)", null=True, blank=True)

    asset_type_name = models.CharField("기초자산유형", max_length=20, blank=True)
    basset_count = models.IntegerField("기초자산개수", null=True, blank=True)
    assets = models.JSONField("기초자산명 목록", default=list, blank=True)

    collected_at = models.DateTimeField("수집일시", auto_now_add=True)

    class Meta:
        ordering = ["-redemption_date"]
        constraints = [
            models.UniqueConstraint(fields=["isin", "redemption_date"], name="uniq_redemption_isin_date")
        ]

    def __str__(self):
        return f"{self.issuer} {self.name} ({self.isin}) {self.exercise_type}"


class HistoricalYieldStat(models.Model):
    """SEIBro '주요기초자산별상환수익률'(공식 집계) — 연도×기초자산조합별 실현수익률·손실 통계.

    개별 종목이 아니라 SEIBro가 직접 집계한 값이라 실제 시장 실현수익률로 신뢰할 수 있다.
    """
    year = models.IntegerField("연도", db_index=True)
    basset_sort = models.CharField("기초자산유형", max_length=20, blank=True)
    assets = models.JSONField("기초자산 조합", default=list, blank=True)  # 이름 리스트

    count = models.IntegerField("상환건수(CNT_HAP)", null=True, blank=True)
    redemption_amount = models.BigIntegerField("상환금액합계(REDAMT_VAL_HAP)", null=True, blank=True)
    margin_rate = models.FloatField("실현수익률(%, RED_MARGIN_RATE)", null=True, blank=True)
    planned_months = models.IntegerField("평균예정만기(개월)", null=True, blank=True)
    held_months = models.IntegerField("평균실제보유(개월)", null=True, blank=True)

    minus_count = models.IntegerField("손실건수(MINUS_CNT)", null=True, blank=True)
    minus_amount = models.BigIntegerField("손실금액(MINUS_RED_AMT)", null=True, blank=True)

    collected_at = models.DateTimeField("수집일시", auto_now_add=True)

    class Meta:
        ordering = ["-year", "-count"]

    def __str__(self):
        return f"{self.year} {'/'.join(self.assets[:2])} {self.margin_rate}%"


class ThreadsReply(models.Model):
    """스레드(@els_rader) 댓글 수집·응답 이력.

    자동 응답은 A버킷(인사·서비스 안내)에만, 그것도 질문 내용을 전혀 반영하지
    않는 정형 문구로만 나간다. 개별 사정이 반영된 답변은 미등록 투자자문업이
    될 수 있어서다(대법원 2018도4413). 분류 규칙은 core/threads_replies.py.

    bucket_reason을 남기는 이유
      "이 댓글을 왜 A로 봤는가"를 사람이 되짚을 수 있어야 자동 응답 범위를
      사후에 검증할 수 있다. 규칙을 고친 뒤 과거 분류가 어떻게 달라지는지도
      이 값으로 비교한다.

    replied_at을 따로 두는 이유
      created_at은 수집 시각이라 발송 시각과 다르다. 시간당 발송 상한과
      최근 문구 중복 검사는 발송 시각 기준이어야 한다.
    """
    BUCKETS = [("A", "A 정형응답 가능"), ("B", "B 사람 답변"), ("C", "C 개별판단 금지")]
    STATUSES = [("new", "수집됨"), ("replied", "자동응답 완료"),
                ("skipped", "발송 안 함"), ("notified", "알림 발송")]

    reply_id = models.CharField("댓글 id", max_length=64, unique=True)
    post_id = models.CharField("원 게시물 id", max_length=64, db_index=True)
    username = models.CharField("작성자", max_length=100, blank=True)
    text = models.TextField("본문", blank=True)
    timestamp = models.DateTimeField("댓글 작성 시각", null=True, blank=True)
    permalink = models.URLField("댓글 링크", max_length=300, blank=True)
    bucket = models.CharField("버킷", max_length=1, choices=BUCKETS, db_index=True)
    bucket_reason = models.CharField("분류 근거", max_length=120, blank=True)
    status = models.CharField("상태", max_length=10, choices=STATUSES,
                              default="new", db_index=True)
    replied_text = models.TextField("발송한 문구", blank=True)
    replied_at = models.DateTimeField("발송 시각", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["status", "bucket"])]

    def __str__(self):
        return f"[{self.bucket}] @{self.username}: {self.text[:30]}"


class PageView(models.Model):
    """접속 로그(자체 분석용) — 개인 식별 불가능한 일별 해시만 저장, 180일 보관."""
    date = models.DateField(db_index=True)
    path = models.CharField(max_length=120)
    ref = models.CharField("유입 도메인", max_length=120, blank=True)
    visitor = models.CharField("방문자 해시(ip+ua+일자)", max_length=16)
    is_auth = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["date", "path"])]

    def __str__(self):
        return f"{self.date} {self.path}"


# ══════════════════════════════════════════════════════════════════
# 기초자산 일별 시세 원본 (PriceBar)
# ══════════════════════════════════════════════════════════════════
# 왜 원본을 통째로 두는가 —
#   "10년 추이 / 누적수익률 / 연간수익률 / 최대낙폭"처럼 **미리 정한 지표**만
#   함수로 만들어 두면 "2020년 3월에 가장 많이 빠진 자산은?" 같은 예상 못 한
#   질문에 답할 수 없다. 일별 OHLCV를 요약 없이 갖고 있다가 질문이 올 때마다
#   그 위에서 계산한다. 이 모델은 그 **데이터 계층**이고, 지표 계산은 별도다.
#
# 왜 조정·미조정을 둘 다 저장하는가 — **이 모델의 핵심 설계 결정**
#   yfinance는 auto_adjust 인자 하나로 둘 중 하나만 준다.
#     · auto_adjust=True  → Close 열이 '배당·분할 소급 조정' 종가
#     · auto_adjust=False → Close(미조정) + Adj Close(조정)가 **함께** 온다
#   용도가 정반대다:
#     · 수익률·낙폭  → 조정종가. 미조정으로 계산하면 배당락 하락이 전부
#                      손실로 잡혀 장기 수익률이 과소평가된다.
#     · 낙인·기준가  → 미조정 종가. 조정종가는 증권사가 고시하는 실제 종가와
#                      어긋나고, 배당주는 시간이 지날수록 과거 가격이 계속
#                      낮아져 오차가 누적된다.
#                      (실측: 브로드컴 2026-04-23 조정 419.28 vs 실제 419.94)
#   하나만 저장하면 나중에 다른 쪽을 **복원할 수 없다** — 조정계수를 따로
#   보관하지 않는 한 역산이 불가능하기 때문. auto_adjust=False 한 번 호출로
#   둘 다 얻으므로 네트워크 비용도 늘지 않는다. (2026-08-06 실측 확인)
#
# 용량 — 1티커 10년 ≈ 2,500행. 42티커면 약 10만 행(≈11MB),
#   355티커면 약 89만 행(≈92MB). SQLite REAL 8바이트 × 6열 + 인덱스 기준.

class PriceBar(models.Model):
    """기초자산 일별 시세 한 줄 (요약·가공 없이 받은 그대로).

    ⚠ 이 테이블에는 **파생값을 넣지 않는다.** 수익률·낙폭·이동평균 따위는
      질의 시점에 계산한다. 한 번 요약해 저장하면 그 요약이 답할 수 있는
      질문만 답하게 되고, 그게 바로 이 설계가 피하려는 것이다.
    """

    SOURCE_PRIMARY = "원본"
    SOURCE_FILLED = "보완"
    SOURCES = [(SOURCE_PRIMARY, "원본 계열 그대로"),
               (SOURCE_FILLED, "대체 계열로 메움")]

    ticker = models.CharField("티커", max_length=24)
    date = models.DateField("거래일")

    open = models.FloatField("시가", null=True, blank=True)
    high = models.FloatField("고가", null=True, blank=True)
    low = models.FloatField("저가", null=True, blank=True)
    # ⚠ close = 미조정(증권사 고시 종가) / adj_close = 배당·분할 조정.
    #   둘을 바꿔 쓰면 낙인 판정이나 장기 수익률 중 하나가 반드시 틀린다.
    close = models.FloatField("종가(미조정)", null=True, blank=True)
    adj_close = models.FloatField("조정종가", null=True, blank=True)
    volume = models.BigIntegerField("거래량", null=True, blank=True)

    # ── 출처 추적 ────────────────────────────────────────────────
    # 코스피200처럼 주 계열(^KS200)에 결측 구간이 있어 다른 계열(069500.KS)로
    # 메운 행을 데이터에서 구분할 수 있어야 한다. 안 그러면 "이 값이 실제
    # 지수 종가인가, 우리가 환산해 넣은 값인가"를 나중에 알 길이 없다.
    source = models.CharField("출처", max_length=8, choices=SOURCES,
                              default=SOURCE_PRIMARY)
    source_ticker = models.CharField("보완 원본 티커", max_length=24, blank=True)
    # 보완 시 스케일 계수 — 지수(1,080p)와 ETF(41,000원)는 ~100배 스케일이
    # 달라 그대로 이어붙이면 계열이 망가진다. 직전 공통 거래일의
    # (주계열 종가 ÷ 보조계열 종가)를 곱해 레벨을 맞춘다. 비율 접합이라
    # **수익률 계열은 보조계열 것이 그대로 보존**된다 (누적수익률·낙폭이 목적).
    scale = models.FloatField("보완 스케일 계수", null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # 같은 티커·날짜가 두 줄 생기면 모든 집계가 조용히 두 배가 된다.
            models.UniqueConstraint(fields=["ticker", "date"], name="uniq_pricebar"),
        ]
        # (ticker, date) 복합 인덱스는 위 유니크 제약이 만들어 준다 →
        # "티커 하나의 기간 구간" 질의는 이미 인덱스로 커버된다.
        # 아래는 그 반대 방향("특정 날짜에 전 자산") 질의용.
        indexes = [models.Index(fields=["date"], name="pricebar_date_idx")]
        ordering = ["ticker", "date"]
        verbose_name = "기초자산 일별 시세"

    def __str__(self):
        return f"{self.ticker} {self.date} {self.close}"

    # ── 조회 헬퍼 ────────────────────────────────────────────────
    # ⚠ 용도별로 함수를 **일부러 둘로 나눴다.** 인자 하나로 받는 형태
    #   (closes(ticker, adjusted=True))로 만들면 호출부가 기본값을 그대로
    #   쓰다가 용도에 안 맞는 계열을 집는다 — 지금 yfinance 호출부 5곳이
    #   auto_adjust를 제각각 쓰게 된 경위가 정확히 그것이다.
    #   이름만 보고도 어느 쪽인지 알게 만든다.

    @classmethod
    def _closes(cls, field, ticker, start=None, end=None):
        qs = cls.objects.filter(ticker=ticker, **{f"{field}__isnull": False})
        if start:
            qs = qs.filter(date__gte=start)
        if end:
            qs = qs.filter(date__lte=end)
        return list(qs.order_by("date").values_list("date", field))

    @classmethod
    def closes_for_return(cls, ticker, start=None, end=None):
        """수익률·최대낙폭용 **조정종가** [(date, adj_close), ...].

        배당·분할이 소급 반영돼 있어 장기 총수익(total return) 계산에 맞다.
        낙인·기준가에는 쓰지 말 것 — 증권사 고시 종가와 어긋난다.
        """
        return cls._closes("adj_close", ticker, start, end)

    @classmethod
    def closes_for_barrier(cls, ticker, start=None, end=None):
        """낙인·기준가용 **미조정 종가** [(date, close), ...].

        증권사가 고시하는 실제 종가와 같은 계열이다.
        누적수익률·낙폭에는 쓰지 말 것 — 배당락이 손실로 잡힌다.
        """
        return cls._closes("close", ticker, start, end)

    @classmethod
    def last_dates(cls, tickers=None, primary_only=False):
        """{티커: 마지막 저장 거래일} — 증분 적재가 어디부터 이어받을지 결정.

        primary_only=True면 **원본 행만** 본다. 증분 갱신이 이 값을 써야 한다 —
        보완 행까지 포함해 최신일을 잡으면, 한 번 메운 구간은 원본 시세가
        나중에 복구돼도 영영 다시 조회하지 않아 보완값이 눌러앉는다.
        """
        from django.db.models import Max
        qs = cls.objects.all()
        if primary_only:
            qs = qs.filter(source=cls.SOURCE_PRIMARY)
        if tickers is not None:
            qs = qs.filter(ticker__in=list(tickers))
        rows = qs.values("ticker").annotate(last=Max("date"))
        return {r["ticker"]: r["last"] for r in rows}
