"""
청약중/최근 상품에 대해 수익률 모의실험(백테스트)을 돌려 결과를 캐시.

- yfinance 호출이 느리므로 티커별 시세를 한 번만 받아 캐시하고 여러 상품이 공유.
- 결과는 Product.loss_prob / sim_samples / sim_result(JSON) / sim_updated 에 저장.
- 상세 페이지·목록은 이 캐시만 읽음 (요청 시 yfinance 안 돌림).
"""

from datetime import date, timedelta

import pandas as pd
from django.core.management.base import BaseCommand
from django.utils import timezone

from core import backtest, market
from core.models import Product


def _jsonable(result: dict) -> dict:
    """date 객체를 ISO 문자열로 변환해 JSONField 저장 가능하게."""
    out = dict(result)
    for k in ("period_start", "period_end"):
        if isinstance(out.get(k), date):
            out[k] = out[k].isoformat()
    return out


class Command(BaseCommand):
    help = "기초자산 과거 데이터로 회차별 상환/손실 분포 시뮬레이션 후 캐시"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=45,
                            help="최근 N일 내 청약마감 상품만 대상 (기본 45)")
        parser.add_argument("--all", action="store_true", help="전체 상품 대상")
        parser.add_argument("--years", type=int, default=20, help="과거 데이터 기간(년)")

    def handle(self, *args, **opts):
        qs = Product.objects.filter(
            barriers_raw__isnull=False, period_months__isnull=False,
            yield_rate__isnull=False,
        )
        if not opts["all"]:
            cutoff = date.today() - timedelta(days=opts["days"])
            qs = qs.filter(sub_end__gte=cutoff)

        products = list(qs)
        self.stdout.write(f"대상 상품: {len(products)}건")

        price_cache = {}  # ticker -> pandas Series (없으면 None)
        years = opts["years"]

        ok = skip = 0
        for p in products:
            result = self._simulate_one(p, price_cache, years)
            if result.get("available"):
                p.loss_prob = result["loss_prob_pct"]
                p.sim_samples = result["samples"]
                p.sim_result = _jsonable(result)
                p.sim_updated = timezone.now()
                p.save(update_fields=["loss_prob", "sim_samples", "sim_result", "sim_updated"])
                ok += 1
            else:
                # 시뮬 불가도 사유 저장 (상세 페이지 안내용), 수치는 null 유지
                p.sim_result = {"available": False, "reason": result.get("reason", "")}
                p.sim_updated = timezone.now()
                p.save(update_fields=["sim_result", "sim_updated"])
                skip += 1

        self.stdout.write(f"[시뮬] 완료 {ok}건 / 불가 {skip}건")

    def _simulate_one(self, product, price_cache, years):
        assets = market.split_assets(product.assets_raw)
        if not assets:
            return {"available": False, "reason": "기초자산 정보 없음"}

        series = {}
        for a in assets:
            tk = market.resolve_ticker(a)
            if not tk:
                return {"available": False, "reason": f"시세 매핑 없음: {a}"}
            if tk not in price_cache:
                price_cache[tk] = self._fetch_series(tk, years)
            s = price_cache[tk]
            if s is None or len(s) == 0:
                return {"available": False, "reason": f"시세 조회 실패: {a}"}
            series[a] = s

        prices = pd.DataFrame(series).dropna()
        if len(prices) < 60:
            return {"available": False, "reason": "공통 시세 구간 부족"}

        return backtest.simulate(
            prices,
            barriers=product.barriers_raw,
            ki=product.ki,
            is_no_ki=product.is_no_ki,
            period_months=product.period_months,
            yield_rate=product.yield_rate,
        )

    def _fetch_series(self, ticker, years):
        import yfinance as yf
        try:
            h = yf.Ticker(ticker).history(period=f"{years}y")
            s = h["Close"].dropna()
            if len(s):
                s.index = s.index.tz_localize(None)
                return s
        except Exception:
            pass
        return None
