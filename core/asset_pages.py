"""기초자산별 공개 페이지 — 로그인 없이 보는 SEO 유입 지면.

상위 10개 자산만 다룬다(엘마 벤치마크, `1_콘텐츠/경쟁분석_엘마_v1.md` 6장
1순위 / 2026-08-24 조 팀장 지시). 전체 자산을 다 열면 꼬리 자산까지 얇은
콘텐츠 페이지가 늘어나 관리 부담만 커진다.

자산명은 market.resolve_ticker로 정규화한 티커를 기준으로 묶는다 —
"KOSPI200"과 "KOSPI200 Index"처럼 발행사마다 다르게 찍힌 표기가 다른
자산으로 갈라지지 않게 하기 위해서다.

URL 슬러그는 영문으로 확정(2026-08-24). 티커를 그대로 쓰면 "069500.KS"처럼
URL이 지저분해지므로, 10개뿐인 김에 손으로 붙였다.

⚠ 배지 통과율·10년 백테스트 성과는 여기 없다. 지금 배지는 "그 주 × 유형"
단위로만 계산돼 있어 자산 단위 집계가 따로 필요하고, 백테스트 자체도 아직
검증 중(선견 편향 재측정 진행 중)이라 공개 지면에 올릴 단계가 아니다.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from core.market import resolve_ticker, split_assets

WINDOW_DAYS = 90  # "최근 발행" 집계 기간 — 목업 검토 때 쓴 것과 맞춘다

# (slug, 정규화 티커, 표시명) — 최근 12개월 발행 건수 기준 상위 10개 (2026-08-24 집계)
TOP_ASSETS = [
    ("kospi200", "069500.KS", "KOSPI200"),
    ("sp500", "^GSPC", "S&P500"),
    ("eurostoxx50", "^STOXX50E", "Euro Stoxx 50"),
    ("nikkei225", "^N225", "Nikkei225"),
    ("samsung", "005930.KS", "삼성전자"),
    ("sk-hynix", "000660.KS", "SK하이닉스"),
    ("micron", "MU", "Micron"),
    ("tesla", "TSLA", "Tesla"),
    ("palantir", "PLTR", "Palantir"),
    ("hscei", "^HSCE", "HSCEI"),
]
_TICKER_BY_SLUG = {slug: ticker for slug, ticker, _ in TOP_ASSETS}
_NAME_BY_SLUG = {slug: name for slug, _, name in TOP_ASSETS}


def _product_tickers(product):
    """상품의 기초자산들을 정규화 티커 집합으로 바꾼다.

    정규화 실패분은 원문 문자열을 그대로 남겨 두는데, 이러면 TOP_ASSETS의
    실제 티커 값과는 우연히도 절대 겹치지 않으므로(전부 실제 야후/KRX
    티커 형식) 집계에 잡음을 넣지 않는다.
    """
    return {resolve_ticker(a) or a for a in split_assets(product.assets_raw or "")}


def asset_context(slug):
    """자산 상세 페이지 컨텍스트. 등록되지 않은 슬러그면 None."""
    ticker = _TICKER_BY_SLUG.get(slug)
    if ticker is None:
        return None

    from core.models import Product

    cutoff = date.today() - timedelta(days=WINDOW_DAYS)
    qs = Product.objects.listed().filter(sub_end__gte=cutoff)
    rows = [p for p in qs if ticker in _product_tickers(p)]

    kis = sorted({p.ki for p in rows if p.ki is not None})
    yields = [p.yield_rate for p in rows if p.yield_rate is not None]

    issuer_counts = Counter(p.issuer for p in rows)
    top_issuers = issuer_counts.most_common(5)
    max_issuer = top_issuers[0][1] if top_issuers else 0

    return {
        "slug": slug,
        "name": _NAME_BY_SLUG[slug],
        "window_days": WINDOW_DAYS,
        "count": len(rows),
        "ki_lo": kis[0] if kis else None,
        "ki_hi": kis[-1] if kis else None,
        "yield_lo": min(yields) if yields else None,
        "yield_hi": max(yields) if yields else None,
        "issuer_count": len(issuer_counts),
        "top_issuers": [
            {"name": name, "count": n, "pct": round(n / max_issuer * 100)}
            for name, n in top_issuers
        ],
        "recent": sorted(rows, key=lambda p: p.sub_end, reverse=True)[:8],
        "others": [(s, name) for s, _, name in TOP_ASSETS if s != slug],
    }
