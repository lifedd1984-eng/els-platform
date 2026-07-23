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
# 등급 컷 (점수 순위 기준)
RADAR_TOP_STRONG = 5    # 1 ~ 이 순위 = 아주 강한 신호
RADAR_TOP_WEAK = 15     # (상위)+1 ~ 이 순위 = 강한 신호, 그 외 배지 없음
RADAR_COLORS = {"아주 강한 신호": "#1B64DA", "강한 신호": "#3182F6"}
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
        {"name": "안전성", "val": f"손실확률 {p.loss_prob:g}%",
         "score": ax["safe"], "stars": _stars_loss(p.loss_prob or 0)},
        {"name": "조기상환", "val": f"1년내 {early:g}%" if early else "-",
         "score": ax["early"], "stars": _stars_early(early)},
        {"name": "방어력",
         "val": "노낙인 (배리어 이하 손실)" if p.is_no_ki else
                (f"낙인 {p.ki}% ({100 - p.ki}% 하락까지 수익상환)" if p.ki is not None else "-"),
         "score": ax["defense"], "stars": _stars_ki(p)},
    ]


def _compute_radar_pool(monday, asset_type):
    """(주차, 유형) 그룹의 {product_id: radar_result} 계산. 배지 대상만 담는다."""
    sunday = monday + timedelta(days=6)
    group = list(Product.objects.filter(
        sub_end__gte=monday, sub_end__lte=sunday,
        asset_type=asset_type, loss_prob__isnull=False))
    if not group:
        return {}
    ki_excl = RADAR_KI_EXCL[asset_type]
    last_max = RADAR_LAST_MAX[asset_type]

    # ── 게이트 ①~⑤ ──
    survivors = [p for p in group if (
        (not p.is_no_ki and p.ki is not None)          # ① 낙인 있음
        and p.ki < ki_excl                              # ② 낙인 < 임계
        and (_radar_early(p) or 0) >= RADAR_EARLY_MIN   # ③ 1년내 조기상환
        and (p.loss_prob or 0) < RADAR_LOSS_MAX         # ④ 손실확률
        and p.barrier_last is not None
        and p.barrier_last <= last_max                  # ⑤ 막차 배리어
    )]
    # ⑥ 수익률 상위 50% (게이트 통과분 기준)
    if survivors:
        ys = sorted(p.yield_rate or 0 for p in survivors)
        y_med = ys[len(ys) // 2]
        survivors = [p for p in survivors if (p.yield_rate or 0) >= y_med]

    # 점수 = 수익률 − 낙인 + 상수, 내림차순 순위 → 등급
    ranked = sorted(survivors, key=lambda p: (p.yield_rate or 0) - p.ki + RADAR_SCORE_SHIFT,
                    reverse=True)
    eligible_n = len(ranked)

    # 수익성 백분위(그룹 전체 기준) — 수익성 축만 상대평가, 나머지는 절대 컷
    yield_col = [p.yield_rate or 0 for p in group]

    result = {}
    for i, p in enumerate(ranked):
        if i < RADAR_TOP_STRONG:
            tier = "아주 강한 신호"
        elif i < RADAR_TOP_WEAK:
            tier = "강한 신호"
        else:
            break   # 16위부터는 배지 없음 → 저장 안 함
        y_pct = round(_radar_pct(yield_col, p.yield_rate or 0))
        ax = _radar_display_ax(p, y_pct)
        result[p.id] = {
            "tier": tier, "color": RADAR_COLORS[tier],
            "srank": i + 1, "group_n": eligible_n,
            "score": round((p.yield_rate or 0) - p.ki + RADAR_SCORE_SHIFT, 1),
            "reasons": [],
            "points": _radar_points(ax), "mini_points": _radar_mini_points(ax),
            "axes": _radar_axes(p, ax),
        }
    return result


def _radar_pool(monday, asset_type):
    """(주차, 유형) 풀을 캐시와 함께 반환. 과거 주차는 영구 캐시,
    이번 주(진행 중)는 하루 단위로 갱신."""
    key = (monday.isoformat(), asset_type)
    today = date.today()
    cur_monday = today - timedelta(days=today.weekday())
    ent = _RADAR_POOL_CACHE.get(key)
    if ent is not None and (ent["day"] is None or ent["day"] == today):
        return ent["map"]
    m = _compute_radar_pool(monday, asset_type)
    _RADAR_POOL_CACHE[key] = {"day": (today if monday >= cur_monday else None), "map": m}
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

    ki = models.IntegerField("KI배리어(%)", null=True, blank=True)
    is_no_ki = models.BooleanField("NoKI 여부", default=False)
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

    issue_date = models.DateField("발행일", null=True, blank=True)
    expiry_date = models.DateField("만기일", null=True, blank=True)
    sub_start = models.DateField("청약시작일", null=True, blank=True)
    sub_end = models.DateField("청약마감일", null=True, blank=True)

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
            return "NoKI"
        return str(self.ki) if self.ki is not None else "-"

    @property
    def d_day(self):
        if not self.sub_end:
            return None
        return (self.sub_end - date.today()).days

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
        if self.loss_prob is None or not self.sub_end:
            return None
        if self.asset_type not in RADAR_KI_EXCL:
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


def radar_top5(monday=None, sunday=None):
    """이번주 레이더 TOP5 상품 리스트.

    선정 기준(weekly 뷰·주간요약 공용): 오늘 이후 마감 ~ 해당 주 일요일 사이,
    레이더 '아주 강한 신호' & 손실확률 0% & 1년내 조기상환 ≥ 90% → 수익률 내림차순 5개.
    monday/sunday 미지정 시 이번 주 기준. (overlap 등 표시용 계산은 호출부 책임)
    """
    today = date.today()
    if monday is None:
        monday = today - timedelta(days=today.weekday())
    if sunday is None:
        sunday = monday + timedelta(days=6)
    pool = Product.objects.filter(
        sub_end__gte=max(monday, today), sub_end__lte=sunday,
        barriers_raw__isnull=False, yield_rate__isnull=False, loss_prob__isnull=False,
    )
    cand = []
    for p in pool:
        r = p.radar
        if not (r and r["tier"] == "아주 강한 신호"):
            continue
        if (p.loss_prob or 0) != 0 or _radar_early(p) < 90:
            continue
        cand.append(p)
    cand.sort(key=lambda p: -(p.yield_rate or 0))
    return cand[:5]


class Preset(models.Model):
    """조건 프리셋 — 계정별 소유(7/20 분리). user null=과거 공용(가족)."""
    ASSET_CHOICES = [("전체", "전체"), ("지수형", "지수형"), ("종목형", "종목형")]
    CURRENCY_CHOICES = [("전체", "전체"), ("KRW", "KRW"), ("USD", "USD")]

    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, null=True, blank=True,
                             related_name="presets")
    name = models.CharField("프리셋명", max_length=50)
    is_default = models.BooleanField("기본 프리셋", default=False)

    issuers = models.JSONField("발행사 목록", default=list, blank=True)  # 빈 리스트=전체
    ki_min = models.IntegerField("KI 최소", null=True, blank=True)
    ki_max = models.IntegerField("KI 최대", null=True, blank=True)
    include_no_ki = models.BooleanField("NoKI 포함", default=True)
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
        rows = []
        for n in range(1, n_barriers + 1):
            months = first + (n - 1) * interval
            eval_date = _add_months(base, months)
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
        - 주기를 판정 못해 임의 추정한 경우 '추정'
        - 텍스트 주기/규칙1/규칙2로 확정된 경우 None(배지 없음)
        """
        p = self.product
        if not p.barriers_raw or not p.period_months:
            return "확인필요"
        if p.schedule_estimated:
            return "추정"
        return None

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

    @property
    def redemption_pending(self):
        """직전 회차 배리어 충족 판정(check_redemptions 기록). 충족 시 verdict, 아니면 None."""
        v = self.verdicts.first()  # ordering=-eval_date → 최신
        return v if (v and v.met) else None

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
