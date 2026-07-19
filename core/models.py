from datetime import date, timedelta

from django.conf import settings
from django.db import models

# ELS/ELB 수익은 배당소득으로 과세: 소득세 14% + 지방소득세 1.4% = 15.4%
# (원금 제외, 수익분에만 부과)
DIVIDEND_TAX_RATE = 0.154

# 금융소득 종합과세 기준 (연 2천만원 초과 시 다른 소득과 합산)
FINANCIAL_INCOME_THRESHOLD = 20_000_000


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
        """상품기간(발행일→만기일) 총 개월수. 둘 중 하나라도 없으면 None."""
        if not self.issue_date or not self.expiry_date:
            return None
        return ((self.expiry_date.year - self.issue_date.year) * 12
                + (self.expiry_date.month - self.issue_date.month))

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
    def structure_label(self):
        """상품 구조 라벨. 스텝다운(배리어 있음)은 None(라벨 불필요).

        배리어가 없는 비(非)스텝다운 상품이 왜 배리어·주기·KI 칸이 비는지
        화면에서 바로 알 수 있도록 구조를 표시한다.
        """
        if self.barriers_raw:
            return None  # 정상 스텝다운 → 별도 라벨 없음
        import re
        d = self.description or ""
        if re.search(r"원금지급|원금보장", d) or self.product_type == "ELB":
            return "원금보장"
        if re.search(r"digital|디지털", d, re.I):
            return "디지털"
        if re.search(r"하이파이브|Hi-Five", d, re.I):
            return "하이파이브"
        if re.search(r"국고채|국채|KTB|금리|환율|USD/KRW|DLS", d, re.I):
            return "DLS"
        if self.is_no_ki:
            return "노낙인"
        return "기타"


class Preset(models.Model):
    """조건 프리셋 — 기본 3종 시드, 전부 수정/삭제 가능."""
    ASSET_CHOICES = [("전체", "전체"), ("지수형", "지수형"), ("종목형", "종목형")]
    CURRENCY_CHOICES = [("전체", "전체"), ("KRW", "KRW"), ("USD", "USD")]

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
    """관심 목록 — 투자 전 후보."""
    product = models.OneToOneField(Product, on_delete=models.CASCADE, related_name="watch")
    memo = models.CharField("메모", max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


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
