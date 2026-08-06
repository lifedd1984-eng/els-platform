"""AI 분석 질문(/ask/) 도구 실행부 — 답변에 쓰이는 숫자는 전부 여기서 나온다.

설계 원칙
  1. 모델은 숫자를 만들지 않는다. 도구가 **원본값 + 표시용 문자열**을 함께 돌려주고,
     모델은 display 문자열을 그대로 옮겨 쓴다. 사후검사가 그 대조를 강제한다.
  2. 커버리지 밖 요청은 빈 결과가 아니라 **사유 코드**를 돌려준다. 빈 배열을 주면
     모델이 "없다"를 자기 말로 지어낸다.
  3. 포트폴리오·조건검색은 **기존 코드를 재사용**한다 (portfolio_facts / ai_research).
     여기서 다시 구현하면 화면과 답이 갈라진다.
"""

import json
import logging
import math
import re
import statistics
from datetime import date, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# 사유 코드 — 도구가 답을 못 줄 때 쓰는 어휘. 모델이 지어내지 않게 고정한다.
OUT_OF_RANGE = "OUT_OF_RANGE"       # 요청 구간이 보유 구간을 아예 벗어남
PARTIAL_RANGE = "PARTIAL_RANGE"     # 일부만 겹침 — 겹치는 구간으로 답하고 알린다
NO_PRICE_DATA = "NO_PRICE_DATA"     # 티커는 알지만 시세가 없음
NO_ASSET = "NO_ASSET"               # 자산명을 해석하지 못함
NO_DATA = "NO_DATA"                 # 조건에 맞는 집계 자료가 없음
OUT_OF_SCOPE = "OUT_OF_SCOPE"       # 서비스가 다루지 않는 종류의 질문

TRADING_DAYS = 252   # 연율 변동성 환산 계수

METRICS = ("cumulative_return", "cagr", "mdd", "mdd_detail", "volatility",
           "worst_day", "best_day", "level_vs_high", "level_vs_low",
           "period_return", "days_below", "recovery_days")

# 자산명 → PriceBar 티커 보정.
# ⚠ market.resolve_ticker("KOSPI200")는 ETF(069500.KS)를 준다. 낙인 판정은 그게
#   맞지만 "코스피200 수익률"의 답은 지수 계열(^KS200)이다. PriceBar에는 둘 다
#   있으므로 /ask/에서만 지수 쪽으로 돌린다 (낙인 계산 경로는 건드리지 않는다).
_INDEX_OVERRIDE = {
    "KOSPI200": "^KS200", "KOSPI 200": "^KS200", "코스피200": "^KS200",
    "코스피 200": "^KS200", "KOSPI200 INDEX": "^KS200",
}


# ══════════════════════════════════════════════════════════════════
# 표시용 포맷 — display 문자열은 반드시 여기를 거친다
# ══════════════════════════════════════════════════════════════════

def num(value, display):
    return None if value is None else {"value": value, "display": display}


def pct(v, digits=1, sign=False):
    if v is None:
        return None
    s = f"{v:+,.{digits}f}%" if sign else f"{v:,.{digits}f}%"
    return num(round(v, digits), s)


def pp(v, digits=1):
    return None if v is None else num(round(v, digits), f"{v:,.{digits}f}%p")


def price(ticker, v):
    """티커별 통화·자릿수. 지수는 포인트, 국내는 원, 해외는 달러."""
    if v is None:
        return None
    if ticker.startswith("^"):
        return num(round(v, 2), f"{v:,.2f}")
    if ticker.endswith((".KS", ".KQ")):
        return num(round(v), f"{v:,.0f}원")
    return num(round(v, 2), f"${v:,.2f}")


def _cnt(n, unit="건"):
    return num(n, f"{n:,}{unit}")


# ══════════════════════════════════════════════════════════════════
# 커버리지 스냅샷 — 하루 한 번만 계산해 프롬프트에 고정 문자열로 박는다
# ══════════════════════════════════════════════════════════════════
# ⚠ 매 요청 DB에서 렌더링하면 프롬프트 프리픽스가 매번 달라져 캐시가 통째로
#   깨진다. 시:분을 넣으면 캐싱이 아예 무의미해진다. 날짜 단위로만 갱신한다.
_COV = {"day": None, "data": None}


