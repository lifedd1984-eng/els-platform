import calendar as pycalendar
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import LoginView
from django.shortcuts import get_object_or_404, redirect, render

from .models import ImportLog, Investment, Preset, Product, WatchItem

# 가족(운영진) 전용 — 공유 데이터(관심·프리셋·업로드)는 staff 계정만
family_required = user_passes_test(lambda u: u.is_active and u.is_staff, login_url="/accounts/login/")

# 운영자(superuser) 전용 — 회원 관리
admin_required = user_passes_test(lambda u: u.is_active and u.is_superuser, login_url="/accounts/login/")


def _scope(qs, user):
    """프리셋/관심 소유 범위: 가족(staff)=공용(user=None)+본인, 일반회원=본인 것만."""
    from django.db.models import Q
    if not user.is_authenticated:
        return qs.none()
    if user.is_staff:
        return qs.filter(Q(user__isnull=True) | Q(user=user))
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


def weekly(request):
    # ── 필터 세션 저장/복원 ──
    if "reset" in request.GET:
        request.session.pop("weekly_filters", None)
        return redirect("weekly")

    # 빈 URL(메뉴 클릭)로 오면 저장된 필터 복원
    if not request.GET and request.session.get("weekly_filters"):
        return redirect("/?" + request.session["weekly_filters"])

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
    sort_key = request.GET.get("sort", "sub_end")
    if sort_key != "term" and sort_key not in SORT_FIELDS:
        sort_key = "sub_end"
    sort_dir = request.GET.get("dir", "asc")
    if sort_key == "term":
        # term_months는 계산 property(DB 컬럼 아님) → Python 정렬. None은 항상 뒤로.
        # 정렬 방향과 무관하게 None이 끝에 오도록 sentinel 사용.
        desc = sort_dir == "desc"
        sentinel = float("-inf") if desc else float("inf")
        products = list(qs.order_by("-yield_rate"))
        products.sort(
            key=lambda p: p.term_months if p.term_months is not None else sentinel,
            reverse=desc,
        )
    else:
        field = SORT_FIELDS[sort_key]
        ordering = (F(field).desc(nulls_last=True) if sort_dir == "desc"
                    else F(field).asc(nulls_last=True))
        products = list(qs.order_by(ordering, "-yield_rate"))

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
        ("ki", "KI", True), ("first", "1차", True), ("last", "막차", True),
        ("term", "기간", True), ("period", "주기", True), ("loss", "손실확률", True),
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

    # 발행사 필터 후보 (이번 주 상품에 존재하는 발행사)
    issuers = sorted(set(
        Product.objects.filter(sub_end__gte=monday, sub_end__lte=sunday)
        .values_list("issuer", flat=True)
    ))

    # ── 이번주 추천 TOP5 (현재 주만) ──
    # 추천점수 = 연수익률 × (1 - 손실확률/100) — 손실 반영 기대수익률.
    # 중복도 = 보유 포트폴리오 중 같은 기초자산을 가진 투자금 비중.
    recommendations = []
    if offset >= 0:  # 지난 주 조회 시에는 표시 안 함
        from core import market as _mkt

        def _asset_keys(raw):
            return {_mkt.resolve_ticker(a) or a for a in _mkt.split_assets(raw)}

        inv_assets = [
            (inv.amount, _asset_keys(inv.product.assets_raw))
            for inv in Investment.objects.filter(status="보유중").select_related("product")
        ]
        total_held = sum(amt for amt, _ in inv_assets)

        pool = Product.objects.filter(
            sub_end__gte=max(monday, date.today()), sub_end__lte=sunday,
            barriers_raw__isnull=False, yield_rate__isnull=False,
            loss_prob__isnull=False,
        )
        # 중복도는 가족(staff) 계정에만 표시 — 외부인에게 보유 성향 노출 방지
        show_overlap = request.user.is_authenticated and request.user.is_staff
        scored = []
        for p in pool:
            score = round(p.yield_rate * (1 - p.loss_prob / 100), 2)
            overlap_pct = None
            if show_overlap:
                pkeys = _asset_keys(p.assets_raw)
                overlap = sum(amt for amt, keys in inv_assets if keys & pkeys)
                overlap_pct = round(overlap / total_held * 100) if total_held else 0
            scored.append({"p": p, "score": score, "overlap_pct": overlap_pct})
        scored.sort(key=lambda r: -r["score"])
        recommendations = scored[:5]

    return render(request, "core/weekly.html", {
        "products": products,
        "recommendations": recommendations,
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
    for asset in assets[:4]:
        t = _mkt.resolve_ticker(asset)
        if not t:
            continue
        hist = _mkt.fetch_history(t)
        if len(hist) < 10:
            continue
        ref = None
        if product.issue_date:
            past = [c for d, c in hist if d <= product.issue_date]
            ref = past[-1] if past else None
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
            chart_lines.append({"label": f"KI {product.ki:g}",
                                "y": _y(product.ki), "color": "#e03131", "dash": "2 3"})
        chart = {"W": W, "H": H, "series": chart_series, "lines": chart_lines,
                 "based_on_issue": bool(product.issue_date)}

    return render(request, "core/product_detail.html", {
        "product": product, "is_watched": is_watched, "svg": svg,
        "sim": sim, "sim_updated": product.sim_updated,
        "ki_statuses": ki_statuses, "ki_worst_buffer": ki_worst_buffer,
        "ki_updated_at": ki_updated_at,
        "chart": chart,
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
                if not request.user.is_staff:
                    data["user"] = request.user
                    data["notify"] = False  # 텔레그램은 가족 채널 전용
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
        product = get_object_or_404(Product, pk=request.POST.get("product_id"))
        if action == "add":
            WatchItem.objects.get_or_create(
                product=product,
                user=None if request.user.is_staff else request.user)
            messages.success(request, "관심 목록에 등록했습니다.")
        elif action == "remove":
            _scope(WatchItem.objects.filter(product=product), request.user).delete()
            messages.success(request, "관심 목록에서 해제했습니다.")
        return redirect(request.POST.get("next") or "watchlist")

    items = _scope(WatchItem.objects.select_related("product").all(), request.user)
    invested_ids = set(Investment.objects.filter(
        user=request.user, status="보유중").values_list("product_id", flat=True))
    return render(request, "core/watchlist.html", {
        "invested_ids": invested_ids,
        "items": items, "active_nav": "watchlist",
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
    asset_exposure = defaultdict(lambda: {"amount": 0, "count": 0})
    issuer_exposure = defaultdict(lambda: {"amount": 0, "count": 0})
    maturity_buckets = defaultdict(int)  # 'YYYY-MM' → 건수

    for inv in holding:
        p = inv.product
        for asset in _split_assets(p.assets_raw):
            asset_exposure[asset]["amount"] += inv.amount
            asset_exposure[asset]["count"] += 1
        issuer_exposure[p.issuer]["amount"] += inv.amount
        issuer_exposure[p.issuer]["count"] += 1
        nxt = inv.next_evaluation
        if nxt:
            maturity_buckets[nxt["date"].strftime("%Y-%m")] += 1

    def _top(exposure):
        rows = [
            {"name": k, "amount": v["amount"], "count": v["count"],
             "pct": round(v["amount"] / total_invested * 100)}
            for k, v in exposure.items()
        ]
        return sorted(rows, key=lambda r: -r["amount"])

    assets = _top(asset_exposure)
    issuers = _top(issuer_exposure)

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
    # 만기 집중: 한 달에 60% 초과 평가 몰림
    if maturity_buckets:
        top_month, top_cnt = max(maturity_buckets.items(), key=lambda x: x[1])
        if top_cnt / len(holding) > 0.6 and len(holding) >= 3:
            warnings.append(f"{top_month} 평가일에 상환이 몰려 있습니다 ({top_cnt}건).")

    return {
        "assets": assets[:6],
        "issuers": issuers[:5],
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
                invested_at=request.POST.get("invested_at") or date.today(),
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
        elif action == "delete":
            Investment.objects.filter(pk=request.POST.get("id"), user=request.user).delete()
            messages.success(request, "투자 기록을 삭제했습니다.")
        elif action == "bulk_delete":
            ids = request.POST.getlist("ids")
            n, _ = Investment.objects.filter(pk__in=ids, user=request.user).delete()
            messages.success(request, f"{n}건의 투자 기록을 삭제했습니다.")
        return redirect(request.POST.get("next") or "portfolio")

    invs = (Investment.objects.filter(user=request.user)
            .select_related("product").prefetch_related("ki_status"))
    holding = [i for i in invs if i.status == "보유중"]
    done = [i for i in invs if i.status != "보유중"]

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

    # ── 리스크 분석 ──────────────────────────────
    risk = _analyze_risk(holding, total_invested)

    # ── 낙인 모니터링: 전체 보유 중 위험/경고(버퍼 ≤ 15%p)만 추림 ──
    ki_updated = None
    ki_alerts = []
    for inv in holding:
        worst = inv.worst_ki_status
        for s in inv.ki_status.all():
            if s.updated_at and (ki_updated is None or s.updated_at > ki_updated):
                ki_updated = s.updated_at
        buf = inv.ki_buffer
        if worst is not None and buf is not None and buf <= 15:
            ki_alerts.append({"inv": inv, "worst": worst, "buffer": buf})
    ki_alerts.sort(key=lambda a: a["buffer"])  # 위험한 순
    has_ki_data = ki_updated is not None

    # 투자 등록 폼용 상품 후보 (최근 청약 상품)
    candidates = Product.objects.filter(
        sub_end__gte=today - timedelta(days=30)
    ).order_by("-sub_end", "issuer")[:200]

    # ── 보유 리스트 정렬 ──
    def _pretax(i):
        s = i.schedule
        return s[0]["expected"] if s else 0

    H_SORT = {
        "issuer": lambda i: (i.product.issuer or ""),
        "assets": lambda i: (i.product.assets_raw or ""),
        "amount": lambda i: i.amount or 0,
        "yield": lambda i: i.product.yield_rate if i.product.yield_rate is not None else -1,
        "issue": lambda i: (i.product.issue_date or date.max),
        "next": lambda i: (i.next_evaluation["date"] if i.next_evaluation else date.max),
        "pretax": _pretax,
        "loss": lambda i: (i.product.loss_prob if i.product.loss_prob is not None else -1),
    }
    h_sort = request.GET.get("hsort", "next")
    if h_sort not in H_SORT:
        h_sort = "next"
    h_dir = request.GET.get("hdir", "asc")
    holding.sort(key=H_SORT[h_sort], reverse=(h_dir == "desc"))

    def _hsort_url(key):
        d = "desc" if (h_sort == key and h_dir == "asc") else "asc"
        return f"?hsort={key}&hdir={d}&psize={page_size}"

    # ── 페이지네이션 ──
    from django.core.paginator import Paginator
    try:
        page_size = int(request.GET.get("psize", 20))
    except (ValueError, TypeError):
        page_size = 20
    if page_size not in (20, 50, 100):
        page_size = 20

    h_page = Paginator(holding, page_size).get_page(request.GET.get("hpage"))
    d_page = Paginator(done, page_size).get_page(request.GET.get("dpage"))

    h_cols = [
        {"key": k, "label": lbl, "num": num, "url": _hsort_url(k),
         "active": h_sort == k, "dir": h_dir}
        for k, lbl, num in [
            ("issuer", "상품", False), ("assets", "기초자산", False),
            ("amount", "투자금액", True), ("yield", "수익률", True),
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
        "perf": perf,
        "h_page": h_page, "d_page": d_page,
        "holding_count": len(holding), "done_count": len(done),
        "h_cols": h_cols, "page_size": page_size,
        "total_invested": total_invested,
        "this_month_evals": this_month_evals,
        "total_redeemed_profit": total_redeemed_profit,
        "total_expected_pretax": total_expected_pretax,
        "expected_profit_pretax": expected_profit_pretax,
        "port_loss_rate": port_loss_rate,
        "loss_coverage_pct": loss_coverage_pct,
        "risk": risk,
        "ki_updated": ki_updated,
        "has_ki_data": has_ki_data,
        "ki_alerts": ki_alerts,
        "candidates": candidates,
        "today": today,
        "active_nav": "portfolio",
    })


# ── 포트폴리오 엑셀 양식 다운로드 ─────────────────
PORTFOLIO_COLS = ["발행사", "상품번호", "투자금액(원)", "청약일(YYYY-MM-DD)", "증권사/계좌", "메모"]


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
        ["4. 청약일은 YYYY-MM-DD. 비우면 오늘 날짜로 등록됩니다."],
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

    cols = ["발행사", "상품번호", "기초자산", "수익률(%)", "KI", "1차", "막차",
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
    """현재 보유 투자내역을 xlsx로 다운로드."""
    import io

    import openpyxl
    from django.http import HttpResponse

    cols = ["발행사", "상품번호", "기초자산", "투자금액(원)", "수익률(%)", "KI",
            "주기(개월)", "1차까지(개월)", "다음평가일", "예상상환금", "손실확률(%)",
            "발행일", "만기일", "스케줄"]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "보유내역"
    ws.append(cols)

    invs = (Investment.objects.filter(user=request.user, status="보유중")
            .select_related("product"))
    holding = sorted(
        invs,
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
            (p.issue_date.strftime("%Y-%m-%d") if p.issue_date else ""),
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
    fname = f"ELS_보유내역_{date.today():%Y%m%d}.xlsx"
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
    except Exception as e:  # noqa: BLE001
        messages.error(request, f"파일을 읽을 수 없습니다: {e}")
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

        inv_date = _parse_invest_date(invested) or date.today()

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

    return render(request, "core/trend.html", {
        "rows": rows,
        "yield_pts": yield_pts, "yield_poly": _polyline(yield_pts),
        "ki_pts": ki_pts, "ki_poly": _polyline(ki_pts),
        "y_lo": y_lo, "y_hi": y_hi, "k_lo": k_lo, "k_hi": k_hi,
        "W": W, "H": H, "trend": trend,
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

    return render(request, "core/calendar.html", {
        "year": year, "month": month, "weeks": weeks,
        "prev_y": prev_y, "prev_m": prev_m, "next_y": next_y, "next_m": next_m,
        "event_count": sum(len(v) for v in events.values()),
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
