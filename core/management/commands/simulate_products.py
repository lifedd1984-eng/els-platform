"""
청약중/최근 상품에 대해 수익률 모의실험(백테스트)을 돌려 결과를 캐시.

- yfinance 호출이 느리므로 티커별 시세를 한 번만 받아 캐시하고 여러 상품이 공유.
- 결과는 Product.loss_prob / sim_samples / sim_result(JSON) / sim_updated 에 저장.
- 상세 페이지·목록은 이 캐시만 읽음 (요청 시 yfinance 안 돌림).
"""

import time
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
        from django.db.models import Q
        from core.models import Investment

        base = Product.objects.filter(
            barriers_raw__isnull=False, period_months__isnull=False,
            yield_rate__isnull=False,
        )
        if opts["all"]:
            qs = base
        else:
            # 최근 청약 상품 + 보유중인 투자 상품(오래됐어도 포함)은 항상 시뮬
            cutoff = date.today() - timedelta(days=opts["days"])
            held_ids = list(
                Investment.objects.filter(status="보유중").values_list("product_id", flat=True)
            )
            qs = base.filter(Q(sub_end__gte=cutoff) | Q(id__in=held_ids))

        products = list(qs)
        self.stdout.write(f"대상 상품: {len(products)}건")

        # ── 미매핑 기초자산 자동 해결 (Yahoo 검색) → 학습 저장 후 아래 시뮬에 반영 ──
        self._auto_learn_tickers(products)

        price_cache = {}  # ticker -> pandas Series (없으면 None)
        years = opts["years"]

        # 시세 조회 실패로 스킵된 상품 추적 (실패 티커 재시도 후 재시뮬 대상)
        fetch_failed_products = []

        ok = skip = 0
        unmapped = {}  # 자산명 → 건수 (티커맵 누락 감지용)
        for p in products:
            result = self._simulate_one(p, price_cache, years)
            reason = result.get("reason", "")
            if reason.startswith("시세 매핑 없음: "):
                name = reason.split(": ", 1)[1]
                # 'nan'·빈 값은 원본 기초자산 결손 → 티커맵으로 못 고침, 알림 제외
                if name.strip().lower() not in ("nan", "none", ""):
                    unmapped[name] = unmapped.get(name, 0) + 1
            if result.get("available"):
                self._save_ok(p, result)
                ok += 1
            else:
                # 시뮬 불가도 사유 저장 (상세 페이지 안내용), 수치는 null 유지
                self._save_skip(p, result)
                skip += 1
                if result.get("fetch_failed"):
                    fetch_failed_products.append(p)

        # 실패 티커 재시도 패스: rate-limit로 실패한 티커들을 잠시 쉬고 재조회
        failed_tickers = [tk for tk, s in price_cache.items() if s is None or len(s) == 0]
        if failed_tickers and fetch_failed_products:
            self.stdout.write(
                f"[재시도] 시세 실패 티커 {len(failed_tickers)}개: {', '.join(failed_tickers)}"
            )
            time.sleep(10)
            recovered = []
            for tk in failed_tickers:
                s = self._fetch_series(tk, years)
                if s is not None and len(s):
                    price_cache[tk] = s
                    recovered.append(tk)
            self.stdout.write(
                f"[재시도] 복구 {len(recovered)}개 / 잔여 실패 "
                f"{len(failed_tickers) - len(recovered)}개"
                + (f" ({', '.join(t for t in failed_tickers if t not in recovered)})"
                   if len(recovered) < len(failed_tickers) else "")
            )
            if recovered:
                re_ok = 0
                for p in fetch_failed_products:
                    result = self._simulate_one(p, price_cache, years)
                    if result.get("available"):
                        self._save_ok(p, result)
                        ok += 1
                        skip -= 1
                        re_ok += 1
                    else:
                        self._save_skip(p, result)
                self.stdout.write(f"[재시도] 재시뮬 성공 {re_ok}건")

        self.stdout.write(f"[시뮬] 완료 {ok}건 / 불가 {skip}건")

        # 티커맵 누락 자산이 있으면 텔레그램으로 알림 → 티커만 추가하면 다음 배치에 자동 반영
        if unmapped:
            from core import telegram
            lines = [f"[티커 누락] 손실확률 미산출 자산 {len(unmapped)}종"]
            for name, cnt in sorted(unmapped.items(), key=lambda x: -x[1]):
                lines.append(f"- {name} ({cnt}개 상품)")
            lines.append("core/market.py TICKER_MAP에 추가 필요")
            telegram.send_message("\n".join(lines))
            self.stdout.write(f"[티커누락 알림] {len(unmapped)}종 발송")

    def _save_ok(self, p, result):
        p.loss_prob = result["loss_prob_pct"]
        p.sim_samples = result["samples"]
        p.sim_result = _jsonable(result)
        p.sim_updated = timezone.now()
        p.save(update_fields=["loss_prob", "sim_samples", "sim_result", "sim_updated"])

    def _save_skip(self, p, result):
        p.sim_result = {"available": False, "reason": result.get("reason", "")}
        p.sim_updated = timezone.now()
        p.save(update_fields=["sim_result", "sim_updated"])

    def _auto_learn_tickers(self, products):
        """대상 상품의 미매핑 기초자산을 Yahoo 검색으로 자동 해결해 학습 저장.
        영문명만 자동 해결(한글 등은 오매핑 위험 → 아래 시뮬에서 알림 처리)."""
        import time
        pending = {}
        for p in products:
            for a in market.split_assets(p.assets_raw):
                if a.strip().lower() in ("nan", "none", ""):
                    continue
                if not market.resolve_ticker(a):
                    pending[a] = pending.get(a, 0) + 1
        if not pending:
            return
        learned = []
        for name, cnt in sorted(pending.items(), key=lambda x: -x[1]):
            tk = market.auto_resolve_ticker(name)
            if tk:
                market.learn_ticker(name, tk)
                learned.append((name, tk, cnt))
                self.stdout.write(f"  [자동티커] {name!r} → {tk} ({cnt}건)")
                time.sleep(0.6)
        if learned:
            self.stdout.write(f"[자동티커] {len(learned)}종 학습 저장")
            try:
                from core import telegram
                lines = ["[티커 자동추가] 신규 기초자산 자동 해결"]
                lines += [f"- {n} → {t} ({c}개)" for n, t, c in learned]
                lines.append("검증 후 필요시 core/market.py TICKER_MAP으로 승격")
                telegram.send_message("\n".join(lines))
            except Exception:
                pass

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
                price_cache[tk] = self._fetch_series(tk, years, throttle=True)
            s = price_cache[tk]
            if s is None or len(s) == 0:
                return {"available": False, "reason": f"시세 조회 실패: {a}",
                        "fetch_failed": True}
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

    def _fetch_series(self, ticker, years, throttle=False):
        import yfinance as yf

        # 새 티커를 실제로 요청할 때만 간격을 둬서 rate-limit 완화 (캐시 히트엔 없음)
        if throttle:
            time.sleep(0.4)

        backoffs = [2, 5]  # 재시도 전 대기(초); 총 3회 시도
        for attempt in range(3):
            try:
                h = yf.Ticker(ticker).history(period=f"{years}y")
                s = h["Close"].dropna()
                if len(s):
                    s.index = s.index.tz_localize(None)
                    return s
            except Exception:
                pass
            if attempt < len(backoffs):
                time.sleep(backoffs[attempt])
        return None
