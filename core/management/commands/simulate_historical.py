"""
과거 ELS 전수분(HistoricalIssue)에 백테스트를 돌려 손실확률·1년내 조기상환률을 채운다.
— "당시 레이더가 있었다면 어떤 상품에 배지를 줬을지" 재현(verify_historical)의 입력.

■ 최적화: 시뮬은 '배지 후보'에만 돌린다 — 단 컷은 넉넉하게
  레이더 배지는 낙인·막차배리어 게이트를 반드시 통과해야 나온다. 이 두 조건은 DB 값만으로
  즉시 판정되므로 먼저 걸러 탈락분은 sim_skip="게이트미달"만 남기고 백테스트를 **생략**한다.
  배지 없는 대조군은 시뮬값 없이도 실제 결과 판정만으로 검증되므로 정보 손실이 없다.

  ⚠ 여기서 쓰는 컷은 서비스 고정 상수가 아니라 **느슨한 상한**
    (hist_radar.PRE_KI_MAX = 지수형 60 / 종목형 65, PRE_LAST_MAX = 80)이다.
    실제 배지 컷은 verify_historical이 시대적응형(트레일링 퍼센타일)으로 정하는데
    과거엔 컷이 더 관대해지므로(실효 낙인 컷 지수형 ~50, 종목형 ~60),
    2026년 상수(45/35)로 미리 자르면 '당시라면 배지였을' 후보를 통째로 놓친다.
    상한을 넘는 상품은 어떤 시대 컷으로도 통과 못 하므로 생략해도 안전하다.

■ 기준이 바뀌었을 때 — --redo-gated
  이전 실행이 옛(더 엄격한) 기준으로 "게이트미달"을 찍어놨다면 이 옵션으로 그 행들의
  sim_skip을 비워 새 상한으로 재검토한다. 이미 시뮬이 끝난 행(sim_loss_prob 보유)은
  건드리지 않으므로 낭비가 없다.

■ 선견 편향(lookahead) 차단
  시세 DataFrame을 **발행일 이전**으로 잘라서 백테스트한다(hist_radar.build_price_frame).
  2018년 상품은 2018년까지의 데이터로만 돌린다 — 그래야 '당시 레이더'가 된다.

■ 시세
  티커별 전 기간(1996~)을 한 번만 받아 메모리 캐시(PriceStore). 지수형이 87%라
  티커 종류가 적어 캐시 히트율이 매우 높다. auto_adjust=False.

■ 재개·안전
  sim_loss_prob가 비고 sim_skip도 빈 건만 대상 → 중단돼도 이어서 돌릴 수 있다.
  200건마다 DB 커밋 + logs/hist_sim.log 진행 기록. Ctrl+C 시 처리분 저장 후 종료.

사용:
  python manage.py simulate_historical --limit 300            (시범)
  python manage.py simulate_historical                        (전수)
  python manage.py simulate_historical --redo-gated           (옛 기준 게이트미달분 재검토)
  python manage.py simulate_historical --start-year 2018 --end-year 2020
"""

import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core import backtest, hist_radar
from core.hist_radar import (
    SKIP_COND, SKIP_GATE, SKIP_PRICE, SKIP_TICKER,
)
from core.models import HistoricalIssue

FLUSH_EVERY = 200
LOG_PATH = Path(settings.BASE_DIR) / "logs" / "hist_sim.log"
FRAME_CACHE_MAX = 64   # (티커조합, 발행일) 프레임 재사용 — 같은 날 같은 자산 상품이 많다


