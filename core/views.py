import calendar as pycalendar
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .models import ImportLog, Investment, Preset, Product, WatchItem


def _week_range(offset: int = 0):
    """offset주 뒤의 (월요일, 일요일)."""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=6)


# ── 주간 청약 (메인) ──────────────────────────────
@login_required
def weekly(request):
    offset = int(request.GET.get("w", 0))
    monday, sunday = _week_range(offset)

    qs = Product.objects.filter(sub_end__gte=monday, sub_end__lte=sunday)

    # 필터
    f_asset = request.GET.get("asset", "")
    f_ki_max = request.GET.get("ki_max", "")
    f_yield_min = request.GET.get("yield_min", "")
    f_currency = request.GET.get("currency", "")
    f_no_ki = request.GET.get("no_ki", "")
    preset_id = request.GET.get("preset", "")

    if preset_id:
        try:
            preset = Preset.objects.get(id=preset_id)
            qs = preset.match_queryset(qs)
        except Preset.DoesNotExist:
            pass
    else:
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

    # 마감일별 그룹핑
    products = list(qs.order_by("sub_end", "-yield_rate"))
    groups = {}
    for p in products:
        groups.setdefault(p.sub_end, []).append(p)
    day_groups = [
        {"date": d, "weekday": "월화수목금토일"[d.weekday()], "products": plist}
        for d, plist in sorted(groups.items())
    ]

    watched_ids = set(WatchItem.objects.values_list("product_id", flat=True))
    last_import = ImportLog.objects.first()
    freshness_days = None
    if last_import:
        freshness_days = (date.today() - last_import.imported_at.date()).days

    return render(request, "core/weekly.html", {
        "day_groups": day_groups,
        "monday": monday, "sunday": sunday, "offset": offset,
        "total": len(products),
        "presets": Preset.objects.all(),
        "watched_ids": watched_ids,
        "freshness_days": freshness_days,
        "filters": {
            "asset": f_asset, "ki_max": f_ki_max, "yield_min": f_yield_min,
            "currency": f_currency, "no_ki": f_no_ki, "preset": preset_id,
        },
        "active_nav": "weekly",
    })


# ── 상품 상세 ─────────────────────────────────────
@login_required
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    is_watched = WatchItem.objects.filter(product=product).exists()

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

    return render(request, "core/product_detail.html", {
        "product": product, "is_watched": is_watched, "svg": svg,
        "active_nav": "weekly",
    })


# ── 프리셋 관리 ───────────────────────────────────
@login_required
def presets(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            Preset.objects.filter(id=request.POST.get("id")).delete()
            messages.success(request, "프리셋을 삭제했습니다.")
        else:  # create / update
            pid = request.POST.get("id")
            data = dict(
                name=request.POST.get("name", "").strip() or "이름없음",
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
                Preset.objects.filter(id=pid).update(**data)
                messages.success(request, "프리셋을 수정했습니다.")
            else:
                Preset.objects.create(**data)
                messages.success(request, "프리셋을 추가했습니다.")
        return redirect("presets")

    today = date.today()
    active_products = Product.objects.filter(sub_end__gte=today)
    preset_list = []
    for p in Preset.objects.all():
        preset_list.append({"obj": p, "match_count": p.match_queryset(active_products).count()})

    return render(request, "core/presets.html", {
        "preset_list": preset_list, "active_nav": "presets",
    })


# ── 관심 목록 ─────────────────────────────────────
@login_required
def watchlist(request):
    if request.method == "POST":
        action = request.POST.get("action")
        product = get_object_or_404(Product, pk=request.POST.get("product_id"))
        if action == "add":
            WatchItem.objects.get_or_create(product=product)
            messages.success(request, "관심 목록에 등록했습니다.")
        elif action == "remove":
            WatchItem.objects.filter(product=product).delete()
            messages.success(request, "관심 목록에서 해제했습니다.")
        return redirect(request.POST.get("next") or "watchlist")

    items = WatchItem.objects.select_related("product").all()
    return render(request, "core/watchlist.html", {
        "items": items, "active_nav": "watchlist",
    })


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
            WatchItem.objects.filter(product=product).delete()
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
        return redirect("portfolio")

    invs = Investment.objects.filter(user=request.user).select_related("product")
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

    # 투자 등록 폼용 상품 후보 (최근 청약 상품)
    candidates = Product.objects.filter(
        sub_end__gte=today - timedelta(days=30)
    ).order_by("-sub_end", "issuer")[:200]

    return render(request, "core/portfolio.html", {
        "holding": holding, "done": done,
        "total_invested": total_invested,
        "holding_count": len(holding),
        "this_month_evals": this_month_evals,
        "total_redeemed_profit": total_redeemed_profit,
        "candidates": candidates,
        "today": today,
        "active_nav": "portfolio",
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
                })

    cal = pycalendar.Calendar(firstweekday=0)  # 월요일 시작
    weeks = []
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            row.append({
                "day": day or "",
                "is_today": bool(day) and date(year, month, day) == today,
                "events": events.get(day, []) if day else [],
            })
        weeks.append(row)

    return render(request, "core/calendar.html", {
        "year": year, "month": month, "weeks": weeks,
        "prev_y": prev_y, "prev_m": prev_m, "next_y": next_y, "next_m": next_m,
        "event_count": sum(len(v) for v in events.values()),
        "active_nav": "calendar",
    })