def coverage(force=False):
    from .models import PRINCIPAL_PROTECTED_TYPES, HistoricalYieldStat, PriceBar, Product

    today = timezone.localdate()
    if not force and _COV["day"] == today and _COV["data"]:
        return _COV["data"]

    from django.db.models import Max, Min

    agg = PriceBar.objects.aggregate(a=Min("date"), b=Max("date"))
    # ⚠ .order_by()로 기본 정렬을 지우고 distinct 해야 한다. PriceBar.Meta.ordering이
    #   ["ticker","date"]라 그냥 distinct()를 걸면 date가 SELECT에 딸려 들어가
    #   (ticker, date) 조합이 유일해져 10만 개가 나온다 — 프롬프트에 그대로 실린다.
    tickers = sorted(PriceBar.objects.order_by()
                     .values_list("ticker", flat=True).distinct())
    domestic = [t for t in tickers if t.endswith((".KS", ".KQ"))]
    years = HistoricalYieldStat.objects.aggregate(a=Min("year"), b=Max("year"))
    data = {
        "day": today.isoformat(),
        "price_first": agg["a"].isoformat() if agg["a"] else None,
        "price_last": agg["b"].isoformat() if agg["b"] else None,
        "tickers": tickers,
        "ticker_n": len(tickers),
        "domestic_n": len(domestic),
        "overseas_n": len(tickers) - len(domestic),
        "product_n": Product.objects.count(),
        "listed_n": Product.objects.listed().count(),
        "excluded_types": list(PRINCIPAL_PROTECTED_TYPES),
        "stat_year_from": years["a"],
        "stat_year_to": years["b"],
    }
    _COV.update(day=today, data=data)
    return data


def coverage_line():
    """시스템 프롬프트에 넣는 한 줄. 하루 동안 바이트가 고정된다.

    ⚠ 숫자마다 무엇을 센 것인지 라벨을 붙인다. '상품 N건'처럼 모집단이
      불분명하면 첫 화면에 틀린 수가 박힌다.
    """
    c = coverage()
    return (f"기초자산 시세 {c['ticker_n']}티커(국내 {c['domestic_n']}·해외 "
            f"{c['overseas_n']}), {c['price_first']}~{c['price_last']}. "
            f"수집 상품 {c['product_n']:,}건 중 검색·통계 대상은 ELB·DLB 제외 "
            f"{c['listed_n']:,}건. SEIBro 상환 집계 "
            f"{c['stat_year_from']}~{c['stat_year_to']}년.")


def data_stamp():
    """데이터가 갱신되면 바뀌는 값 — 당일 질문 캐시 키에 섞는다.

    09:30 배치가 시세를 채우기 전에 답한 결과가 배치 뒤에도 재사용되면
    화면과 답이 어긋난다. 시세 최신일이 바뀌면 캐시도 갈린다.
    """
    c = coverage()
    return c.get("price_last") or "-"


# ══════════════════════════════════════════════════════════════════
# 자산명 해석 — 서버가 한다 (모델에 resolve 왕복을 시키지 않는다)
# ══════════════════════════════════════════════════════════════════

def resolve_asset(name):
    """자산명 → (ticker, 표시명) 또는 (None, 사유코드)."""
    from . import market
    from .models import PriceBar

    raw = (name or "").strip()
    if not raw:
        return None, NO_ASSET
    up = re.sub(r"\s+", " ", raw).upper()
    ticker = _INDEX_OVERRIDE.get(up) or market.resolve_ticker(raw)
    if not ticker:
        return None, NO_ASSET
    if ticker not in set(coverage()["tickers"]):
        # 티커맵에는 있으나 시세를 적재하지 않은 자산 (예: 신규 편입 직후)
        if not PriceBar.objects.filter(ticker=ticker).exists():
            return None, NO_PRICE_DATA
    return ticker, market.shorten_asset_display(raw)


def _bars(ticker, start, end, price_kind):
    from .models import PriceBar
    fn = (PriceBar.closes_for_barrier if price_kind == "raw"
          else PriceBar.closes_for_return)
    return fn(ticker, start, end)


def _parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _series_meta(ticker, rows, req_start, req_end, price_kind):
    """구간 정보 + 보완 여부 + 사유코드 판정."""
    from .models import PriceBar

    full = PriceBar.objects.filter(ticker=ticker)
    from django.db.models import Max, Min
    span = full.aggregate(a=Min("date"), b=Max("date"))
    have_a, have_b = span["a"], span["b"]
    reason = None
    if not rows:
        reason = OUT_OF_RANGE if (req_start and have_b and req_start > have_b) or \
                                 (req_end and have_a and req_end < have_a) else NO_PRICE_DATA
        return None, reason, have_a, have_b
    if (req_start and have_a and req_start < have_a) or (req_end and have_b and req_end > have_b):
        reason = PARTIAL_RANGE
    filled = PriceBar.objects.filter(ticker=ticker, source=PriceBar.SOURCE_FILLED,
                                     date__gte=rows[0][0], date__lte=rows[-1][0]).count()
    meta = {
        "ticker": ticker,
        "first": rows[0][0].isoformat(),
        "last": rows[-1][0].isoformat(),
        "rows": len(rows),
        "price_kind": price_kind,
        "series": ("조정종가 PriceBar.closes_for_return()" if price_kind != "raw"
                   else "미조정 종가 PriceBar.closes_for_barrier()"),
        "filled_rows": filled,
        "filled_note": ("없음 (전 구간 원본 계열)" if not filled
                        else f"{filled:,}행이 대체 계열 환산 보완값"),
        "have_from": have_a.isoformat() if have_a else None,
        "have_to": have_b.isoformat() if have_b else None,
    }
    return meta, reason, have_a, have_b