class Command(BaseCommand):
    help = "과거 공모 ELS 전수분 백테스트(손실확률·1년내 조기상환), 배지 후보만"

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int, default=2016)
        parser.add_argument("--end-year", type=int, default=2026)
        parser.add_argument("--limit", type=int, default=0, help="N건만 처리(시범용, 0=전체)")
        parser.add_argument("--years", type=int, default=20, help="백테스트 표본 구간(년)")
        parser.add_argument("--delay", type=float, default=0.4,
                            help="신규 티커 조회 간격(초), rate-limit 완화용")
        parser.add_argument("--redo-gated", action="store_true",
                            help="옛 기준으로 '게이트미달' 처리된 행을 새 상한으로 재검토")

    # ------------------------------------------------------------------ main
    def handle(self, *args, **opts):
        years = opts["years"]

        base = dict(
            product_type="ELS", recu_whcd="공모",
            detail_fetched=True, parse_error="",
            issue_date__year__range=(opts["start_year"], opts["end_year"]),
        )
        if opts["redo_gated"]:
            # 게이트 기준이 완화됐으므로 옛 탈락분만 미처리 상태로 되돌린다
            # (시뮬이 이미 끝난 행·티커/시세 문제로 빠진 행은 그대로 둔다)
            n = HistoricalIssue.objects.filter(
                sim_skip=SKIP_GATE, sim_loss_prob__isnull=True, **base).update(sim_skip="")
            self.stdout.write(f"[재검토] 게이트미달 {n}건을 미처리로 되돌림 "
                              f"(상한 낙인 {hist_radar.PRE_KI_MAX}, 막차 "
                              f"{hist_radar.PRE_LAST_MAX})")
            self._log(f"[재검토] 게이트미달 {n}건 초기화")

        qs = HistoricalIssue.objects.filter(
            # 재개: 아직 손도 안 댄 건만 (성공분·스킵분은 건너뛴다)
            sim_loss_prob__isnull=True, sim_skip="", **base
        )
        ids = list(qs.order_by("issue_date").values_list("id", flat=True))
        if opts["limit"]:
            ids = ids[: opts["limit"]]

        total = len(ids)
        self.stdout.write(f"시뮬 대상(미처리): {total}건 "
                          f"({opts['start_year']}~{opts['end_year']}, 공모 ELS)")
        if not total:
            return
        self._log(f"=== 시작 {total}건 ({opts['start_year']}~{opts['end_year']}) ===")

        self.store = hist_radar.PriceStore(throttle=opts["delay"], logger=self._log)
        self._sim_cache = {}    # (티커, 배리어, 낙인, 주기, 발행일) → (손실확률, 1년내)
        self._frame_cache = {}  # (티커, 발행일) → DataFrame | 사유코드

        stats = {"ok": 0, SKIP_GATE: 0, SKIP_TICKER: 0, SKIP_PRICE: 0, SKIP_COND: 0}
        started = time.time()
        done = 0
        status = "완료"

        try:
            for i in range(0, total, FLUSH_EVERY):
                chunk = list(HistoricalIssue.objects.filter(id__in=ids[i:i + FLUSH_EVERY]))
                try:
                    for issue in chunk:
                        self._process(issue, years, stats)
                        done += 1
                finally:
                    # 중단되더라도 이 청크에서 계산한 값은 반드시 저장한다
                    HistoricalIssue.objects.bulk_update(
                        chunk, ["sim_loss_prob", "sim_early_1y", "sim_skip"])
                self._progress(done, total, stats, started)
        except KeyboardInterrupt:
            status = "중단(사용자)"
            self.stdout.write(self.style.WARNING("\n중단 요청 — 처리분 저장 후 종료합니다."))

        elapsed = (time.time() - started) / 60
        summary = (
            f"{status}: 처리 {done}/{total} / 시뮬성공 {stats['ok']} "
            f"/ 게이트미달 {stats[SKIP_GATE]} / 티커미해결 {stats[SKIP_TICKER]} "
            f"/ 시세부족 {stats[SKIP_PRICE]} / 조건부족 {stats[SKIP_COND]} "
            f"/ 시세조회 {self.store.fetched}티커 / 경과 {elapsed:.0f}m"
        )
        self.stdout.write(self.style.SUCCESS(summary))
        self._log(summary)
        if self.store.failed:
            uniq = sorted(set(self.store.failed))
            line = f"  시세 실패 티커 {len(uniq)}종: {', '.join(uniq[:40])}"
            self.stdout.write(line)
            self._log(line)

    # ------------------------------------------------------------------ 1건
    def _process(self, issue, years, stats):
        asset_type = hist_radar.asset_type_of(issue)

        # ① 값싼 사전 게이트 — 여기서 걸리면 백테스트 자체를 생략
        reason = hist_radar.prefilter(issue, asset_type)
        if reason:
            issue.sim_skip = reason
            stats[reason] += 1
            return

        # ② 기초자산 → 티커
        tickers = hist_radar.issue_tickers(issue)
        if not tickers:
            issue.sim_skip = SKIP_TICKER
            stats[SKIP_TICKER] += 1
            return

        bars = [int(b) for b in issue.stepdown_barriers]
        # first_eval_months가 결과를 바꾸므로 캐시 키에도 반드시 들어가야 한다.
        # 빠뜨리면 1차 평가 시점만 다른 상품끼리 서로의 결과를 덮어쓴다.
        key = (tuple(tickers), tuple(bars), issue.ki, issue.period_months,
               issue.first_eval_months, issue.issue_date)
        # 손실확률·1년내 조기상환은 수익률과 무관(수익률은 회차 수익 표시에만 쓰임)
        # → 같은 구조·같은 발행일이면 결과가 동일하므로 재사용한다.
        cached = self._sim_cache.get(key)
        if cached is not None:
            issue.sim_loss_prob, issue.sim_early_1y = cached
            stats["ok"] += 1
            return

        # ③ 시세 프레임 — **발행일 이전**만 (선견 편향 차단)
        prices, err = self._frame(tickers, issue.issue_date)
        if prices is None:
            issue.sim_skip = err
            stats[err] += 1
            return

        res = backtest.simulate(
            prices,
            barriers=bars,
            ki=issue.ki,
            is_no_ki=False,
            period_months=issue.period_months,
            # 1차 평가가 이후 주기와 다른 상품은 이걸 안 넘기면 총기간이 짧게 잡혀
            # 손실확률이 틀린다. simulate_products는 2026-08-03에 고쳤는데 여기는
            # 남아 있었다 — 지금은 대상 데이터가 0건이라 영향이 없지만
            # collect_seibro_detail 커버리지가 늘면 바로 문제가 된다. (2026-08-04)
            first_eval_months=issue.first_eval_months,
            yield_rate=issue.yield_rate,
            sample_years=years,
        )
        if not res.get("available"):
            issue.sim_skip = SKIP_PRICE
            stats[SKIP_PRICE] += 1
            self._log(f"  [{issue.isin}] 시뮬불가: {res.get('reason', '')}")
            return

        issue.sim_loss_prob = res["loss_prob_pct"]
        issue.sim_early_1y = res["early_1y_pct"]
        self._sim_cache[key] = (issue.sim_loss_prob, issue.sim_early_1y)
        stats["ok"] += 1

    def _frame(self, tickers, issue_date):
        """(티커조합, 발행일) 단위 프레임 캐시 — 같은 날 같은 기초자산 상품이 몰려 나온다."""
        key = (tuple(tickers), issue_date)
        hit = self._frame_cache.get(key)
        if hit is not None:
            return (None, hit) if isinstance(hit, str) else (hit, "")
        prices, err = hist_radar.build_price_frame(self.store, tickers, issue_date)
        if len(self._frame_cache) >= FRAME_CACHE_MAX:
            self._frame_cache.clear()   # 발행일 순 처리라 오래된 항목은 다시 안 쓰인다
        self._frame_cache[key] = err if prices is None else prices
        return prices, err

    # ------------------------------------------------------------------ 로그
    def _progress(self, done, total, stats, started):
        elapsed = (time.time() - started) / 60
        remain = (elapsed / done) * (total - done) if done else 0
        line = (f"진행 {done}/{total}, 시뮬 {stats['ok']}, 게이트미달 {stats[SKIP_GATE]}, "
                f"티커미해결 {stats[SKIP_TICKER]}, 시세부족 {stats[SKIP_PRICE]}, "
                f"경과 {elapsed:.0f}m, 예상잔여 {remain:.0f}m")
        self.stdout.write("  " + line)
        self._log(line)

    def _log(self, msg):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
        except Exception:
            pass
