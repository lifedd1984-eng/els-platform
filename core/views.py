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
    f_issuer = request.GET.get("issuer", "")
    preset_id = request.GET.get("preset", "")

    if preset_id:
        try:
            preset = Preset.objects.get(id=preset_id)
            qs = preset.match_queryset(qs)
        except Preset.DoesNotExist:
            pass
    else:
        if f_issuer:
            qs = qs.filter(issuer=f_issuer)
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

    # 발행사 필터 후보 (이번 주 상품에 존재하는 발행사)
    issuers = sorted(set(
        Product.objects.filter(sub_end__gte=monday, sub_end__lte=sunday)
        .values_list("issuer", flat=True)
    ))

    return render(request, "core/weekly.html", {
        "day_groups": day_groups,
        "monday": monday, "sunday": sunday, "offset": offset,
        "total": len(products),
        "presets": Preset.objects.all(),
        "issuers": issuers,
        "watched_ids": watched_ids,
        "freshness_days": freshness_days,
        "filters": {
            "asset": f_asset, "ki_max": f_ki_max, "yield_min": f_yield_min,
            "currency": f_currency, "no_ki": f_no_ki, "preset": preset_id,
            "issuer": f_issuer,
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
                issuer=request.POST.get("issuer", "").strip(),
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

    # 세후 예상 수익 (보유분이 1차 평가에 전부 조기상환된다고 가정)
    total_expected_after_tax = sum(
        (i.first_eval_after_tax or i.amount) for i in holding
    )
    expected_profit_after_tax = total_expected_after_tax - total_invested

    # ── 리스크 분석 ──────────────────────────────
    risk = _analyze_risk(holding, total_invested)

    # ── 낙인 모니터링 갱신 시각 ──
    ki_updated = None
    for inv in holding:
        for s in inv.ki_status.all():
            if s.updated_at and (ki_updated is None or s.updated_at > ki_updated):
                ki_updated = s.updated_at
    has_ki_data = ki_updated is not None

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
        "total_expected_after_tax": total_expected_after_tax,
        "expected_profit_after_tax": expected_profit_after_tax,
        "risk": risk,
        "ki_updated": ki_updated,
        "has_ki_data": has_ki_data,
        "candidates": candidates,
        "today": today,
        "active_nav": "portfolio",
    })


# ── 시장 트렌드 ───────────────────────────────────
@login_required
def market_trend(request):
    """주차별 평균 수익률·KI 추이 (sub_end 기준, 최근 12주)."""
    from collections import defaultdict

    weeks_n = 12
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


# ── 엑셀 업로드 ──────────────────────────────────
@login_required
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