# ══════════════════════════════════════════════════════════════════
# 1. price_series — 차트용 축약 계열
# ══════════════════════════════════════════════════════════════════

def price_series(user, asset=None, start=None, end=None, freq="monthly",
                 price_kind="adjusted", **_):
    ticker, label = resolve_asset(asset)
    if ticker is None:
        return {"ok": False, "reason": label, "asset": asset,
                "detail": f"'{asset}'은(는) 시세를 보유한 자산이 아닙니다."}
    s, e = _parse_date(start), _parse_date(end)
    rows = _bars(ticker, s, e, price_kind)
    meta, reason, have_a, have_b = _series_meta(ticker, rows, s, e, price_kind)
    if meta is None:
        return {"ok": False, "reason": reason, "asset": label, "ticker": ticker,
                "have_from": have_a.isoformat() if have_a else None,
                "have_to": have_b.isoformat() if have_b else None,
                "detail": f"요청 구간에 {label} 시세가 없습니다."}

    if freq == "daily":
        picked = rows
    else:
        # 구간(월/주)마다 마지막 거래일만 남긴다. 계산은 언제나 일별 전수로 하고
        # 축약은 차트용일 뿐이다 — 축약값으로 지표를 재면 낙폭이 얕게 나온다.
        key = ((lambda d: (d.year, d.month)) if freq == "monthly"
               else (lambda d: d.isocalendar()[:2]))
        picked = []
        for i, (d, v) in enumerate(rows):
            if i + 1 == len(rows) or key(rows[i + 1][0]) != key(d):
                picked.append((d, v))

    # 낙폭 음영·고점/저점 마커는 **일별 전수**로 잡는다. 축약한 월말 계열로
    # 재면 고점을 놓쳐 낙폭이 얕게 나온다 (차트 선만 축약이고 지표는 아니다).
    worst, wp, wt, _rec = _mdd(rows)
    return {
        "ok": True, "asset": label, "reason": reason,
        "meta": meta,
        "freq": freq,
        "points": [{"d": d.isoformat(), "v": round(v, 4)} for d, v in picked],
        "first_px": price(ticker, rows[0][1]),
        "last_px": price(ticker, rows[-1][1]),
        "high_px": price(ticker, max(v for _, v in rows)),
        "low_px": price(ticker, min(v for _, v in rows)),
        "mdd_marks": {
            "mdd": pct(worst, 1, sign=True),
            "peak": {"d": rows[wp][0].isoformat(), "v": rows[wp][1],
                     "px": price(ticker, rows[wp][1])},
            "trough": {"d": rows[wt][0].isoformat(), "v": rows[wt][1],
                       "px": price(ticker, rows[wt][1])},
        },
    }


# ══════════════════════════════════════════════════════════════════
# 2. metric_calc — 구간 지표
# ══════════════════════════════════════════════════════════════════

def _mdd(rows):
    """(최대낙폭%, 고점idx, 저점idx, 회복idx|None)."""
    peak_i, peak_v = 0, rows[0][1]
    worst, wp, wt = 0.0, 0, 0
    for i, (_, v) in enumerate(rows):
        if v > peak_v:
            peak_v, peak_i = v, i
        dd = (v / peak_v - 1) * 100
        if dd < worst:
            worst, wp, wt = dd, peak_i, i
    rec = None
    for i in range(wt + 1, len(rows)):
        if rows[i][1] >= rows[wp][1]:
            rec = i
            break
    return worst, wp, wt, rec


def _cum(rows):
    return (rows[-1][1] / rows[0][1] - 1) * 100 if rows[0][1] else None


def _bucket_key(d, group_by):
    if group_by == "year":
        return str(d.year)
    if group_by == "quarter":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    if group_by == "month":
        return f"{d.year}-{d.month:02d}"
    return None


