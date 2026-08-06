import calendar as pycalendar
import logging
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import LoginView
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import (
    ImportLog, Investment, Preset, Product, WatchItem, attach_peak_ratios,
    peak_ratios,
)

logger = logging.getLogger(__name__)

# 가족(운영진) 전용 — 공유 데이터(관심·프리셋·업로드)는 staff 계정만
family_required = user_passes_test(lambda u: u.is_active and u.is_staff, login_url="/accounts/login/")

# 운영자(superuser) 전용 — 회원 관리
admin_required = user_passes_test(lambda u: u.is_active and u.is_superuser, login_url="/accounts/login/")


def _scope(qs, user):
    """프리셋/관심 소유 범위 — 계정별 완전 분리.

    (구버전은 staff끼리 user=None '공용'을 공유했으나, 공개 서비스로 전환하면서
     운영자 계정끼리 관심목록·프리셋이 섞이는 문제가 있어 개인화로 통일했다.)
    """
    if not user.is_authenticated:
        return qs.none()
    return qs.filter(user=user)


from django import forms as _forms


class SignUpForm(UserCreationForm):
    """가입 폼 — 이메일 필수 (아이디/비밀번호 찾기에 사용)."""
    email = _forms.EmailField(required=True)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user


class RememberLoginView(LoginView):
    """로그인 유지 체크 시 30일, 미체크 시 브라우저 종료로 세션 만료."""
    template_name = "core/login.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.POST.get("remember"):
            self.request.session.set_expiry(60 * 60 * 24 * 30)  # 30일
        else:
            self.request.session.set_expiry(0)  # 브라우저 닫으면 로그아웃
        return response


def signup(request):
    """회원가입 — 일반 회원은 공개 화면 + 본인 포트폴리오만 사용."""
    if request.user.is_authenticated:
        return redirect("weekly")
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            auth_login(request, user)
            messages.success(request, "가입을 환영합니다! 포트폴리오에서 투자를 등록해보세요.")
            return redirect("weekly")
    else:
        form = SignUpForm()
    return render(request, "core/signup.html", {"form": form})


def find_id(request):
    """아이디 찾기 — 가입 이메일 입력 시 마스킹된 아이디 표시."""
    from django.contrib.auth import get_user_model
    found = None
    searched = False
    if request.method == "POST":
        searched = True
        email = request.POST.get("email", "").strip()
        if email:
            names = list(get_user_model().objects.filter(email__iexact=email)
                         .values_list("username", flat=True))
            found = [n[:2] + "*" * max(len(n) - 4, 1) + n[-2:] if len(n) > 4
                     else n[0] + "*" * (len(n) - 1) for n in names]
    return render(request, "core/find_id.html", {"found": found, "searched": searched})


def _week_range(offset: int = 0):
    """offset주 뒤의 (월요일, 일요일)."""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=6)


# ── 주간 청약 (메인) ──────────────────────────────
WEEKLY_FILTER_PARAMS = ["asset", "ki_max", "yield_min", "currency",
                        "no_ki", "issuer", "preset", "sort", "dir"]

# 시장 국면 카드에 표시할 대표 지수
REGIME_INDEXES = [("KOSPI200", "KOSPI200 Index"), ("S&P500", "S&P500 Index"),
                  ("Nikkei225", "Nikkei225 Index"), ("Euro Stoxx 50", "Euro Stoxx 50 Index")]
# 등급(양호/주의/과열) 라벨은 붙이지 않는다 — 분기 발행 기준으로 만든 눈금과
# 주간 청약 기준 통과율은 모수가 달라 단정할 근거가 없다. 사실만 전달한다.
# (2026-08-03 태훈님: "굳이 평가를 우리가 할 필요는 없다")


def _market_regime(monday, sunday):
    """시장 국면 — 이번 주 조건 통과율 + 대표 지수 위치.

    통과율은 시세가 필요 없는 두 게이트(낙인 p30 · 1차 배리어)로만 계산한다.
    10년 분기 검증에서 통과율 ↔ 이후 1년 주식수익률 상관 +0.52 — 즉 조건이
    나쁜 시기는 시장도 과열된 시기였다. '갈아타라'가 아니라 '지금이 어디인가'를
    읽는 용도. (regime_signal.py 검증, 2026-08-02)
    """
    from core import market as _m
    from core.models import (
        RADAR_V7_B0_MAX, _peak_fetch_days, _peak_from_series, v7_ki_cut,
    )

    group = list(Product.objects.filter(sub_end__gte=monday, sub_end__lte=sunday))
    if not group:
        return None
    n_all = n_pass = 0
    for p in group:
        if p.asset_type not in RADAR_V7_B0_MAX:
            continue
        n_all += 1
        cut = v7_ki_cut(p.asset_type)
        if (not p.is_no_ki and p.ki is not None and cut is not None and p.ki < cut
                and p.barrier_first is not None
                and p.barrier_first <= RADAR_V7_B0_MAX[p.asset_type]):
            n_pass += 1
    if not n_all:
        return None
    rate = n_pass / n_all * 100

    def _gauge(label, name):
        # 화면 표기가 "52주 고점 대비 / 직전 1년 대비"이므로 창도 캘린더 52주로 자른다.
        # (예전엔 fetch_history(days=370)의 종가 전체를 썼는데 370은 거래일 수로
        #  해석돼 실제로는 1.5년 창이었다 — 실측 557캘린더일. 2026-08-05)
        tk = _m.resolve_ticker(name)
        hist = _m.fetch_history(tk, days=_peak_fetch_days(date.today())) if tk else None
        if not hist:
            return None
        peak, ret1y = _peak_from_series(hist, hist[-1][0])
        if peak is None or ret1y is None:
            return None      # 표에 두 값이 다 있어야 한 행이 성립한다
        return {"name": label, "peak": round(peak, 1), "ret1y": round(ret1y, 1)}

    idx = [g for g in (_gauge(lb, nm) for lb, nm in REGIME_INDEXES) if g]

    # 종목형 기초자산 — 이번 주 종목형 상품에 가장 많이 등장하는 5개
    freq = {}
    for p in group:
        if p.asset_type != "종목형":
            continue
        for a in _m.split_assets(p.assets_raw or ""):
            a = a.strip()
            if a:
                freq[a] = freq.get(a, 0) + 1
    # 지수는 위 표에 이미 있으므로 제외 (혼합형 상품 때문에 섞여 들어온다)
    idx_tickers = {_m.resolve_ticker(nm) for _, nm in REGIME_INDEXES}
    stocks = []
    for name, cnt in sorted(freq.items(), key=lambda x: -x[1]):
        if _m.resolve_ticker(name) in idx_tickers:
            continue
        g = _gauge(_m.shorten_asset_display(name), name)
        if g:
            g["cnt"] = cnt
            stocks.append(g)
        if len(stocks) >= 5:
            break

    # 통과 조건을 화면에 그대로 노출 (컷은 매년 자동 산출되므로 하드코딩 금지)
    cond = " · ".join(
        f"{t} 낙인 {v7_ki_cut(t)} 미만 · 1차 조기상환 {RADAR_V7_B0_MAX[t]} 이하"
        for t in ("지수형", "종목형"))
    return {"n_all": n_all, "n_pass": n_pass, "rate": round(rate, 1),
            "cond": cond, "indexes": idx, "stocks": stocks}


