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
WEEKLY_FILTER_PARAMS = ["asset", "ki_max", "yield_min", "currency",
                        "no_ki", "issuer", "preset", "sort", "dir"]


@login_required
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
        "products": products,
        "columns": columns,
        "monday": monday, "sunday": sunday, "offset": offset,
        "total": len(products),
        "presets": Preset.objects.all(),
        "issuers": issuers,
        "watched_ids": watched_ids,
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

    # 수익률 모의실험 결과 (배치가 저장한 캐시)
    sim = product.sim_result or None

    # 이 상품을 보유 중이면 상품별 낙인 모니터링 (자산별 레벨/버퍼)
    from core.models import KnockInStatus
    ki_statuses = None
    ki_worst_buffer = None
    ki_updated_at = None
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

    return render(request, "core/product_detail.html", {
        "product": product, "is_watched": is_watched, "svg": svg,
        "sim": sim, "sim_updated": product.sim_updated,
        "ki_statuses": ki_statuses, "ki_worst_buffer": ki_worst_buffer,
        "ki_updated_at": ki_updated_at,
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
            ("next", "다음 평가일", False), ("pretax", "예상상환금", True),
            ("loss", "손실확률", True),
        ]
    ]

    return render(request, "core/portfolio.html", {
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
        ["ELS 플랫폼 — 투자내역 일괄등록 양식"],
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


def _match_product_for_investment(issuer, product_no):
    """발행사+상품번호로 Product 매칭 (여러 개면 최근 sub_end)."""
    qs = Product.objects.filter(
        issuer=str(issuer).strip(), product_no=str(product_no).strip()
    )
    return qs.order_by("-sub_end").first()


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
            errors.append(f"{i}행: '{issuer} {product_no}' 상품을 찾을 수 없습니다.")
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