def metric_calc(user, asset=None, metrics=None, start=None, end=None,
                group_by="none", threshold_pct=None, price_kind=None, **_):
    metrics = [m for m in (metrics or []) if m in METRICS] or ["cumulative_return"]
    # 낙인성 지표(기준가 대비 하회)는 미조정 종가가 맞다 — 배당락을 손실로 세면
    # "45% 아래로 내려간 적 있나"의 답이 틀린다.
    if price_kind is None:
        price_kind = "raw" if "days_below" in metrics else "adjusted"

    ticker, label = resolve_asset(asset)
    if ticker is None:
        return {"ok": False, "reason": label, "asset": asset,
                "detail": f"'{asset}'은(는) 시세를 보유한 자산이 아닙니다."}
    s, e = _parse_date(start), _parse_date(end)
    rows = _bars(ticker, s, e, price_kind)
    meta, reason, have_a, have_b = _series_meta(ticker, rows, s, e, price_kind)
    if meta is None:
        return {"ok": False, "reason": reason, "asset": label, "ticker": ticker,
                "have_from": have_a.isoformat() if have_a else None,
                "have_to": have_b.isoformat() if have_b else None,
                "detail": (f"{label} 시세는 {have_a}부터 있습니다."
                           if have_a else f"{label} 시세가 없습니다.")}
    if len(rows) < 2:
        return {"ok": False, "reason": NO_PRICE_DATA, "asset": label,
                "detail": "구간 안의 거래일이 2일 미만이라 계산할 수 없습니다."}

    out = {"ok": True, "asset": label, "reason": reason, "meta": meta, "metrics": {}}
    m = out["metrics"]
    days = (rows[-1][0] - rows[0][0]).days
    years = days / 365.25
    out["span"] = {"days": days, "years": num(round(years, 2), f"{years:.2f}년"),
                   "trading_days": _cnt(len(rows), "거래일")}

    rets = [rows[i][1] / rows[i - 1][1] - 1 for i in range(1, len(rows)) if rows[i - 1][1]]

    if "cumulative_return" in metrics or "period_return" in metrics:
        c = _cum(rows)
        key = "cumulative_return" if "cumulative_return" in metrics else "period_return"
        m[key] = pct(c, 1, sign=True)
    if "cagr" in metrics and years > 0 and rows[0][1] > 0:
        g = ((rows[-1][1] / rows[0][1]) ** (1 / years) - 1) * 100
        m["cagr"] = pct(g, 1, sign=True)
    if "volatility" in metrics and len(rets) > 1:
        m["volatility"] = pct(statistics.stdev(rets) * math.sqrt(TRADING_DAYS) * 100, 1)
    if "mdd" in metrics or "mdd_detail" in metrics or "recovery_days" in metrics:
        worst, wp, wt, rec = _mdd(rows)
        if "mdd" in metrics or "mdd_detail" in metrics:
            m["mdd"] = pct(worst, 1, sign=True)
        if "mdd_detail" in metrics:
            m["mdd_detail"] = {
                "peak": rows[wp][0].isoformat(), "peak_px": price(ticker, rows[wp][1]),
                "trough": rows[wt][0].isoformat(), "trough_px": price(ticker, rows[wt][1]),
                "recovered": rows[rec][0].isoformat() if rec is not None else None,
                "recovered_note": None if rec is not None else "구간 안에서 고점을 회복하지 못했습니다.",
            }
        if "recovery_days" in metrics:
            m["recovery_days"] = (_cnt((rows[rec][0] - rows[wt][0]).days, "일")
                                  if rec is not None else None)
    if "worst_day" in metrics and rets:
        i = min(range(len(rets)), key=lambda k: rets[k])
        m["worst_day"] = {"date": rows[i + 1][0].isoformat(), "ret": pct(rets[i] * 100, 1, sign=True)}
    if "best_day" in metrics and rets:
        i = max(range(len(rets)), key=lambda k: rets[k])
        m["best_day"] = {"date": rows[i + 1][0].isoformat(), "ret": pct(rets[i] * 100, 1, sign=True)}
    if "level_vs_high" in metrics:
        hi = max(rows, key=lambda r: r[1])
        m["level_vs_high"] = {"high": price(ticker, hi[1]), "high_date": hi[0].isoformat(),
                              "level": pct(rows[-1][1] / hi[1] * 100, 1),
                              "gap": pct((rows[-1][1] / hi[1] - 1) * 100, 1, sign=True)}
    if "level_vs_low" in metrics:
        lo = min(rows, key=lambda r: r[1])
        m["level_vs_low"] = {"low": price(ticker, lo[1]), "low_date": lo[0].isoformat(),
                             "level": pct(rows[-1][1] / lo[1] * 100, 1)}
    if "days_below" in metrics:
        th = float(threshold_pct) if threshold_pct is not None else 65.0
        base = rows[0][1]
        hits = [(d, v) for d, v in rows if base and v < base * th / 100]
        m["days_below"] = {
            "threshold": pct(th, 0),
            "base_px": price(ticker, base), "base_date": rows[0][0].isoformat(),
            "days": _cnt(len(hits), "거래일"),
            "first_hit": hits[0][0].isoformat() if hits else None,
            "lowest_level": pct(min(v for _, v in rows) / base * 100, 1) if base else None,
        }

    if group_by in ("year", "quarter", "month"):
        buckets = {}
        for d, v in rows:
            buckets.setdefault(_bucket_key(d, group_by), []).append((d, v))
        by = []
        for k, br in sorted(buckets.items()):
            if len(br) < 2:
                continue
            w, _, _, _ = _mdd(br)
            by.append({"bucket": k, "ret": pct(_cum(br), 1, sign=True),
                       "mdd": pct(w, 1, sign=True), "close": price(ticker, br[-1][1]),
                       "trading_days": len(br),
                       "partial": _is_partial(br, group_by)})
        out["by_bucket"] = by
        out["group_by"] = group_by
    return out