def weekly(request):
    # ── 필터 세션 저장/복원 ──
    if "reset" in request.GET:
        request.session.pop("weekly_filters", None)
        return redirect("weekly")

    # 빈 URL(메뉴 클릭)로 오면 저장된 필터 복원
    if not request.GET and request.session.get("weekly_filters"):
        from django.urls import reverse
        return redirect(reverse("weekly") + "?" + request.session["weekly_filters"])

    # 그 외에는 현재 필터 상태를 저장 (주 이동 파라미터 w 제외)
    _saved = request.GET.copy()
    _saved.pop("w", None)
    request.session["weekly_filters"] = _saved.urlencode()

    offset = int(request.GET.get("w", 0))
    monday, sunday = _week_range(offset)

    qs = Product.objects.filter(sub_end__gte=monday, sub_end__lte=sunday)

    # 필터
    f_asset = request.GET.get("asset", "")
    f_ki_max = request.GET.get("ki_max", "")
    f_yield_min = request.GET.get("yield_min", "")
    f_currency = request.GET.get("currency", "")
    f_no_ki = request.GET.get("no_ki", "")
    f_issuers = request.GET.getlist("issuer")  # 다중 선택
    preset_id = request.GET.get("preset", "")

    if preset_id:
        try:
            preset = Preset.objects.get(id=preset_id)
            qs = preset.match_queryset(qs)
        except Preset.DoesNotExist:
            pass
    else:
        if f_issuers:
            qs = qs.filter(issuer__in=f_issuers)
        if f_asset:
            qs = qs.filter(asset_type=f_asset)
        if f_currency:
            qs = qs.filter(currency=f_currency)
        if f_yield_min:
            qs = qs.filter(yield_rate__gte=float(f_yield_min))
        if f_ki_max:
            from django.db.models import Q
            cond = Q(is_no_ki=False, ki__lte=int(f_ki_max))
            if f_no_ki != "exclude":
                cond |= Q(is_no_ki=True)
            qs = qs.filter(cond)

    # ── 정렬 ──────────────────────────────────────
    from django.db.models import F
    SORT_FIELDS = {
        "issuer": "issuer", "product_no": "product_no", "assets": "assets_raw",
        "yield": "yield_rate", "ki": "ki", "first": "barrier_first",
        "last": "barrier_last", "period": "period_months", "type": "asset_type",
        "sub_end": "sub_end", "loss": "loss_prob",
    }
    # term_months·peak_ratio는 계산값(DB 컬럼 아님) → Python 정렬
    PY_SORT = {"term": lambda p: p.term_months, "peak": lambda p: p.peak_ratio}

    # 기본 정렬: 수익률 내림차순 (정렬 파라미터 없을 때)
    sort_key = request.GET.get("sort", "yield")
    if sort_key not in PY_SORT and sort_key not in SORT_FIELDS:
        sort_key = "yield"
    sort_dir = request.GET.get("dir", "desc" if "sort" not in request.GET else "asc")
    if sort_key in PY_SORT:
        products = list(qs.order_by("-yield_rate"))
    else:
        field = SORT_FIELDS[sort_key]
        ordering = (F(field).desc(nulls_last=True) if sort_dir == "desc"
                    else F(field).asc(nulls_last=True))
        products = list(qs.order_by(ordering, "-yield_rate"))

    # 고점대비 — 표시·정렬 모두에 필요하므로 정렬 전에 붙인다
    attach_peak_ratios(products)

    if sort_key in PY_SORT:
        # None은 정렬 방향과 무관하게 항상 뒤로 (sentinel)
        desc = sort_dir == "desc"
        sentinel = float("-inf") if desc else float("inf")
        getter = PY_SORT[sort_key]
        products.sort(
            key=lambda p: getter(p) if getter(p) is not None else sentinel,
            reverse=desc,
        )

    # 정렬 헤더용 컬럼 메타 (URL은 현재 필터 유지 + 정렬 토글)
    base_params = request.GET.copy()
    base_params.pop("sort", None)
    base_params.pop("dir", None)

    def _sort_url(key):
        p = base_params.copy()
        p["sort"] = key
        p["dir"] = "desc" if (sort_key == key and sort_dir == "asc") else "asc"
        return "?" + p.urlencode()

    col_defs = [
        ("issuer", "발행사", False), ("product_no", "상품번호", False),
        ("assets", "기초자산", False), ("yield", "수익률", True),
        ("ki", "낙인", True), ("first", "1차", True), ("last", "막차", True),
        ("term", "기간", True), ("period", "주기", True),
        ("peak", "고점대비", True), ("loss", "손실확률", True),
        ("type", "유형", False), ("sub_end", "마감", True),
    ]
    columns = [
        {"key": k, "label": lbl, "num": num, "url": _sort_url(k),
         "active": sort_key == k, "dir": sort_dir}
        for k, lbl, num in col_defs
    ]

    watched_ids = set(_scope(WatchItem.objects.all(), request.user).values_list("product_id", flat=True))
    invested_ids = set()
    if request.user.is_authenticated:
        invested_ids = set(Investment.objects.filter(
            user=request.user, status="보유중").values_list("product_id", flat=True))
    last_import = ImportLog.objects.first()
    freshness_days = None
    if last_import:
        freshness_days = (date.today() - last_import.imported_at.date()).days

    regime = _market_regime(monday, sunday)

    # 발행사 필터 후보 (이번 주 상품에 존재하는 발행사)
    issuers = sorted(set(
        Product.objects.filter(sub_end__gte=monday, sub_end__lte=sunday)
        .values_list("issuer", flat=True)
    ))

    # ── 이번주 TOP5 (현재 주만) ──
    # 아주 강한 신호 상품 중 손실확률 0% & 1년내 조기상환 ≥90% → 수익률 상위 5.
    # (고쿠폰이지만 손실확률이 있는 상품은 '강한 신호'에서 확인)
    # 중복도 = 보유 포트폴리오 중 같은 기초자산을 가진 투자금 비중.
    # 선정 로직은 models.radar_top5()로 통일(주간요약 텔레그램과 동일 기준).
    # overlap(보유 중복도) 계산만 뷰에 남긴다.
    # ── v7 TOP5 이원화: 안정(지수형) / 수익(종목형) 트랙 ──
    top5_tracks = []
    if offset >= 0:  # 지난 주 조회 시에는 표시 안 함
        from core import market as _mkt
        from core.models import radar_tracks

        def _asset_keys(raw):
            return {_mkt.resolve_ticker(a) or a for a in _mkt.split_assets(raw)}

        # 본인 보유 기준 — 전 회원 합산이면 남의 포트폴리오와의 중복률이 나온다.
        # 익명 사용자에게는 조회 자체를 하지 않는다. user=AnonymousUser로 filter를
        # 걸면 DB에 닿기도 전에 TypeError가 나서 /weekly/가 통째로 500이 됐다
        # (2026-08-03, 비로그인 방문자 전원 영향. 어차피 결과는 staff에게만 쓴다).
        inv_assets = []
        if request.user.is_authenticated:
            inv_assets = [
                (inv.amount, _asset_keys(inv.product.assets_raw))
                for inv in Investment.objects.filter(
                    user=request.user, status="보유중").select_related("product")
            ]
        total_held = sum(amt for amt, _ in inv_assets)

        # 중복도는 가족(staff) 계정에만 표시 — 외부인에게 보유 성향 노출 방지
        show_overlap = request.user.is_authenticated and request.user.is_staff
        tracks = radar_tracks(monday, sunday)
        TRACK_META = {
            "지수형": {"label": "지수형 TOP5",
                      "sub": "1차 조기상환 90 이하 · 고점 회피(완만한 상승은 예외) · "
                             "낙인이 가장 낮은 30% 미만", "icon": "fa-shield-halved"},
            "종목형": {"label": "종목형 TOP5",
                      "sub": "1차 조기상환 80 이하 · 고점 회피(완만한 상승은 예외) · "
                             "낙인이 가장 낮은 30% 미만", "icon": "fa-rocket"},
        }
        for tier in ("지수형", "종목형"):
            items = []
            for p in tracks.get(tier, []):
                overlap_pct = None
                if show_overlap:
                    pkeys = _asset_keys(p.assets_raw)
                    overlap = sum(amt for amt, keys in inv_assets if keys & pkeys)
                    overlap_pct = round(overlap / total_held * 100) if total_held else 0
                r = p.radar or {}
                items.append({"p": p, "overlap_pct": overlap_pct,
                              "peak": r.get("peak")})
            if items:
                meta = TRACK_META[tier]
                top5_tracks.append({
                    "tier": tier, "label": meta["label"], "sub": meta["sub"],
                    "icon": meta["icon"],
                    "color": items[0]["p"].radar["color"], "items": items,
                })

    # ── 낙인대별 최고 수익 (선택한 주차 상품 · 유형 탭 × 낙인 값별 수익률 top5) ──
    # 대상 낙인 값은 태훈님 지정: 종목형 15~30, 지수형 25~40. 노낙인 제외.
    # TOP5처럼 주차를 따라간다 (주 이동 시 그 주의 분포로 갱신, 필터바 무관).
    # 지수형 먼저 (탭 기본 선택도 지수형) — 2026-08-03 태훈님 요청
    KI_BUCKETS = {"지수형": (25, 30, 35, 40), "종목형": (15, 20, 25, 30)}
    from collections import defaultdict
    _buckets = defaultdict(list)
    for p in Product.objects.filter(
            sub_end__gte=monday, sub_end__lte=sunday,
            is_no_ki=False, ki__isnull=False):
        t = p.asset_type
        if t in KI_BUCKETS and p.ki in KI_BUCKETS[t]:
            _buckets[(t, int(p.ki))].append(p)
    ki_top5 = []
    for t, kis in KI_BUCKETS.items():
        cols = []
        for k in kis:
            lst = _buckets.get((t, k), [])
            if not lst:
                continue
            top = sorted(lst, key=lambda p: p.yield_rate or 0, reverse=True)[:5]
            cols.append({"ki": k, "count": len(lst), "top": top})
        if cols:
            ki_top5.append({"type": t, "cols": cols})
    if not ki_top5:
        ki_top5 = None

    return render(request, "core/weekly.html", {
        "regime": regime,
        "products": products,
        "top5_tracks": top5_tracks,
        "ki_top5": ki_top5,
        "columns": columns,
        "monday": monday, "sunday": sunday, "offset": offset,
        "total": len(products),
        "presets": _scope(Preset.objects.all(), request.user),
        "issuers": issuers,
        "watched_ids": watched_ids,
        "invested_ids": invested_ids,
        "freshness_days": freshness_days,
        "filters": {
            "asset": f_asset, "ki_max": f_ki_max, "yield_min": f_yield_min,
            "currency": f_currency, "no_ki": f_no_ki, "preset": preset_id,
            "issuers": f_issuers,
        },
        "has_saved_filters": bool(request.session.get("weekly_filters")),
        "active_nav": "weekly",
    })


# ── 상품 상세 ─────────────────────────────────────
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    is_watched = _scope(WatchItem.objects.filter(product=product), request.user).exists()

    # 배리어 계단 SVG용 데이터
    barriers = product.barriers_raw or []
    svg = None
    if barriers:
        w, h, pad = 640, 220, 36
        n = len(barriers)
        bmin, bmax = min(barriers), max(barriers)
        span = max(bmax - bmin, 10)
        steps = []
        step_w = (w - pad * 2) / n
        for i, b in enumerate(barriers):
            x = pad + i * step_w
            y = pad + (bmax - b) / span * (h - pad * 2)
            steps.append({
                "x": round(x, 1), "x2": round(x + step_w - 4, 1),
                "cx": round(x + step_w / 2, 1),
                "y": round(y, 1), "v": b, "n": i + 1,
            })
        ki_y = None
        if product.ki is not None:
            ki_y = pad + (bmax - product.ki) / span * (h - pad * 2)
            ki_y = min(ki_y, h - 8)
        svg = {"w": w, "h": h, "steps": steps, "ki_y": ki_y}

    # 수익률 모의실험 결과 (배치가 저장한 캐시)
    sim = product.sim_result or None

    # 이 상품을 보유 중이면 상품별 낙인 모니터링 (자산별 레벨/버퍼)
    from core.models import KnockInStatus
    ki_statuses = None
    ki_worst_buffer = None
    ki_updated_at = None
    inv = None
    if request.user.is_authenticated:
        inv = (product.investments.filter(user=request.user, status="보유중")
               .prefetch_related("ki_status").first())
    if inv:
        rows = list(inv.ki_status.all())
        if rows:
            for s in rows:
                s.buffer = None if (s.level_pct is None or product.ki is None or product.is_no_ki) \
                    else round(s.level_pct - product.ki, 1)
                if s.updated_at and (ki_updated_at is None or s.updated_at > ki_updated_at):
                    ki_updated_at = s.updated_at
            ki_statuses = sorted(rows, key=lambda s: (s.level_pct if s.level_pct is not None else 999))
            ki_worst_buffer = inv.ki_buffer

    # ── 기초자산 1년 시세 차트 (레벨% = 종가/기준가×100, SVG) ──
    from core import market as _mkt
    chart = None
    assets = _mkt.split_assets(product.assets_raw)
    series_list = []
    # 기준가 산정일 + 거래일 오프셋 (설명서 평가일 우선, 없으면 발행사별 규칙)
    base_date, base_back = _mkt.base_price_date(product)
    for asset in assets[:4]:
        t = _mkt.resolve_ticker(asset)
        if not t:
            continue
        hist = _mkt.fetch_history(t)
        if len(hist) < 10:
            continue
        ref = None
        if base_date:
            past = [c for d, c in hist if d <= base_date]
            ref = past[-1 - base_back] if len(past) > base_back else None
        if ref is None:
            ref = hist[0][1]  # 미발행 상품은 1년 전 시점=100
        hi_d, hi_c = max(hist, key=lambda x: x[1])
        lo_d, lo_c = min(hist, key=lambda x: x[1])
        series_list.append({"asset": asset, "ref": ref,
                            "hi": (hi_d, hi_c), "lo": (lo_d, lo_c),
                            "cur": hist[-1][1],
                            "pts": [(d, c / ref * 100) for d, c in hist]})
    if series_list:
        all_d = [d for s in series_list for d, _ in s["pts"]]
        dmin, dmax = min(all_d), max(all_d)
        span = (dmax - dmin).days or 1
        levels = [v for s in series_list for _, v in s["pts"]]
        marks = [100]
        if product.barrier_first is not None:
            marks.append(product.barrier_first)
        if product.ki is not None and not product.is_no_ki:
            marks.append(product.ki)
        lo = min(min(levels), min(marks)) - 5
        hi = max(max(levels), max(marks)) + 5
        W, H, PL, PR, PT, PB = 720, 260, 44, 10, 14, 30
        pw, ph = W - PL - PR, H - PT - PB

        def _x(d):
            return round(PL + pw * (d - dmin).days / span, 1)

        def _y(v):
            return round(PT + ph * (1 - (v - lo) / (hi - lo)), 1)

        palette = ["#1b64da", "#e8590c", "#0ca678", "#845ef7"]

        def _fmt_price(v):
            return f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}"

        chart_series = []
        for i, s in enumerate(series_list):
            ref = s["ref"]
            (hi_d, hi_c), (lo_d, lo_c) = s["hi"], s["lo"]
            chart_series.append({
                "asset": s["asset"], "color": palette[i % 4],
                "poly": " ".join(f"{_x(d)},{_y(v)}" for d, v in s["pts"]),
                "last": round(s["pts"][-1][1], 1),
                # 실제 가격 정보 (기준가 통화 단위 그대로)
                "ref_price": _fmt_price(ref),
                "cur_price": _fmt_price(s["cur"]),
                "first_price": _fmt_price(ref * product.barrier_first / 100)
                               if product.barrier_first is not None else None,
                "ki_price": _fmt_price(ref * product.ki / 100)
                            if (product.ki is not None and not product.is_no_ki) else None,
                "hi_price": _fmt_price(hi_c), "hi_date": hi_d,
                "hi_x": _x(hi_d), "hi_y": _y(hi_c / ref * 100),
                "lo_price": _fmt_price(lo_c), "lo_date": lo_d,
                "lo_x": _x(lo_d), "lo_y": _y(lo_c / ref * 100),
            })
        chart_lines = [{"label": "기준 100", "y": _y(100), "color": "#868e96", "dash": "4 3"}]
        if product.barrier_first is not None:
            chart_lines.append({"label": f"1차 {product.barrier_first:g}",
                                "y": _y(product.barrier_first), "color": "#e8590c", "dash": "6 3"})
        if product.ki is not None and not product.is_no_ki:
            chart_lines.append({"label": f"낙인 {product.ki:g}",
                                "y": _y(product.ki), "color": "#e03131", "dash": "2 3"})
        # 고점대비 — 발행 시점(기준가 산정일)과 현재 시점을 따로 계산해 둘 다 보여준다.
        # 화면 문구가 "…자리에서 발행"이면 숫자도 발행일 값이어야 한다(2026-08-05 태훈님).
        # 상품 단위 값은 자산별 최댓값 = 가장 덜 빠진 자산 기준(레이더 게이트와 같은 뜻).
        peak = peak_ratios(product)
        # 고점 발행이었나 판정은 템플릿이 issue_max(발행 완료분)로 한다 — v7 게이트가
        # 보는 시점과 맞추기 위함. 미발행분만 now_max로 "이대로면" 이라고 쓴다.
        peak["asset_rows"] = [r for r in peak["rows"] if r["now"] is not None]
        # 발행일(기준가 산정일) 세로 점선 — 차트 구간 안에 있을 때만
        base_x = _x(base_date) if (base_date and dmin <= base_date <= dmax) else None
        chart = {"W": W, "H": H, "series": chart_series, "lines": chart_lines,
                 "based_on_issue": bool(base_date),
                 "range_from": dmin, "range_to": dmax,
                 "base_x": base_x, "top": PT, "bottom": H - PB,
                 "peak": peak}

    return render(request, "core/product_detail.html", {
        "product": product, "is_watched": is_watched, "svg": svg,
        "sim": sim, "sim_updated": product.sim_updated,
        "ki_statuses": ki_statuses, "ki_worst_buffer": ki_worst_buffer,
        "ki_updated_at": ki_updated_at,
        "chart": chart,
        "my_inv": inv,  # 보유 중이면 상품 정보에 내 투자금액 표시
        "active_nav": "weekly",
    })