def _period_bounds(d, group_by):
    """d가 속한 구간의 시작·끝 날짜."""
    if group_by == "year":
        return date(d.year, 1, 1), date(d.year, 12, 31)
    if group_by == "quarter":
        q = (d.month - 1) // 3
        start = date(d.year, q * 3 + 1, 1)
        end_m = q * 3 + 3
        nxt = date(d.year + 1, 1, 1) if end_m == 12 else date(d.year, end_m + 1, 1)
        return start, nxt - timedelta(days=1)
    start = date(d.year, d.month, 1)
    nxt = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return start, nxt - timedelta(days=1)


def _is_partial(br, group_by):
    """구간을 온전히 덮지 못한 버킷인지. 표에 '부분 구간'으로 표시한다.

    거래일은 달력 첫날·마지막날이 아니므로 7일 여유를 둔다 (1/1 휴장, 12/31 휴장).
    """
    lo, hi = _period_bounds(br[0][0], group_by)
    return (br[0][0] - lo).days > 7 or (hi - br[-1][0]).days > 7


# ══════════════════════════════════════════════════════════════════
# 3. product_filter — 기존 조건검색을 그대로 재사용
# ══════════════════════════════════════════════════════════════════

def product_filter(user, **flt):
    """ai_research.run_filter/describe 재사용 — 검색 화면과 결과가 같아야 한다.

    run_filter는 scope='products'일 때 Product.objects.listed()를 타므로
    ELB·DLB가 자동으로 빠진다.
    """
    from . import ai_research

    flt = {k: v for k, v in flt.items() if v is not None}
    rows, err = ai_research.run_filter(flt, user)
    if err:
        code = OUT_OF_SCOPE if flt.get("unanswerable") else NO_DATA
        return {"ok": False, "reason": code, "detail": err}
    chips = ai_research.describe(flt)
    if not rows:
        return {"ok": False, "reason": NO_DATA, "chips": chips,
                "detail": "조건에 맞는 상품이 없습니다."}

    def _row(p):
        s = p.sim_result or {}
        e1 = s.get("early_1y_pct") if s.get("early_1y_pct") is not None else s.get("early_redemp_pct")
        badge = (p.radar or {}).get("tier") if getattr(p, "radar", None) else None
        return {
            "id": p.id,
            "issuer": p.issuer, "no": p.product_no,
            "assets": p.assets_raw,
            "type": p.asset_type or None,
            "yield": pct(p.yield_rate, 2) if p.yield_rate is not None else None,
            "ki": "노낙인" if p.is_no_ki else (f"{p.ki:g}" if p.ki is not None else None),
            "barrier_first": num(p.barrier_first, f"{p.barrier_first}%") if p.barrier_first else None,
            "loss_prob": pct(p.loss_prob, 2) if p.loss_prob is not None else None,
            "early_1y": pct(e1, 1) if e1 is not None else None,
            "sub_end": p.sub_end.isoformat() if p.sub_end else None,
            "badge": badge,
        }

    return {"ok": True, "chips": chips, "n": len(rows),
            "scope": flt.get("scope", "products"),
            "excluded": "ELB·DLB(원금지급형) 제외",
            "products": [_row(p) for p in rows]}


# ══════════════════════════════════════════════════════════════════
# 4. product_stats — 발행·상환 집계
# ══════════════════════════════════════════════════════════════════

_KI_BANDS = ((0, 35, "35 이하"), (35, 45, "35~45"), (45, 55, "45~55"),
             (55, 100, "55 초과"))
_YIELD_BANDS = ((0, 5, "5% 미만"), (5, 8, "5~8%"), (8, 12, "8~12%"),
                (12, 999, "12% 이상"))
_TERM_BANDS = ((0, 12, "1년 이하"), (12, 24, "1~2년"), (24, 36, "2~3년"),
               (36, 999, "3년 초과"))


def _band(v, bands):
    """값 → 구간 라벨. 경계는 (lo, hi] 로 잡는다 (lo=0이면 하한 없음)."""
    for lo, hi, name in bands:
        if v <= hi and (lo == 0 or v > lo):
            return name
    return bands[-1][2]


def product_stats(user, metric=None, group_by="none", year_from=None, year_to=None,
                  asset_type=None, asset_contains=None, subscribing_only=False,
                  limit=None, **_):
    from django.db.models import Count, Sum

    from .models import HistoricalIssue, HistoricalYieldStat, Product

    limit = max(1, min(int(limit or 12), 30))
    cov = coverage()
    y_from = int(year_from) if year_from else None
    y_to = int(year_to) if year_to else None
    if y_from and cov["stat_year_to"] and y_from > cov["stat_year_to"]:
        return {"ok": False, "reason": OUT_OF_RANGE,
                "detail": f"집계 자료는 {cov['stat_year_from']}~{cov['stat_year_to']}년만 있습니다."}

    # ── 상환 실적 계열: SEIBro 공식 집계(HistoricalYieldStat) ──
    if metric in ("realized_yield", "loss_count", "loss_rate"):
        qs = HistoricalYieldStat.objects.all()
        if y_from:
            qs = qs.filter(year__gte=y_from)
        if y_to:
            qs = qs.filter(year__lte=y_to)
        if asset_type:
            qs = qs.filter(basset_sort=asset_type)
        rows = list(qs.values("year", "count", "margin_rate", "minus_count",
                              "redemption_amount", "assets"))
        if asset_contains:
            key = asset_contains.lower()
            rows = [r for r in rows if any(key in (a or "").lower() for a in (r["assets"] or []))]
        if not rows:
            return {"ok": False, "reason": NO_DATA, "detail": "조건에 맞는 상환 집계가 없습니다."}

        def _agg(sub):
            n = sum(r["count"] or 0 for r in sub)
            minus = sum(r["minus_count"] or 0 for r in sub)
            wsum = sum((r["margin_rate"] or 0) * (r["count"] or 0) for r in sub)
            return {
                "count": _cnt(n),
                "loss_count": _cnt(minus),
                "loss_rate": pct(minus / n * 100, 2) if n else None,
                "realized_yield": pct(wsum / n, 2, sign=True) if n else None,
            }

        if group_by == "year":
            by = {}
            for r in rows:
                by.setdefault(r["year"], []).append(r)
            data = [dict(bucket=str(y), **_agg(v)) for y, v in sorted(by.items())]
        else:
            data = [dict(bucket="전체", **_agg(rows))]
        return {"ok": True, "metric": metric, "group_by": group_by,
                "source": "SEIBro 주요기초자산별 상환수익률(공식 집계)",
                "note": "실현수익률은 상환건수 가중평균입니다.",
                "rows": data[:limit] if group_by == "year" else data}

    # ── 청약중 상품 대상 (현행 Product) ──
    if subscribing_only:
        qs = Product.objects.listed().filter(sub_end__gte=date.today())
        if asset_type:
            qs = qs.filter(asset_type=asset_type)
        if asset_contains:
            qs = qs.filter(assets_raw__icontains=asset_contains)
        items = list(qs.values("issuer", "assets_raw", "asset_type", "ki",
                               "is_no_ki", "yield_rate", "period_months"))
        if not items:
            return {"ok": False, "reason": NO_DATA, "detail": "청약 가능한 상품이 없습니다."}
        return {"ok": True, "metric": metric, "scope": "청약중",
                "total": _cnt(len(items)),
                "rows": _tally(items, metric, group_by, limit, live=True)}

    # ── 발행 이력 (SEIBro HistoricalIssue) ──
    qs = HistoricalIssue.objects.all()
    if y_from:
        qs = qs.filter(issue_date__year__gte=y_from)
    if y_to:
        qs = qs.filter(issue_date__year__lte=y_to)
    if asset_type:
        qs = qs.filter(basset_sort=asset_type)

    if metric == "issue_count":
        if group_by == "year":
            data = (qs.exclude(issue_date=None).values("issue_date__year")
                    .annotate(n=Count("id")).order_by("issue_date__year"))
            rows = [{"bucket": str(r["issue_date__year"]), "count": _cnt(r["n"])} for r in data]
        elif group_by == "issuer":
            data = qs.values("issuer").annotate(n=Count("id")).order_by("-n")[:limit]
            rows = [{"bucket": r["issuer"], "count": _cnt(r["n"])} for r in data]
        elif group_by == "asset_type":
            data = qs.values("basset_sort").annotate(n=Count("id")).order_by("-n")
            rows = [{"bucket": r["basset_sort"] or "미상", "count": _cnt(r["n"])} for r in data]
        else:
            rows = [{"bucket": "전체", "count": _cnt(qs.count())}]
        return {"ok": True, "metric": metric, "group_by": group_by,
                "source": "SEIBro 발행종목조회", "rows": rows[:limit] if limit else rows}

    if metric == "issuer_share":
        total = qs.count()
        data = qs.values("issuer").annotate(n=Count("id")).order_by("-n")[:limit]
        return {"ok": True, "metric": metric, "total": _cnt(total),
                "source": "SEIBro 발행종목조회",
                "rows": [{"issuer": r["issuer"], "count": _cnt(r["n"]),
                          "share": pct(r["n"] / total * 100, 1) if total else None}
                         for r in data]}

    if metric == "asset_frequency":
        # 기초자산은 JSON 리스트라 파이썬에서 센다. 구간을 안 주면 최근 1년으로 좁힌다.
        if not (y_from or y_to):
            qs = qs.filter(issue_date__year=date.today().year)
        tally, total = {}, 0
        for assets in qs.values_list("assets", flat=True).iterator(chunk_size=2000):
            for a in (assets or []):
                nm = (a.get("name") if isinstance(a, dict) else a) or ""
                nm = nm.strip()
                if not nm:
                    continue
                if asset_contains and asset_contains.lower() not in nm.lower():
                    continue
                tally[nm] = tally.get(nm, 0) + 1
                total += 1
        if not tally:
            return {"ok": False, "reason": NO_DATA, "detail": "해당 구간의 기초자산 집계가 없습니다."}
        top = sorted(tally.items(), key=lambda kv: -kv[1])[:limit]
        return {"ok": True, "metric": metric, "total": _cnt(total, "회"),
                "source": "SEIBro 발행종목조회 기초자산 등장 횟수",
                "rows": [{"asset": k, "count": _cnt(v, "회"),
                          "share": pct(v / total * 100, 1)} for k, v in top]}

    if metric in ("ki_distribution", "yield_distribution", "term_distribution"):
        field, bands = {
            "ki_distribution": ("ki", _KI_BANDS),
            "yield_distribution": ("yield_rate", _YIELD_BANDS),
            "term_distribution": ("period_months", _TERM_BANDS),
        }[metric]
        base = qs.exclude(**{f"{field}__isnull": True})
        vals = list(base.values_list("issue_date__year", field))
        if not vals:
            return {"ok": False, "reason": NO_DATA,
                    "detail": f"{metric}에 쓸 값이 채워진 이력이 없습니다."}
        if group_by == "year":
            by = {}
            for y, v in vals:
                by.setdefault(y, {}).setdefault(_band(v, bands), 0)
                by[y][_band(v, bands)] += 1
            rows = []
            for y in sorted(k for k in by if k is not None):
                tot = sum(by[y].values())
                rows.append({"bucket": str(y), "count": _cnt(tot),
                             "bands": [{"band": b, "count": _cnt(by[y].get(b, 0)),
                                        "share": pct(by[y].get(b, 0) / tot * 100, 1)}
                                       for _, _, b in bands]})
        else:
            tally = {}
            for _, v in vals:
                tally[_band(v, bands)] = tally.get(_band(v, bands), 0) + 1
            tot = sum(tally.values())
            rows = [{"band": b, "count": _cnt(tally.get(b, 0)),
                     "share": pct(tally.get(b, 0) / tot * 100, 1)} for _, _, b in bands]
        return {"ok": True, "metric": metric, "group_by": group_by,
                "sample": _cnt(len(vals)),
                "note": "SEIBro 상세조회가 채워진 건만 집계합니다 (전수 아님).",
                "rows": rows[:limit] if group_by == "year" else rows}

    return {"ok": False, "reason": OUT_OF_SCOPE,
            "detail": f"'{metric}'은(는) 집계할 수 없는 지표입니다."}