# ── 프리셋 관리 ───────────────────────────────────
@login_required
def presets(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            _scope(Preset.objects.filter(id=request.POST.get("id")), request.user).delete()
            messages.success(request, "프리셋을 삭제했습니다.")
        else:  # create / update
            pid = request.POST.get("id")
            data = dict(
                name=request.POST.get("name", "").strip() or "이름없음",
                issuers=request.POST.getlist("issuers"),
                ki_min=request.POST.get("ki_min") or None,
                ki_max=request.POST.get("ki_max") or None,
                include_no_ki=request.POST.get("include_no_ki") == "on",
                asset_type=request.POST.get("asset_type", "전체"),
                yield_min=request.POST.get("yield_min") or None,
                period_max=request.POST.get("period_max") or None,
                currency=request.POST.get("currency", "전체"),
                notify=request.POST.get("notify") == "on",
            )
            if pid:
                _scope(Preset.objects.filter(id=pid), request.user).update(**data)
                messages.success(request, "프리셋을 수정했습니다.")
            else:
                data["user"] = request.user
                if not request.user.is_staff:
                    data["notify"] = False  # 텔레그램은 운영 채널 전용
                Preset.objects.create(**data)
                messages.success(request, "프리셋을 추가했습니다.")
        return redirect("presets")

    today = date.today()
    active_products = Product.objects.filter(sub_end__gte=today)
    preset_list = []
    for p in _scope(Preset.objects.all(), request.user):
        preset_list.append({"obj": p, "match_count": p.match_queryset(active_products).count()})

    # 발행사 후보 (전체 상품 기준 — 최근 60일)
    issuers = sorted(set(
        Product.objects.filter(sub_end__gte=today - timedelta(days=60))
        .values_list("issuer", flat=True)
    ))

    return render(request, "core/presets.html", {
        "preset_list": preset_list, "issuers": issuers, "active_nav": "presets",
    })


# ── 관심 목록 ─────────────────────────────────────
@login_required
def watchlist(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "clear":  # product_id 없이 옴 → 상단에서 먼저 처리
            n, _ = _scope(WatchItem.objects.all(), request.user).delete()
            messages.success(request, f"관심 목록 {n}건을 초기화했습니다.")
            return redirect("watchlist")
        product = get_object_or_404(Product, pk=request.POST.get("product_id"))
        is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
        if action == "add":
            WatchItem.objects.get_or_create(product=product, user=request.user)
            if not is_ajax:
                messages.success(request, "관심 목록에 등록했습니다.")
        elif action == "remove":
            _scope(WatchItem.objects.filter(product=product), request.user).delete()
            if not is_ajax:
                messages.success(request, "관심 목록에서 해제했습니다.")
        if is_ajax:
            from django.http import JsonResponse
            return JsonResponse({"watched": action == "add"})
        return redirect(request.POST.get("next") or "watchlist")

    items = list(_scope(WatchItem.objects.select_related("product").all(), request.user))
    invested_ids = set(Investment.objects.filter(
        user=request.user, status="보유중").values_list("product_id", flat=True))

    # ── 정렬 (헤더 클릭) ──
    _min_date = date.min
    W_SORT = {
        "issuer": lambda i: i.product.issuer or "",
        "no": lambda i: i.product.product_no or "",
        "assets": lambda i: i.product.assets_raw or "",
        "yield": lambda i: i.product.yield_rate if i.product.yield_rate is not None else -1,
        "ki": lambda i: 999 if i.product.is_no_ki else (i.product.ki if i.product.ki is not None else -1),
        "first": lambda i: i.product.barrier_first if i.product.barrier_first is not None else -1,
        "last": lambda i: i.product.barrier_last if i.product.barrier_last is not None else -1,
        "term": lambda i: i.product.term_months if i.product.term_months is not None else -1,
        "period": lambda i: i.product.period_months if i.product.period_months is not None else -1,
        "loss": lambda i: i.product.loss_prob if i.product.loss_prob is not None else -1,
        "type": lambda i: i.product.asset_type or "",
        "sub_end": lambda i: i.product.sub_end or _min_date,
        "confirm": lambda i: i.product.confirm_date or _min_date,
    }
    # 기본 정렬: 마감 가까운 순 (sub_end 오름차순)
    w_sort = request.GET.get("wsort", "sub_end")
    w_dir = request.GET.get("wdir", "asc")
    if w_sort in W_SORT:
        items.sort(key=W_SORT[w_sort], reverse=(w_dir == "desc"))

    def _wurl(key):
        d = "desc" if (w_sort == key and w_dir == "asc") else "asc"
        return f"?wsort={key}&wdir={d}"

    w_cols = [
        {"key": k, "label": lbl, "num": num, "url": _wurl(k),
         "active": w_sort == k, "dir": w_dir}
        for k, lbl, num in [
            ("issuer", "발행사", False), ("no", "상품번호", False),
            ("assets", "기초자산", False), ("yield", "수익률", True),
            ("ki", "낙인", True), ("first", "1차", True), ("last", "막차", True),
            ("term", "기간", True), ("period", "주기", True),
            ("loss", "손실확률", True), ("type", "유형", False),
            ("sub_end", "마감", True), ("confirm", "숙려마감", True),
        ]
    ]

    return render(request, "core/watchlist.html", {
        "invested_ids": invested_ids,
        "items": items, "w_cols": w_cols, "active_nav": "watchlist",
        "vapid_public_key": settings.VAPID_PUBLIC_KEY,
    })


def _split_assets(assets_raw: str):
    """'KOSPI200 , SK하이닉스' → ['KOSPI200', 'SK하이닉스']."""
    import re
    return [a.strip() for a in re.split(r"[,/]+", assets_raw or "") if a.strip()]


def _analyze_risk(holding, total_invested):
    """보유 포트폴리오의 집중도/분산 리스크 분석.

    ELS는 워스트오브 구조 → 각 기초자산에 투자금 전액이 노출된다.
    같은 자산에 여러 건 몰리면 그 자산 하나로 전체가 위험해진다.
    """
    if not holding or not total_invested:
        return None

    from collections import defaultdict

    def _bucket():
        return {"amount": 0, "count": 0, "items": []}

    asset_exposure = defaultdict(_bucket)
    issuer_exposure = defaultdict(_bucket)
    week_exposure = defaultdict(_bucket)     # 청약 주차(빈티지)
    ki_exposure = defaultdict(_bucket)       # 종목형 낙인별
    maturity_buckets = defaultdict(int)  # 'YYYY-MM' → 건수

    for inv in holding:
        p = inv.product
        item = {"pid": p.id, "label": f"{p.issuer} {p.product_no}", "amount": inv.amount,
                "ki": "없음" if p.is_no_ki else (f"{p.ki:g}" if p.ki is not None else "-"),
                "issued": p.issued_on}
        for asset in _split_assets(p.assets_raw):
            b = asset_exposure[asset]
            b["amount"] += inv.amount
            b["count"] += 1
            b["items"].append(item)
        b = issuer_exposure[p.issuer]
        b["amount"] += inv.amount
        b["count"] += 1
        b["items"].append(item)
        # 청약 주차 = 마감일이 속한 주의 월요일. 같은 주 발행분은 같은 빈티지로,
        # 시장 급변 시 함께 무너질 수 있다 (2021 HSCEI·2023 LG화학 사례).
        wd = p.sub_end or p.issue_date
        if wd:
            monday = wd - timedelta(days=wd.weekday())
            b = week_exposure[monday]
            b["amount"] += inv.amount
            b["count"] += 1
            b["items"].append(item)
        # 종목형 낙인별 — 낙인이 얕을수록(숫자 클수록) 방어선이 약하다
        if p.asset_type == "종목형":
            kkey = "노낙인" if p.is_no_ki else (f"낙인 {p.ki:g}" if p.ki is not None else "낙인 미상")
            b = ki_exposure[kkey]
            b["amount"] += inv.amount
            b["count"] += 1
            b["items"].append(item)
        nxt = inv.next_evaluation
        if nxt:
            maturity_buckets[nxt["date"].strftime("%Y-%m")] += 1

    def _row(name, v):
        return {"name": name, "amount": v["amount"], "count": v["count"],
                "pct": round(v["amount"] / total_invested * 100),
                "items": sorted(v["items"], key=lambda i: -i["amount"])}

    def _top(exposure):
        return sorted((_row(k, v) for k, v in exposure.items()),
                      key=lambda r: -r["amount"])

    assets = _top(asset_exposure)
    issuers = _top(issuer_exposure)
    weeks = [_row(f"{k.month}.{k.day}~{(k + timedelta(days=6)).month}.{(k + timedelta(days=6)).day}", v)
             for k, v in sorted(week_exposure.items(), key=lambda kv: -kv[1]["amount"])]
    # 낙인 낮은(깊은) 순 정렬, 노낙인·미상은 뒤로
    def _ki_order(name):
        if name.startswith("낙인 ") and name != "낙인 미상":
            return (0, float(name.split()[1]))
        return (1, 999)
    ki_types = sorted((_row(k, v) for k, v in ki_exposure.items()),
                      key=lambda r: _ki_order(r["name"]))

    # 경고: 단일 자산/발행사 노출이 전체의 50% 초과
    warnings = []
    if assets and assets[0]["pct"] > 50:
        warnings.append(
            f"기초자산 '{assets[0]['name']}'에 전체의 {assets[0]['pct']}%가 집중되어 있습니다."
        )
    if issuers and issuers[0]["pct"] > 60:
        warnings.append(
            f"발행사 '{issuers[0]['name']}'에 전체의 {issuers[0]['pct']}%가 집중되어 있습니다."
        )
    # 청약 주차 집중: 같은 주 상품은 같은 빈티지 — 한 주에 50% 초과면 경고
    if weeks and weeks[0]["pct"] > 50 and len(holding) >= 3:
        warnings.append(
            f"청약 주차 {weeks[0]['name']}에 전체의 {weeks[0]['pct']}%가 집중되어 있습니다. "
            f"같은 주 상품은 시장 급변 시 함께 흔들립니다."
        )
    # 만기 집중: 한 달에 60% 초과 평가 몰림
    if maturity_buckets:
        top_month, top_cnt = max(maturity_buckets.items(), key=lambda x: x[1])
        if top_cnt / len(holding) > 0.6 and len(holding) >= 3:
            warnings.append(f"{top_month} 평가일에 상환이 몰려 있습니다 ({top_cnt}건).")

    return {
        "assets": assets[:6],
        "issuers": issuers[:5],
        "weeks": weeks[:6],
        "week_total": len(weeks),
        "ki_types": ki_types,
        "maturity": sorted(maturity_buckets.items()),
        "warnings": warnings,
    }


# ── 포트폴리오 ────────────────────────────────────
@login_required
def portfolio(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            product = get_object_or_404(Product, pk=request.POST.get("product_id"))
            Investment.objects.create(
                user=request.user,
                product=product,
                amount=int(request.POST.get("amount", "0").replace(",", "")),
                # 발행일 미입력 시 상품 발행일 기준 — 실현수익 연환산이
                # 발행일~상환일 실경과일로 계산되므로 발행일이 정확한 기준
                invested_at=(request.POST.get("invested_at")
                             or product.issued_on or date.today()),
                broker_account=request.POST.get("broker_account", ""),
                memo=request.POST.get("memo", ""),
            )
            _scope(WatchItem.objects.filter(product=product), request.user).delete()
            messages.success(request, "투자를 등록했습니다.")
        elif action == "redeem":
            inv = get_object_or_404(Investment, pk=request.POST.get("id"), user=request.user)
            inv.status = request.POST.get("status", "조기상환")
            inv.redeemed_at = request.POST.get("redeemed_at") or date.today()
            amt = request.POST.get("redeemed_amount", "").replace(",", "")
            inv.redeemed_amount = int(amt) if amt else None
            inv.save()
            messages.success(request, "상환 처리했습니다.")
        elif action == "edit":
            inv = get_object_or_404(Investment, pk=request.POST.get("id"), user=request.user)
            changed = False
            if "amount" in request.POST:
                a = request.POST.get("amount", "").replace(",", "").strip()
                if a.isdigit() and int(a) > 0:
                    inv.amount = int(a)
                    changed = True
            if "redeemed_amount" in request.POST:
                r = request.POST.get("redeemed_amount", "").replace(",", "").strip()
                inv.redeemed_amount = int(r) if r.isdigit() else None
                changed = True
            if changed:
                inv.save()
                messages.success(request, "금액을 수정했습니다.")
        elif action == "delete":
            Investment.objects.filter(pk=request.POST.get("id"), user=request.user).delete()
            messages.success(request, "투자 기록을 삭제했습니다.")
        elif action == "bulk_delete":
            ids = request.POST.getlist("ids")
            n, _ = Investment.objects.filter(pk__in=ids, user=request.user).delete()
            messages.success(request, f"{n}건의 투자 기록을 삭제했습니다.")
        return redirect(request.POST.get("next") or "portfolio")

    invs = (Investment.objects.filter(user=request.user)
            .select_related("product").prefetch_related("ki_status", "verdicts"))
    holding = [i for i in invs if i.status == "보유중"]
    done = [i for i in invs if i.status != "보유중"]
    # 조기상환 실패: 지난 평가에서 배리어 미달이 확정된 건 (통계에는 보유중으로 포함,
    # 리스트 표시만 분리 — 돈은 여전히 들어가 있는 상태이므로)
    missed = [i for i in holding if i.missed_redemption]
    holding_display = [i for i in holding if not i.missed_redemption]

    today = date.today()
    month_end = date(today.year, today.month, pycalendar.monthrange(today.year, today.month)[1])
    this_month_evals = 0
    for inv in holding:
        nxt = inv.next_evaluation
        if nxt and today <= nxt["date"] <= month_end:
            this_month_evals += 1

    total_invested = sum(i.amount for i in holding)
    total_redeemed_profit = sum(
        (i.redeemed_amount - i.amount) for i in done if i.redeemed_amount
    )

    # 세전 예상 수익 (보유분이 1차 평가에 전부 조기상환된다고 가정)
    total_expected_pretax = 0
    for i in holding:
        sched = i.schedule
        total_expected_pretax += sched[0]["expected"] if sched else i.amount
    expected_profit_pretax = total_expected_pretax - total_invested
    # 수익률 = 예상 세전수익 ÷ 총 투자금액 (연환산 아님 — 1차 상환까지의 단순 수익률)
    expected_profit_rate = (round(expected_profit_pretax / total_invested * 100, 2)
                            if total_invested else None)

    # 포트폴리오 예상 손실율 = Σ(투자금 × 손실확률) / Σ투자금  (금액 가중평균)
    weighted_loss = 0
    loss_weight = 0  # 손실확률이 있는 투자금 합 (커버리지 표기용)
    for i in holding:
        lp = i.product.loss_prob
        if lp is not None:
            weighted_loss += i.amount * lp
            loss_weight += i.amount
    port_loss_rate = round(weighted_loss / total_invested, 2) if total_invested else None
    loss_coverage_pct = round(loss_weight / total_invested * 100) if total_invested else 0

    # 유형별(종목형/지수형) 보유 분해 — 건수·투자금액
    holding_by_type = {
        "종목형": {"count": 0, "amount": 0},
        "지수형": {"count": 0, "amount": 0},
        "기타": {"count": 0, "amount": 0},
    }
    for i in holding:
        t = i.product.asset_type if i.product.asset_type in ("종목형", "지수형") else "기타"
        holding_by_type[t]["count"] += 1
        holding_by_type[t]["amount"] += i.amount

    # ── 리스크 분석 ──────────────────────────────
    risk = _analyze_risk(holding, total_invested)

    # ── 스트레스 테스트: 자산별 추가 하락 시나리오 → 낙인 진입액·예상 손실 ──
    # 위험 단위는 '노출 %'가 아니라 '낙인 발동가 분포'다. 같은 자산이라도
    # 발행 시기(기준가)가 다르면 발동가가 달라 함께 무너지지 않는다 — 총노출만
    # 보면 과대평가라는 태훈님 지적(2026-08-01)을 반영한 지표.
    KI_LOSS_ASSUMED = 55   # 낙인 확정 시 원금 손실률 가정(%)
    stress = None
    if holding and total_invested:
        from collections import defaultdict as _dd

        from core import market as _mkt_s
        from core.models import KnockInStatus

        # 자산 키는 티커로 정규화 — 'Micron'과 'Micron Technology'가 표기만 다른
        # 같은 자산으로 분리되면 3% 게이트에 걸려 통째로 누락된다 (레드팀 B [상3])
        def _akey(name):
            n = (name or "").strip()
            return _mkt_s.resolve_ticker(n) or n

        _sa = _dd(lambda: {"name": None, "cur": None, "cur_at": None, "trigs": [], "amt": 0})
        _inv_assets = _dd(list)   # inv_id → [(asset_key, trigger|None)]
        _inv_amt = {}
        _covered = set()
        for _s in KnockInStatus.objects.filter(
                investment__in=holding).select_related("investment__product"):
            _inv = _s.investment
            if _s.ref_price is None or _s.current_price is None:
                continue
            _p = _inv.product
            _k = _akey(_s.asset_name)
            _a = _sa[_k]
            if _a["name"] is None:
                _a["name"] = _mkt_s.shorten_asset_display(_s.asset_name.strip())
            # 티커로 합친 버킷에는 표기가 다른 행이 섞인다('Micron'/'Micron Technology').
            # 그냥 덮어쓰면 마지막에 읽힌 행의 현재가가 자산 전체의 기준이 돼,
            # 배치가 갱신하다 만 옛 값이 여유·손실 계산을 통째로 흔들 수 있다.
            # 갱신 시각이 가장 최신인 행의 값을 채택한다.
            if _a["cur"] is not None and _a["cur"] != _s.current_price:
                logger.info("스트레스 테스트 현재가 불일치 자산=%s: %s(%s) vs %s(%s) — 최신값 채택",
                            _k, _a["cur"], _a["cur_at"], _s.current_price, _s.updated_at)
            if _a["cur"] is None or (_s.updated_at is not None
                                     and (_a["cur_at"] is None or _s.updated_at > _a["cur_at"])):
                _a["cur"] = _s.current_price
                _a["cur_at"] = _s.updated_at
            _covered.add(_inv.id)
            _inv_amt[_inv.id] = _inv.amount
            trig = None
            if not (_p.is_no_ki or _p.ki is None):
                trig = _s.ref_price * _p.ki / 100.0
                _a["trigs"].append((trig, _inv.amount))
            _inv_assets[_inv.id].append((_k, trig))
        # 노출은 투자 단위로 (같은 투자가 한 자산에 두 번 잡히지 않게)
        for _iid, _al in _inv_assets.items():
            for _k in {k for k, _ in _al}:
                _sa[_k]["amt"] += _inv_amt[_iid]

        SHOCKS = (40, 50, 60, 70)
        rows = []
        _shown = set()   # 3% 게이트를 통과해 실제로 표에 그려지는 자산 키
        for _k, _a in _sa.items():
            pct = _a["amt"] / total_invested * 100
            if pct < 3 or not _a["cur"] or not _a["trigs"]:
                continue
            _shown.add(_k)
            needs = [(1 - trig / _a["cur"]) * 100 for trig, _ in _a["trigs"]]
            amts = [amt for _, amt in _a["trigs"]]
            # 금액가중 평균 여유 — 대표값 (min은 가장 취약한 1건이라 대표성이 없다)
            wavg = sum(n * m for n, m in zip(needs, amts)) / sum(amts)
            losses = {}
            for d in SHOCKS:
                px = _a["cur"] * (1 - d / 100)
                hit = sum(amt for trig, amt in _a["trigs"] if px < trig)
                losses[d] = round(hit / total_invested * 100 * KI_LOSS_ASSUMED / 100, 2)
            rows.append({"name": _a["name"] or _k, "pct": round(pct, 1),
                         "wavg": round(wavg, 1), "nearest": round(min(needs), 1),
                         "losses": losses})
        if rows:
            # 합계는 투자 단위 union — 워스트오브 상품은 어느 자산이든 발동가를
            # 깨지면 낙인 1번이다. 자산 행 합산은 다중자산 투자를 중복 계상해
            # 이론상 상한(55%)을 넘는 값까지 만들었다 (레드팀 B [상1·상2])
            # union 범위는 표에 그려진 자산(_shown)으로 제한한다. 3% 게이트에 걸려
            # 행이 없는 자산까지 세면 <tfoot> '합계'가 바로 위 행들과 안 맞아,
            # 같은 표 안에서 셈이 어긋난 것처럼 보인다.
            _cur = {k: _sa[k]["cur"] for k in _sa}
            tot = {}
            for d in SHOCKS:
                knocked = 0
                for _iid, _al in _inv_assets.items():
                    if any(k in _shown and t is not None and _cur.get(k)
                           and _cur[k] * (1 - d / 100) < t for k, t in _al):
                        knocked += _inv_amt[_iid]
                tot[d] = round(knocked / total_invested * 100 * KI_LOSS_ASSUMED / 100, 2)
            # '몰아 샀다면'(-60%): 전액을 각 자산의 가장 취약한 기준가에 샀다고 가정.
            # 자산 단위 _worst_trig만 보면 안 되고 투자별 발동가 t도 함께 봐야 한다 —
            # t가 None인 노낙인 투자는 같은 기초자산의 낙인 상품이 아무리 위험해도
            # 절대 낙인되지 않는데, 이를 빼먹으면 그 투자금까지 conc60에 얹혀
            # '시기를 나눠 산 덕분에 N%p를 막았습니다'가 부풀려진다.
            _worst_trig = {k: max((t for t, _ in _sa[k]["trigs"]), default=None)
                           for k in _shown}
            conc_knocked = 0
            for _iid, _al in _inv_assets.items():
                if any(t is not None and _worst_trig.get(k) is not None and _cur.get(k)
                       and _cur[k] * 0.40 < _worst_trig[k] for k, t in _al):
                    conc_knocked += _inv_amt[_iid]
            conc = round(conc_knocked / total_invested * 100 * KI_LOSS_ASSUMED / 100, 2)
            rows.sort(key=lambda r: -r["losses"][60])
            _miss = [i for i in holding if i.id not in _covered]
            stress = {
                "rows": rows,
                "shocks": list(SHOCKS),
                "loss_assumed": KI_LOSS_ASSUMED,
                "total": tot,
                "conc60": conc,
                "saved60": round(conc - tot[60], 2),
                "worst": rows[0],
                "missing_n": len(_miss),
                "missing_amt": sum(i.amount for i in _miss),
            }

    # ── 낙인 모니터링: 전체 보유 중 위험/경고(버퍼 ≤ 20%p)만 추림 ──
    ki_updated = None
    ki_alerts = []
    for inv in holding:
        worst = inv.worst_ki_status
        for s in inv.ki_status.all():
            if s.updated_at and (ki_updated is None or s.updated_at > ki_updated):
                ki_updated = s.updated_at
        buf = inv.ki_buffer
        if worst is not None and buf is not None and buf <= 20:
            ki_alerts.append({"inv": inv, "worst": worst, "buffer": buf})
    ki_alerts.sort(key=lambda a: a["buffer"])  # 위험한 순
    has_ki_data = ki_updated is not None

    # 투자 등록 폼용 상품 후보 (최근 청약 상품)
    # 투자등록 후보 = 관심목록에 담아둔 상품만 (가족=공용+본인, 회원=본인)
    candidates = Product.objects.filter(
        id__in=_scope(WatchItem.objects.all(), request.user).values_list("product_id", flat=True)
    ).order_by("-sub_end", "issuer")

    # ── 보유 리스트 정렬 ──
    def _pretax(i):
        s = i.schedule
        return s[0]["expected"] if s else 0

    H_SORT = {
        "issuer": lambda i: (i.product.issuer or ""),
        "assets": lambda i: (i.product.assets_raw or ""),
        "amount": lambda i: i.amount or 0,
        "yield": lambda i: i.product.yield_rate if i.product.yield_rate is not None else -1,
        "issue": lambda i: (i.product.issued_on or date.max),
        "ki": lambda i: (999 if i.product.is_no_ki or i.product.ki is None else i.product.ki),
        # Product에는 stepdown_barriers가 없다(HistoricalIssue 필드) — 예전엔
        # AttributeError로 이 정렬이 500을 냈다. 1차 배리어는 barrier_first다.
        "b1": lambda i: (i.product.barrier_first
                         if i.product.barrier_first is not None else 999),
        "next": lambda i: (i.next_evaluation["date"] if i.next_evaluation else date.max),
        "pretax": _pretax,
        "loss": lambda i: (i.product.loss_prob if i.product.loss_prob is not None else -1),
    }
    h_sort = request.GET.get("hsort", "next")
    if h_sort not in H_SORT:
        h_sort = "next"
    h_dir = request.GET.get("hdir", "asc")
    holding_display.sort(key=H_SORT[h_sort], reverse=(h_dir == "desc"))
    missed.sort(key=lambda i: i.missed_redemption.eval_date)  # 오래 놓친 순

    def _hsort_url(key):
        d = "desc" if (h_sort == key and h_dir == "asc") else "asc"
        return f"?hsort={key}&hdir={d}&psize={page_size}"

    # ── 페이지네이션 ──
    from django.core.paginator import Paginator
    try:
        page_size = int(request.GET.get("psize", 10))
    except (ValueError, TypeError):
        page_size = 10
    if page_size not in (10, 20, 50, 100):
        page_size = 10

    h_page = Paginator(holding_display, page_size).get_page(request.GET.get("hpage"))
    d_page = Paginator(done, page_size).get_page(request.GET.get("dpage"))

    h_cols = [
        {"key": k, "label": lbl, "num": num, "url": _hsort_url(k),
         "active": h_sort == k, "dir": h_dir}
        for k, lbl, num in [
            ("issuer", "상품", False), ("assets", "기초자산", False),
            ("amount", "투자금액", True), ("yield", "수익률", True),
            ("ki", "낙인", True), ("b1", "1차 조기상환", True),
            ("issue", "발행일", False),
            ("next", "다음 평가일", False), ("pretax", "예상상환금", True),
            ("loss", "손실확률", True),
        ]
    ]

    # ── 실현수익 실적 (상환완료 + 상환금 입력된 건) ──
    realized = [i for i in done if i.redeemed_amount is not None and i.redeemed_at]
    perf = None
    if realized:
        from collections import defaultdict
        total_in = sum(i.amount for i in realized)
        total_out = sum(i.redeemed_amount for i in realized)
        profit = total_out - total_in
        # 연환산: 각 건의 보유일수 가중 수익률 평균
        ann_rates = []
        for i in realized:
            days = (i.redeemed_at - i.invested_at).days if i.invested_at else None
            if days and days > 0:
                ann_rates.append((i.redeemed_amount - i.amount) / i.amount * 365 / days * 100)
        monthly = defaultdict(int)
        for i in realized:
            monthly[i.redeemed_at.strftime("%Y.%m")] += i.redeemed_amount - i.amount
        months = sorted(monthly)[-12:]
        max_abs = max(abs(monthly[m]) for m in months) or 1
        bars = [{"label": m, "value": monthly[m],
                 "h": round(abs(monthly[m]) / max_abs * 100, 1),
                 "neg": monthly[m] < 0} for m in months]
        perf = {
            "count": len(realized),
            "profit": profit,
            "rate": round(profit / total_in * 100, 2) if total_in else 0,
            "ann_rate": round(sum(ann_rates) / len(ann_rates), 2) if ann_rates else None,
            "bars": bars,
        }

    return render(request, "core/portfolio.html", {
        "vapid_public_key": settings.VAPID_PUBLIC_KEY,
        "perf": perf,
        "h_page": h_page, "d_page": d_page,
        "holding_count": len(holding_display), "done_count": len(done),
        "missed": missed, "missed_count": len(missed),
        "holding_total_count": len(holding),  # 빈 상태 문구용 (보유 전체)
        "h_cols": h_cols, "page_size": page_size,
        "total_invested": total_invested,
        "holding_by_type": holding_by_type,
        "this_month_evals": this_month_evals,
        "total_redeemed_profit": total_redeemed_profit,
        "total_expected_pretax": total_expected_pretax,
        "expected_profit_pretax": expected_profit_pretax,
        "expected_profit_rate": expected_profit_rate,
        "port_loss_rate": port_loss_rate,
        "loss_coverage_pct": loss_coverage_pct,
        "risk": risk,
        "ki_updated": ki_updated,
        "has_ki_data": has_ki_data,
        "ki_alerts": ki_alerts,
        "stress": stress,
        "candidates": candidates,
        "today": today,
        "active_nav": "portfolio",
    })


# ── 포트폴리오 엑셀 양식 다운로드 ─────────────────
PORTFOLIO_COLS = ["발행사", "상품번호", "투자금액(원)", "발행일(YYYY-MM-DD)", "증권사/계좌", "메모"]


@login_required
def portfolio_template(request):
    """투자내역 일괄등록 엑셀 양식 다운로드."""
    import io
    import openpyxl
    from django.http import HttpResponse

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "투자내역"
    ws.append(PORTFOLIO_COLS)
    # 예시 행 (안내용, 업로드 시 발행사가 실제 매칭 안되면 자동 무시됨)
    ws.append(["키움증권", "1965", 10000000, "2026-07-16", "키움 CMA", "예시 행 — 삭제 후 작성"])
    widths = [14, 10, 14, 18, 14, 22]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    guide = wb.create_sheet("작성안내")
    for row in [
        ["ELS 레이더 — 투자내역 일괄등록 양식"],
        [""],
        ["1. '투자내역' 시트에 한 행씩 입력하세요."],
        ["2. 발행사 + 상품번호로 수집된 상품과 자동 매칭합니다."],
        ["   (주간청약/상품 목록에 있는 발행사·상품번호와 동일하게 입력)"],
        ["3. 투자금액은 숫자만 (원 단위). 예: 10000000"],
        ["4. 발행일은 YYYY-MM-DD. 비우면 오늘 날짜로 등록됩니다."],
        ["5. 증권사/계좌·메모는 선택입니다."],
        ["6. 예시 행은 삭제하고 업로드하세요."],
        [""],
        ["※ 매칭 실패한 행은 등록되지 않고 결과에 표시됩니다."],
    ]:
        guide.append(row)
    guide.column_dimensions["A"].width = 60

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = 'attachment; filename="ELS_투자내역_양식.xlsx"'
    return resp


@login_required
def watchlist_export(request):
    """관심목록을 xlsx로 다운로드 (본인 범위)."""
    import io

    import openpyxl
    from django.http import HttpResponse

    cols = ["발행사", "상품번호", "기초자산", "수익률(%)", "낙인", "1차", "막차",
            "기간", "주기", "손실확률(%)", "유형", "청약마감", "숙려마감", "메모", "보유"]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "관심목록"
    ws.append(cols)

    items = _scope(WatchItem.objects.select_related("product").all(), request.user)
    invested_ids = set(Investment.objects.filter(
        user=request.user, status="보유중").values_list("product_id", flat=True))
    for item in items:
        p = item.product
        ws.append([
            p.issuer, p.product_no, p.assets_raw or "",
            p.yield_rate if p.yield_rate is not None else "",
            p.ki_display,
            p.barrier_first if p.barrier_first is not None else "",
            p.barrier_last if p.barrier_last is not None else "",
            p.term_display,
            p.period_display,
            p.loss_prob if p.loss_prob is not None else "",
            p.asset_type or (p.structure_label or ""),
            p.sub_end.strftime("%Y-%m-%d") if p.sub_end else "",
            p.confirm_date.strftime("%Y-%m-%d") if p.confirm_date else "",
            item.memo or "",
            "보유중" if p.id in invested_ids else "",
        ])
    for i, c in enumerate(cols, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = max(10, len(c) + 4)

    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="watchlist_{date.today():%Y%m%d}.xlsx"'
    return resp


@login_required
def portfolio_export(request):
    """보유·상환 내역을 조 팀장 관리 양식(17열)으로 다운로드 — **본인 데이터만**.

    첫 시트 '보유계약'이 조 팀장이 쓰던 엑셀 양식이고, 둘째 시트 '상세'는 종전
    다운로드 형식(손실확률·다음평가일 등)을 그대로 남겨 둔 것이다.
    """
    import io

    import openpyxl
    from django.http import HttpResponse

    from core import portfolio_export as pfx

    mine = (Investment.objects.filter(user=request.user)
            .select_related("product"))

    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "보유계약"
    sheet.append(pfx.COLUMNS)
    for row in pfx.build_rows(mine):
        sheet.append(row)
    for i, w in enumerate(pfx.COLUMN_WIDTHS, 1):
        sheet.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    sheet.freeze_panes = "A2"

    cols = ["발행사", "상품번호", "기초자산", "투자금액(원)", "수익률(%)", "낙인",
            "주기(개월)", "1차까지(개월)", "다음평가일", "예상상환금", "손실확률(%)",
            "발행일", "만기일", "스케줄"]

    ws = wb.create_sheet("상세")
    ws.append(cols)

    holding = sorted(
        [i for i in mine if i.status == "보유중"],
        key=lambda i: (i.next_evaluation["date"] if i.next_evaluation else date.max),
    )
    for inv in holding:
        p = inv.product
        nxt = inv.next_evaluation
        badge = inv.schedule_badge or "확정"
        ws.append([
            p.issuer, p.product_no, p.assets_raw or "",
            inv.amount,
            p.yield_rate if p.yield_rate is not None else "",
            p.ki_display,
            p.period_months if p.period_months is not None else "",
            p.first_eval_months if p.first_eval_months is not None else "",
            (nxt["date"].strftime("%Y-%m-%d") if nxt else ""),
            (nxt["expected"] if nxt and nxt["expected"] else ""),
            p.loss_prob if p.loss_prob is not None else "",
            (p.issued_on.strftime("%Y-%m-%d") if p.issued_on else ""),
            (p.expiry_date.strftime("%Y-%m-%d") if p.expiry_date else ""),
            badge,
        ])

    widths = [12, 10, 22, 14, 9, 6, 9, 11, 12, 14, 9, 12, 12, 8]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    fname = f"ELS_보유계약_{date.today():%Y%m%d}.xlsx"
    from urllib.parse import quote
    resp["Content-Disposition"] = (
        f"attachment; filename=\"portfolio.xlsx\"; "
        f"filename*=UTF-8''{quote(fname)}"
    )
    return resp


def _match_product_for_investment(issuer, product_no):
    """발행사+상품번호로 Product 매칭.

    중복 행(같은 issuer·product_no, sub_end만 다름)이 있을 때 배리어(스케줄 정보)가
    있는 '정상 행'을 우선 선택한다. 그다음 최신 sub_end. 이렇게 해야 스케줄이 빈
    껍데기 행에 투자가 연결되는 문제(예: 미래에셋 37858)를 막는다.
    """
    qs = Product.objects.filter(
        issuer=str(issuer).strip(), product_no=str(product_no).strip()
    )
    candidates = list(qs)
    if not candidates:
        return None

    def _sort_key(p):
        has_barriers = 1 if (p.barriers_raw and len(p.barriers_raw) > 0) else 0
        sub = p.sub_end or date.min  # None은 가장 뒤로
        return (has_barriers, sub, p.id)

    return max(candidates, key=_sort_key)


@login_required
def portfolio_upload(request):
    """엑셀로 투자내역 일괄 등록."""
    if request.method != "POST":
        return redirect("portfolio")

    import openpyxl

    f = request.FILES.get("excel")
    if not f or not f.name.lower().endswith((".xlsx", ".xlsm")):
        messages.error(request, "엑셀 파일(.xlsx)을 선택해주세요.")
        return redirect("portfolio")

    try:
        wb = openpyxl.load_workbook(f, data_only=True)
    except Exception:  # noqa: BLE001
        # 예외 원문(openpyxl·zipfile의 영문 메시지, 서버 파일 경로가 섞여 나온다)이
        # 회원 화면에 그대로 찍히던 것을 로그로 돌린다. 화면에는 사람 말 한 줄만.
        # (2026-08-06)
        logger.exception("포트폴리오 엑셀 업로드 실패: %s", getattr(f, "name", ""))
        messages.error(request, "파일을 읽을 수 없습니다. 형식을 확인해 주세요.")
        return redirect("portfolio")

    ws = wb["투자내역"] if "투자내역" in wb.sheetnames else wb.worksheets[0]

    created = 0
    errors = []
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or all(c is None for c in row):
            continue
        issuer = row[0]
        product_no = row[1] if len(row) > 1 else None
        amount = row[2] if len(row) > 2 else None
        invested = row[3] if len(row) > 3 else None
        broker = row[4] if len(row) > 4 else ""
        memo = row[5] if len(row) > 5 else ""

        if not issuer or product_no is None or amount in (None, ""):
            errors.append(f"{i}행: 발행사·상품번호·투자금액은 필수입니다.")
            continue

        product = _match_product_for_investment(issuer, product_no)
        if not product:
            errors.append(
                f"{i}행: 발행사 '{str(issuer).strip()}' + 상품번호 '{str(product_no).strip()}' "
                f"에 해당하는 수집 상품이 없어 등록하지 못했습니다 "
                f"(발행사명·상품번호를 목록과 동일하게 입력했는지 확인)."
            )
            continue

        try:
            amount_int = int(str(amount).replace(",", "").replace("원", "").strip())
        except (ValueError, TypeError):
            errors.append(f"{i}행: 투자금액 '{amount}'을 숫자로 읽을 수 없습니다.")
            continue

        # 날짜 미기재 시 상품 발행일로 — 오늘 날짜 폴백은 발행일 표시·연환산을 오염시킴
        inv_date = _parse_invest_date(invested) or product.issued_on or date.today()

        Investment.objects.create(
            user=request.user, product=product, amount=amount_int,
            invested_at=inv_date, broker_account=str(broker or "")[:100],
            memo=str(memo or "")[:200],
        )
        WatchItem.objects.filter(product=product).delete()
        created += 1

    if created:
        messages.success(request, f"{created}건의 투자를 등록했습니다.")
    if errors:
        messages.error(request, "일부 행을 건너뛰었습니다: " + " / ".join(errors[:8])
                        + (f" 외 {len(errors)-8}건" if len(errors) > 8 else ""))
    if not created and not errors:
        messages.error(request, "등록할 데이터가 없습니다.")
    return redirect("portfolio")


def _parse_invest_date(val):
    """엑셀 셀값 → date. datetime/문자열/None 처리."""
    from datetime import datetime as _dt
    if val is None or val == "":
        return None
    if hasattr(val, "date"):  # datetime
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip().replace(".", "-").replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


# ── 시장 트렌드 ───────────────────────────────────
def market_trend(request):
    """주차별 평균 수익률·KI 추이 (sub_end 기준, 최근 20주)."""
    from collections import defaultdict

    weeks_n = 20
    qs = Product.objects.filter(sub_end__isnull=False)
    buckets = defaultdict(list)
    for p in qs:
        monday = p.sub_end - timedelta(days=p.sub_end.weekday())
        buckets[monday].append(p)

    ordered = sorted(buckets)[-weeks_n:]
    rows = []
    for wk in ordered:
        ps = buckets[wk]
        ys = [p.yield_rate for p in ps if p.yield_rate is not None]
        kis = [p.ki for p in ps if p.ki is not None and not p.is_no_ki]
        rows.append({
            "week": wk,
            "count": len(ps),
            "avg_yield": round(sum(ys) / len(ys), 1) if ys else None,
            "avg_ki": round(sum(kis) / len(kis), 1) if kis else None,
        })

    # ── SVG 좌표 계산 ──
    W, H = 720, 240
    PAD_L, PAD_R, PAD_T, PAD_B = 44, 44, 20, 40
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    def _line(key, vmin, vmax):
        vals = [r[key] for r in rows if r[key] is not None]
        if not vals:
            return [], vmin, vmax
        lo = vmin if vmin is not None else min(vals)
        hi = vmax if vmax is not None else max(vals)
        span = (hi - lo) or 1
        pts = []
        n = len(rows)
        for i, r in enumerate(rows):
            if r[key] is None:
                continue
            x = PAD_L + (plot_w * i / max(n - 1, 1))
            y = PAD_T + plot_h * (1 - (r[key] - lo) / span)
            pts.append({"x": round(x, 1), "y": round(y, 1), "v": r[key],
                        "week": r["week"], "count": r["count"]})
        return pts, lo, hi

    yield_pts, y_lo, y_hi = _line("avg_yield", None, None)
    ki_pts, k_lo, k_hi = _line("avg_ki", None, None)

    def _polyline(pts):
        return " ".join(f"{p['x']},{p['y']}" for p in pts)

    # 추세 요약 (첫→마지막)
    trend = None
    if len(yield_pts) >= 2:
        diff = yield_pts[-1]["v"] - yield_pts[0]["v"]
        trend = {
            "yield_diff": round(diff, 1),
            "yield_up": diff >= 0,
            "ki_diff": round(ki_pts[-1]["v"] - ki_pts[0]["v"], 1) if len(ki_pts) >= 2 else None,
        }

    # ── 레이더 신호 성과 검증 (verify_radar가 채운 RadarVerdict 집계) ──
    from core.models import RadarVerdict
    BADGE_TIERS = ("타겟 신호", "아주 강한 신호", "강한 신호")
    verdicts = list(RadarVerdict.objects.select_related("product").all())
    badges = [v for v in verdicts if v.tier in BADGE_TIERS]

    radar = None
    if verdicts:
        evaluated = [v for v in badges if v.met is not None]
        hit = [v for v in evaluated if v.met]
        radar_stats = {
            "total": len(badges),
            "evaluated": len(evaluated),
            "hit": len(hit),
            "hit_rate": round(len(hit) / len(evaluated) * 100, 1) if evaluated else None,
        }

        # 주차별(최근 12주) 배지 상품 적중/미충족/대기 — 막대 높이는 상품 수 비례
        wk_map = defaultdict(lambda: {"hit": 0, "miss": 0, "wait": 0})
        for v in badges:
            b = wk_map[v.week_monday]
            if v.met is True:
                b["hit"] += 1
            elif v.met is False:
                b["miss"] += 1
            else:
                b["wait"] += 1
        # 전체 주차 표시 — 판정 확정이 몰린 초기(1~5월) 주차가 최근 12주 창에
        # 잘려 전부 '대기'만 보이던 문제
        recent_weeks = sorted(wk_map)
        max_total = max((sum(wk_map[w].values()) for w in recent_weeks), default=0) or 1
        BAR_MAX = 96
        radar_weeks = []
        for w in recent_weeks:
            b = wk_map[w]
            total = b["hit"] + b["miss"] + b["wait"]
            done = b["hit"] + b["miss"]
            radar_weeks.append({
                "week": w, "total": total,
                "hit": b["hit"], "miss": b["miss"], "wait": b["wait"],
                "hit_h": round(b["hit"] / max_total * BAR_MAX),
                "miss_h": round(b["miss"] / max_total * BAR_MAX),
                "wait_h": round(b["wait"] / max_total * BAR_MAX),
                "rate": round(b["hit"] / done * 100) if done else None,
            })

        # 등급별 1차 적중률 (배지 2등급 + 대조군 '없음')
        grade_defs = [
            ("타겟 신호", "타겟 신호 (v7)", "#1B64DA"),
            ("아주 강한 신호", "아주 강한 신호 (v6)", "#1B64DA"),
            ("강한 신호", "강한 신호 (v6)", "#3182F6"),
            ("없음", "배지 없음 (대조군)", "#8B95A1"),
        ]
        radar_grades = []
        for tier, label, color in grade_defs:
            ev = [v for v in verdicts if v.tier == tier and v.met is not None]
            ht = [v for v in ev if v.met]
            radar_grades.append({
                "label": label, "color": color,
                "total": len(ev), "hit": len(ht),
                "rate": round(len(ht) / len(ev) * 100, 1) if ev else None,
            })

        # 최근 판정 내역 10건 — 판정 확정건만 (평가 전 대기는 미래 평가일이라 제외)
        radar_recent = sorted(
            (v for v in badges if v.met is not None),
            key=lambda v: v.eval_date, reverse=True)[:10]

        radar = {
            "stats": radar_stats,
            "weeks": radar_weeks,
            "grades": radar_grades,
            "recent": radar_recent,
        }

    return render(request, "core/trend.html", {
        "rows": rows,
        "yield_pts": yield_pts, "yield_poly": _polyline(yield_pts),
        "ki_pts": ki_pts, "ki_poly": _polyline(ki_pts),
        "y_lo": y_lo, "y_hi": y_hi, "k_lo": k_lo, "k_hi": k_hi,
        "W": W, "H": H, "trend": trend,
        "radar": radar,
        "active_nav": "trend",
    })


# ── 상환 캘린더 ───────────────────────────────────
@login_required
def redemption_calendar(request):
    today = date.today()
    year = int(request.GET.get("y", today.year))
    month = int(request.GET.get("m", today.month))

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    # 이 달의 평가 이벤트 수집
    events = {}  # day -> [event]
    invs = Investment.objects.filter(user=request.user, status="보유중").select_related("product")
    for inv in invs:
        for row in inv.schedule:
            d = row["date"]
            if d.year == year and d.month == month:
                events.setdefault(d.day, []).append({
                    "inv": inv, "n": row["n"],
                    "barrier": row["barrier"], "expected": row["expected"],
                    "badge": inv.schedule_badge,
                    "is_past": d < today,
                })

    cal = pycalendar.Calendar(firstweekday=0)  # 월요일 시작
    weeks = []
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            row.append({
                "day": day or "",
                "is_today": bool(day) and date(year, month, day) == today,
                "is_past": bool(day) and date(year, month, day) < today,
                "events": events.get(day, []) if day else [],
            })
        weeks.append(row)

    # ── 이 달 예상 결과 요약 ──
    # 한 투자가 같은 달에 두 번 평가받으면 첫 회차에서 상환되고 끝나므로
    # 가장 이른 평가 1건만 대표로 집계한다(투자금액·예상상환금 중복 방지).
    first_ev = {}
    for day in sorted(events):
        for ev in events[day]:
            first_ev.setdefault(ev["inv"].id, (day, ev))
    summary = None
    if first_ev:
        rows = [ev for _, ev in first_ev.values()]
        invested = sum(ev["inv"].amount for ev in rows)
        expected = sum(ev["expected"] for ev in rows if ev["expected"])
        exp_n = sum(1 for ev in rows if ev["expected"])
        by_type = {"종목형": {"amount": 0, "count": 0}, "지수형": {"amount": 0, "count": 0}}
        loss_w = loss_base = 0
        for ev in rows:
            p = ev["inv"].product
            b = by_type.get(p.asset_type)
            if b:
                b["amount"] += ev["inv"].amount
                b["count"] += 1
            if p.loss_prob is not None:
                loss_w += ev["inv"].amount * p.loss_prob
                loss_base += ev["inv"].amount
        summary = {
            "count": len(rows),
            "upcoming": sum(1 for _, (d, _e) in first_ev.items() if not _e["is_past"]),
            "past": sum(1 for _, (d, _e) in first_ev.items() if _e["is_past"]),
            "invested": invested,
            "expected": expected,
            "expected_n": exp_n,
            "profit": expected - sum(ev["inv"].amount for ev in rows if ev["expected"]),
            "by_type": by_type,
            "loss_rate": round(loss_w / loss_base, 2) if loss_base else None,
            "loss_coverage": round(loss_base / invested * 100) if invested else 0,
        }

    return render(request, "core/calendar.html", {
        "year": year, "month": month, "weeks": weeks,
        "prev_y": prev_y, "prev_m": prev_m, "next_y": next_y, "next_m": next_m,
        "event_count": sum(len(v) for v in events.values()),
        "summary": summary,
        "active_nav": "calendar",
    })


# ── 엑셀 업로드 ──────────────────────────────────
@family_required
def upload_excel(request):
    """ELS_Curator가 만든 청약중인상품_*.xlsx를 웹에서 업로드해 임포트."""
    import io
    import os as _os

    from django.conf import settings as _settings
    from django.core.management import call_command

    result = None
    if request.method == "POST":
        f = request.FILES.get("excel")
        if not f:
            messages.error(request, "파일을 선택해주세요.")
        elif not f.name.lower().endswith((".xlsx", ".xlsm")):
            messages.error(request, "엑셀 파일(.xlsx)만 업로드할 수 있습니다.")
        elif f.size > 20 * 1024 * 1024:
            messages.error(request, "20MB 이하 파일만 가능합니다.")
        else:
            _os.makedirs(_settings.UPLOAD_DIR, exist_ok=True)
            save_path = _os.path.join(_settings.UPLOAD_DIR, f.name)
            with open(save_path, "wb") as dest:
                for chunk in f.chunks():
                    dest.write(chunk)

            out = io.StringIO()
            try:
                call_command("import_els", file=save_path, stdout=out)
                result = out.getvalue().strip() or "처리 완료"
                # 새 상품이 들어왔으니 레이더 신호 주차 캐시 무효화
                # (안 하면 이번 주 배지·TOP5가 다음날까지 옛 데이터 기준)
                from core.models import _RADAR_POOL_CACHE
                _RADAR_POOL_CACHE.clear()
                messages.success(request, f"'{f.name}' 임포트 완료")
            except Exception as e:  # noqa: BLE001
                messages.error(request, f"임포트 오류: {e}")

    recent = ImportLog.objects.order_by("-imported_at")[:10]
    return render(request, "core/upload.html", {
        "result": result,
        "recent": recent,
        "active_nav": "upload",
    })


# ── 회원 관리 (운영자 전용) ─────────────────────────
@admin_required
def member_admin(request):
    from django.contrib.auth import get_user_model
    User = get_user_model()

    if request.method == "POST":
        target = get_object_or_404(User, pk=request.POST.get("id"))
        action = request.POST.get("action")
        if target.is_superuser:
            messages.error(request, "운영자 계정은 변경할 수 없습니다.")
        elif action == "toggle_active":
            target.is_active = not target.is_active
            target.save()
            messages.success(request, f"{target.username} 계정을 {'활성화' if target.is_active else '비활성화'}했습니다.")
        elif action == "toggle_staff":
            target.is_staff = not target.is_staff
            target.save()
            messages.success(request, f"{target.username} 계정을 {'가족(staff)으로 지정' if target.is_staff else '일반회원으로 변경'}했습니다.")
        return redirect("member_admin")

    # 회원 수가 적어 파이썬 집계 (다중 조인 annotate의 Sum 부풀림 회피)
    members = list(User.objects.order_by("-date_joined"))
    for m in members:
        held = m.investments.filter(status="보유중")
        m.inv_count = held.count()
        m.inv_total = sum(i.amount for i in held)
        m.watch_count = m.watch_items.count()
        m.preset_count = m.presets.count()
    return render(request, "core/members.html", {
        "members": members,
        "active_nav": "members",
    })


# ── PWA (홈화면 앱 설치) ─────────────────────────
def pwa_manifest(request):
    from django.http import JsonResponse
    return JsonResponse({
        "name": "ELS 레이더",
        "short_name": "ELS 레이더",
        "description": "매주 쏟아지는 ELS, 레이더가 대신 찾아드립니다",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#F2F4F6",
        "theme_color": "#3182F6",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    })


def pwa_icon(request, size):
    from pathlib import Path

    from django.conf import settings as _s
    from django.http import FileResponse, Http404
    if size not in ("180", "192", "512"):
        raise Http404
    path = Path(_s.BASE_DIR) / "core" / "assets" / f"icon-{size}.png"
    if not path.exists():
        raise Http404
    resp = FileResponse(open(path, "rb"), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=86400"
    return resp


def threads_card(request, day):
    """스레드 게시용 데이터 카드 이미지.

    Meta 서버가 게시 시점에 이 URL을 직접 가져간다. 인증을 걸면 안 되고,
    캐시를 길게 줘야 재게시 때 원본 서버 부담이 없다.
    """
    from pathlib import Path

    from django.conf import settings as _s
    from django.http import FileResponse, Http404
    path = Path(_s.BASE_DIR) / "core" / "assets" / "threads" / f"card_{day:02d}.png"
    if not path.exists():
        raise Http404
    resp = FileResponse(open(path, "rb"), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=604800"
    return resp


def csrf_failure(request, reason=""):
    """CSRF 검증 실패 커스텀 처리 (settings.CSRF_FAILURE_VIEW).

    로그인 성공이 CSRF 토큰을 회전시킨 직후 같은 폼이 다시 제출되면
    (모바일 연타·브라우저 재전송) 옛 토큰이라 403이 난다. 이 경우
    사용자는 이미 로그인돼 있으므로 에러 대신 홈으로 보낸다.
    비로그인 실패(오래 열린 페이지 등)만 새로고침 안내 페이지를 보여준다.
    """
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    return render(request, "core/csrf_failure.html", status=403)


# ── 웹 푸시 구독 ─────────────────────────────────
def service_worker(request):
    """서비스 워커 스크립트 — 루트(/sw.js)에서 서빙해야 스코프가 사이트 전체가 된다."""
    from pathlib import Path

    from django.http import FileResponse, Http404
    path = Path(settings.BASE_DIR) / "core" / "assets" / "sw.js"
    if not path.exists():
        raise Http404
    resp = FileResponse(open(path, "rb"), content_type="application/javascript")
    resp["Cache-Control"] = "no-cache"  # 워커 수정이 다음 방문에 바로 반영되도록
    return resp


def _push_login_required(view):
    """푸시 엔드포인트 전용 로그인 검사.

    이 세 뷰는 fetch()만 호출한다. login_required는 미로그인 시 로그인 페이지로
    302 리다이렉트하는데, fetch는 리다이렉트를 따라가 200(로그인 HTML)을 받는다.
    그러면 클라이언트의 res.ok가 true가 되어 저장되지도 않은 구독을 '성공'으로
    표시한다 — 리다이렉트 대신 403 JSON을 돌려줘야 클라이언트가 실패를 안다.
    """
    from functools import wraps

    from django.http import JsonResponse

    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "auth"}, status=403)
        return view(request, *args, **kwargs)

    return _wrapped


@_push_login_required
@require_POST
def push_subscribe(request):
    """브라우저가 발급받은 푸시 구독 저장. 같은 endpoint 재등록 시 소유자·키 갱신."""
    import json

    from django.http import JsonResponse

    from .models import PushSubscription
    try:
        data = json.loads(request.body)
        endpoint = data["endpoint"]
        p256dh = data["keys"]["p256dh"]
        auth = data["keys"]["auth"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False}, status=400)
    if not endpoint.startswith("https://"):
        return JsonResponse({"ok": False}, status=400)
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"user": request.user, "p256dh": p256dh, "auth": auth,
                  "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200]},
    )
    return JsonResponse({"ok": True})


@_push_login_required
@require_POST
def push_unsubscribe(request):
    """알림 끄기 — endpoint 소유(브라우저)가 곧 증명이라 endpoint 기준으로 삭제."""
    import json

    from django.http import JsonResponse

    from .models import PushSubscription
    try:
        endpoint = json.loads(request.body)["endpoint"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False}, status=400)
    PushSubscription.objects.filter(endpoint=endpoint).delete()
    return JsonResponse({"ok": True})


@_push_login_required
@require_POST
def push_test(request):
    """구독 직후 확인용 테스트 알림 1건."""
    from django.http import JsonResponse

    from . import push
    n = push.send_to_user(
        request.user, "알림이 켜졌어요",
        "조기상환 평가 D-7·D-1, 관심상품 청약마감 전날 아침에 알려드립니다.",
        url="/portfolio/",
    )
    return JsonResponse({"ok": n > 0, "sent": n})


def og_image(request):
    """링크 공유 미리보기 이미지 (1200x630, base.html og:image가 참조)."""
    from pathlib import Path

    from django.conf import settings as _s
    from django.http import FileResponse, Http404
    path = Path(_s.BASE_DIR) / "core" / "assets" / "og.png"
    if not path.exists():
        raise Http404
    resp = FileResponse(open(path, "rb"), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=86400"
    return resp


# ── 상품 검색 (공개) ─────────────────────────────
def product_search(request):
    from django.db.models import Q
    from django.conf import settings as dj_settings
    from . import ai_research

    q = request.GET.get("q", "").strip()
    results = []
    if q:
        results = list(
            Product.objects.filter(
                Q(product_no__icontains=q) | Q(issuer__icontains=q)
                | Q(assets_raw__icontains=q) | Q(name__icontains=q)
            ).order_by("-sub_end")[:200]
        )

    # ── AI 검색: 자연어 → 필터 → 기존 쿼리 실행 (답변 생성 없음) ──
    aiq = request.GET.get("aiq", "").strip()
    ai = None
    if aiq:
        flt, err = ai_research.ask(aiq)
        if err:
            ai = {"q": aiq, "error": err}
        else:
            rows, err = ai_research.run_filter(flt, request.user)
            ai = {"q": aiq, "error": err,
                  "results": rows or [], "chips": ai_research.describe(flt)}

    invested_ids = set()
    if request.user.is_authenticated:
        invested_ids = set(Investment.objects.filter(
            user=request.user, status="보유중").values_list("product_id", flat=True))
    return render(request, "core/search.html", {
        "q": q, "results": results, "invested_ids": invested_ids,
        "ai": ai, "ai_enabled": bool(dj_settings.ANTHROPIC_API_KEY),
        "active_nav": "search",
    })


# ── 홈 (/) — 로그인 여부 무관 랜딩 ──
def home(request):
    return about(request)


# ── 소개 랜딩 (공개) ─────────────────────────────
_ABOUT_CACHE = {"day": None, "ctx": None}


def _about_accuracy():
    """레이더 신호 성과검증 집계 — 배지 상품 vs 배지 없는 상품의 평가일 통과율."""
    from django.db.models import Count, Max, Min, Q

    from .models import RadarVerdict

    qs = RadarVerdict.objects.filter(met__isnull=False)
    rows = qs.values("tier").annotate(n=Count("id"), ok=Count("id", filter=Q(met=True)))
    badge_n = badge_ok = ctrl_n = ctrl_ok = 0
    for r in rows:
        if r["tier"] in ("타겟 신호", "아주 강한 신호", "강한 신호"):
            badge_n += r["n"]
            badge_ok += r["ok"]
        else:
            ctrl_n += r["n"]
            ctrl_ok += r["ok"]
    if not badge_n or not ctrl_n:
        return None
    span = qs.aggregate(a=Min("week_monday"), b=Max("week_monday"))
    badge_pct = badge_ok / badge_n * 100
    ctrl_pct = ctrl_ok / ctrl_n * 100
    return {
        "badge_n": badge_n, "badge_ok": badge_ok, "badge_pct": round(badge_pct, 1),
        "ctrl_n": ctrl_n, "ctrl_ok": ctrl_ok, "ctrl_pct": round(ctrl_pct, 1),
        "delta": round(badge_pct - ctrl_pct, 1),
        "total_n": badge_n + ctrl_n, "start": span["a"], "end": span["b"],
        # 막대 높이(%) — 60% 기준선 축으로 차이를 시각적으로 벌린다 (값은 라벨에 명시)
        "badge_h": round(max(8.0, (badge_pct - 60) / 40 * 90), 1),
        "ctrl_h": round(max(8.0, (ctrl_pct - 60) / 40 * 90), 1),
    }


def _about_ki_series():
    """SEIBro 표본의 연도별 평균 낙인 배리어 → 꺾은선 좌표(viewBox 620x190)."""
    from django.db.models import Avg, Count
    from django.db.models.functions import ExtractYear

    from .models import HistoricalIssue

    rows = list(HistoricalIssue.objects.filter(ki__isnull=False)
                .annotate(y=ExtractYear("issue_date")).values("y")
                .annotate(n=Count("id"), avg=Avg("ki")).order_by("y"))
    rows = [r for r in rows if r["y"] and r["n"] >= 30]   # 표본 30건 미만 연도는 제외
    if len(rows) < 5:
        return None

    x0, x1, y_top, y_bot = 46.0, 596.0, 22.0, 150.0      # 그리기 영역
    lo, hi = 40.0, 62.0                                   # y축 범위(%)
    step = (x1 - x0) / (len(rows) - 1)

    def _y(v):
        return round(y_bot - (v - lo) / (hi - lo) * (y_bot - y_top), 1)

    pts = []
    for i, r in enumerate(rows):
        pts.append({"x": round(x0 + step * i, 1), "y": _y(r["avg"]),
                    "year": r["y"], "avg": round(r["avg"], 1), "n": r["n"]})
    return {
        "points": " ".join(f"{p['x']},{p['y']}" for p in pts),
        "first": pts[0], "last": pts[-1], "marks": [pts[0], pts[len(pts) // 2], pts[-1]],
        "sample_n": sum(r["n"] for r in rows),
        "grid": [{"v": v, "y": _y(v)} for v in (60, 55, 50, 45)],
        "drop": round(max(p["avg"] for p in pts) - pts[-1]["avg"], 1),
    }


def _about_live():
    """운영자 실계좌 상환 실적 집계 — 금액은 노출하지 않고 비율·건수만."""
    done = [i for i in Investment.objects.filter(
        status__in=["조기상환", "만기상환", "낙인후상환"],
        redeemed_amount__isnull=False, redeemed_at__isnull=False)
        if i.amount and (i.redeemed_at - i.invested_at).days > 0]
    if not done:
        return None
    anns, months, wins = [], [], 0
    for i in done:
        days = (i.redeemed_at - i.invested_at).days
        r = (i.redeemed_amount - i.amount) / i.amount
        anns.append(r * 365 / days)
        months.append(days / 30.4)
        if r > 0:
            wins += 1
    n = len(done)
    return {"n": n, "win": wins,
            "ann": round(sum(anns) / n * 100, 1),
            "avg_m": round(sum(months) / n, 1)}


def about(request):
    """소개 랜딩 — 실측 숫자는 하루 1회만 계산해 캐시(마케팅 유입 대비)."""
    today = date.today()
    if _ABOUT_CACHE["day"] == today and _ABOUT_CACHE["ctx"]:
        ctx = _ABOUT_CACHE["ctx"]
    else:
        from .models import radar_top5
        top5 = radar_top5()
        if not top5:                       # 마감이 지나 이번주가 비면 지난주 것으로
            last_mon = today - timedelta(days=today.weekday() + 7)
            top5 = radar_top5(last_mon, last_mon + timedelta(days=6))
        # stat_*·live는 랜딩 개편(2026-07-31)으로 화면에서 빠짐 — _about_live는 실전 인증
        # 섹션 재삽입 때 다시 쓸 예정이라 헬퍼는 남겨둔다.
        ctx = {
            "accuracy": _about_accuracy(),
            "ki": _about_ki_series(),
            "top5": top5,
        }
        _ABOUT_CACHE.update(day=today, ctx=ctx)
    return render(request, "core/about.html", {"active_nav": "about", **ctx})


# ── 법적 페이지 (공개) ────────────────────────────
def legal_terms(request):
    """이용약관."""
    return render(request, "core/legal_terms.html", {})


def legal_privacy(request):
    """개인정보처리방침."""
    return render(request, "core/legal_privacy.html", {})


def legal_disclaimer(request):
    """투자 유의사항(면책)."""
    return render(request, "core/legal_disclaimer.html", {})


# ── 회원 탈퇴 ────────────────────────────────────
@login_required
def account_delete(request):
    """계정 및 관련 데이터 완전 삭제.

    Investment / WatchItem / Preset 은 모두 user FK on_delete=CASCADE 이므로
    user.delete() 한 번으로 함께 삭제된다.
    실수 방지를 위해 비밀번호 + 확인 문구("탈퇴합니다") 두 가지를 요구한다.
    """
    from django.contrib.auth import logout as auth_logout

    user = request.user
    # 운영자 계정은 이 화면으로 삭제 불가 (실수로 지우면 관리 권한 복구 불가)
    if user.is_superuser:
        messages.error(request, "운영자 계정은 이 화면에서 탈퇴할 수 없습니다.")
        return redirect("weekly")

    ctx = {
        "inv_count": Investment.objects.filter(user=user).count(),
        "watch_count": WatchItem.objects.filter(user=user).count(),
        "preset_count": Preset.objects.filter(user=user).count(),
    }

    if request.method == "POST":
        password = request.POST.get("password", "")
        confirm = (request.POST.get("confirm") or "").strip()
        if not user.check_password(password):
            ctx["error"] = "비밀번호가 올바르지 않습니다."
            return render(request, "core/account_delete.html", ctx)
        if confirm != "탈퇴합니다":
            ctx["error"] = "확인 문구가 일치하지 않습니다. '탈퇴합니다' 를 그대로 입력해 주세요."
            return render(request, "core/account_delete.html", ctx)

        auth_logout(request)          # 세션 먼저 정리
        user.delete()                 # 계정 + CASCADE 데이터 삭제
        messages.success(request, "탈퇴가 완료되었습니다")
        return redirect("weekly")

    return render(request, "core/account_delete.html", ctx)


# ── 접속 통계 (운영자 전용) ───────────────────────
@admin_required
def stats(request):
    """최근 30일 방문·유입·가입 집계."""
    from django.contrib.auth import get_user_model
    from django.db.models import Count

    from .models import PageView

    today = date.today()
    start = today - timedelta(days=29)
    qs = PageView.objects.filter(date__gte=start)

    daily = {r["date"]: r for r in qs.values("date").annotate(
        views=Count("id"), uniq=Count("visitor", distinct=True))}
    srows = (get_user_model().objects.filter(date_joined__date__gte=start)
             .values("date_joined__date").annotate(n=Count("id")))
    signup_map = {r["date_joined__date"]: r["n"] for r in srows}

    days, max_uniq = [], 1
    for i in range(30):
        d = start + timedelta(days=i)
        r = daily.get(d, {})
        u = r.get("uniq", 0)
        max_uniq = max(max_uniq, u)
        days.append({"d": d, "views": r.get("views", 0), "uniq": u,
                     "signups": signup_map.get(d, 0)})
    for row in days:
        row["h"] = round(row["uniq"] / max_uniq * 100, 1)

    ctx = {
        "days": days, "start": start, "today": today,
        "top_paths": qs.values("path").annotate(n=Count("id")).order_by("-n")[:12],
        "top_refs": (qs.exclude(ref="").values("ref")
                     .annotate(n=Count("visitor", distinct=True)).order_by("-n")[:10]),
        "totals": {
            "views": qs.count(),
            "uniq": qs.values("visitor").distinct().count(),
            "signups": sum(r["signups"] for r in days),
            "today_views": days[-1]["views"], "today_uniq": days[-1]["uniq"],
        },
    }
    return render(request, "core/stats.html", ctx)