def _tally(items, metric, group_by, limit, live=False):
    """청약중 상품(현행 Product)에 대한 간단 집계."""
    if metric == "issuer_share":
        t = {}
        for i in items:
            t[i["issuer"]] = t.get(i["issuer"], 0) + 1
        tot = len(items)
        return [{"issuer": k, "count": _cnt(v), "share": pct(v / tot * 100, 1)}
                for k, v in sorted(t.items(), key=lambda kv: -kv[1])[:limit]]
    if metric == "asset_frequency":
        from . import market
        t = {}
        for i in items:
            for a in market.split_assets(i["assets_raw"]):
                nm = market.shorten_asset_display(a)
                t[nm] = t.get(nm, 0) + 1
        tot = sum(t.values()) or 1
        return [{"asset": k, "count": _cnt(v, "회"), "share": pct(v / tot * 100, 1)}
                for k, v in sorted(t.items(), key=lambda kv: -kv[1])[:limit]]
    field, bands = {
        "ki_distribution": ("ki", _KI_BANDS),
        "yield_distribution": ("yield_rate", _YIELD_BANDS),
        "term_distribution": ("period_months", _TERM_BANDS),
    }.get(metric, ("ki", _KI_BANDS))
    t = {}
    if field == "ki":
        for i in items:
            key = ("노낙인" if i["is_no_ki"]
                   else (_band(i["ki"], bands) if i["ki"] is not None else "미상"))
            t[key] = t.get(key, 0) + 1
    else:
        for i in items:
            v = i[field]
            if v is None:
                continue
            key = _band(v, bands)
            t[key] = t.get(key, 0) + 1
    tot = sum(t.values()) or 1
    return [{"band": k, "count": _cnt(v), "share": pct(v / tot * 100, 1)}
            for k, v in sorted(t.items(), key=lambda kv: -kv[1])]


# ══════════════════════════════════════════════════════════════════
# 5. portfolio_facts — 화면과 같은 함수를 부른다
# ══════════════════════════════════════════════════════════════════

def portfolio_facts_tool(user, views=None, sort=None, within_days=None, limit=None, **_):
    from . import portfolio_facts

    if not (user and user.is_authenticated):
        return {"ok": False, "reason": "NEEDS_LOGIN", "detail": "로그인이 필요합니다."}
    data = portfolio_facts.facts(user, views or ["summary"], sort=sort,
                                 within_days=within_days, limit=limit)
    if data.get("reason") == "NO_PORTFOLIO":
        return {"ok": False, **data}
    data["ok"] = True
    data["source"] = "본인이 등록한 보유 기록. 스트레스 테스트는 /portfolio/ 화면과 같은 계산."
    return data


# ══════════════════════════════════════════════════════════════════
# 디스패치
# ══════════════════════════════════════════════════════════════════

RUNNERS = {
    "price_series": price_series,
    "metric_calc": metric_calc,
    "product_filter": product_filter,
    "product_stats": product_stats,
    "portfolio_facts": portfolio_facts_tool,
}


def run(name, user, params):
    """도구 하나 실행. 예외는 사유 코드로 바꿔 모델에 돌려준다."""
    fn = RUNNERS.get(name)
    if fn is None:
        return {"ok": False, "reason": OUT_OF_SCOPE, "detail": f"알 수 없는 도구: {name}"}
    try:
        return fn(user, **(params or {}))
    except Exception:
        logger.exception("ask 도구 실패 name=%s params=%s", name, params)
        return {"ok": False, "reason": "TOOL_ERROR",
                "detail": "조회 중 오류가 발생했습니다."}


def slim_for_model(name, result):
    """설명턴에 보낼 축약본. 화면 블록은 원본을 쓴다.

    왜 나누나
      차트 좌표는 서버가 그린다. 121개 포인트를 모델에 넘겨 봐야 3~5문장을
      쓰는 데 아무 쓸모가 없는데, 실측으로 그 한 덩어리가 설명턴 입력의
      58%를 먹었다. 프리픽스를 줄이는 것보다 여기를 자르는 게 훨씬 크다.
      (프리픽스는 오히려 줄이면 안 된다 — 캐시 최소치 아래로 떨어진다.)
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    out = dict(result)
    if name == "price_series":
        pts = out.pop("points", [])
        out["points_note"] = (f"차트용 {len(pts)}개 포인트는 서버가 직접 그립니다 "
                              f"(문장에 옮길 필요 없음).")
    if name == "product_filter":
        out["products"] = [{k: v for k, v in p.items() if k != "id"}
                           for p in out.get("products", [])]
    return out


def displays(result, acc=None):
    """도구 결과에 들어 있는 display 문자열 전부 — 사후검사가 이 집합과 대조한다."""
    acc = set() if acc is None else acc
    if isinstance(result, dict):
        if set(result) >= {"value", "display"} and isinstance(result.get("display"), str):
            acc.add(result["display"])
            return acc
        for v in result.values():
            displays(v, acc)
    elif isinstance(result, (list, tuple)):
        for v in result:
            displays(v, acc)
    elif isinstance(result, str):
        # 날짜·티커처럼 그대로 인용해도 되는 값
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", result):
            acc.add(result)
    return acc


def display_map(result, acc=None):
    """{표시문자열: 원본값} — 답변에서 강조할 대상과 부호를 함께 알기 위해."""
    acc = {} if acc is None else acc
    if isinstance(result, dict):
        if set(result) >= {"value", "display"} and isinstance(result.get("display"), str):
            acc.setdefault(result["display"], result["value"])
            return acc
        for v in result.values():
            display_map(v, acc)
    elif isinstance(result, (list, tuple)):
        for v in result:
            display_map(v, acc)
    return acc


def to_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
