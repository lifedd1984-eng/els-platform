"""
과거 레이더 재현 + 성과검증.

"당시(발행 주차) 레이더가 있었다면 어떤 상품에 배지를 줬을지"를 그룹 내 상대평가로
재현하고(radar_tier/radar_rank), 배지군과 대조군(배지 없음)의 **실제 1차 조기상환**
결과를 시세로 판정해(verdict_met) 적중률 격차를 산출한다.

■ 그룹
  issue_date가 속한 주의 월요일 × 유형(지수형/종목형).
  SEIBro엔 청약일이 없어 발행일 주차를 청약주차의 근사로 쓴다.
  레이더는 그룹 내 상대평가이므로 기준이 일관되기만 하면 된다.

■ 배지 재현
  core.models의 레이더 상수를 그대로 import해 _compute_radar_pool과 같은 순서로 판정
  (hist_radar.reproduce_radar). 상수를 바꾸면 재현 결과도 따라간다.
  시뮬값(sim_loss_prob/sim_early_1y)이 없는 건은 자동으로 배지 후보에서 빠진다
  — simulate_historical이 값싼 게이트로 미리 떨어뜨린 건들이라 원래 배지를 못 받는다.

■ 결과 판정 (배지군 + 대조군 전체)
  1차 평가일 = eval_dates[0]. 오늘보다 3일 이상 지난 건만 판정(시세 확정 대기).
  기준가는 SEIBro 공시 std_price를 쓰되, market.is_normalized_std_price로
  정규화 공시값(0·1·100·1000 등)으로 판정되면 **발행일 종가**로 대체한다.
  워스트 레벨 = min(평가일 종가 / 기준가) × 100, 충족 = 레벨 >= stepdown_barriers[0].
  시세를 못 구하면 미판정(null)으로 남겨 다음 실행 때 재시도한다.

■ 재개
  verdict_met이 이미 확정된 건은 다시 시세를 안 본다(집계에는 그대로 포함).
  평가일 미도래·시세 미확보 건은 null로 남아 다음 실행 때 재시도된다.
  배지 재현(radar_tier/rank)은 매 실행 그룹 단위로 다시 계산한다 — 상수가 바뀌면 따라간다.

사용:
  python manage.py verify_historical --limit 500       (시범, 마지막 주차가 잘려 상대평가가
                                                        일부 왜곡될 수 있으니 검증엔 전수 권장)
  python manage.py verify_historical                   (전수)
  python manage.py verify_historical --start-year 2018 --end-year 2020
"""

import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core import hist_radar, market
from core.models import HistoricalIssue

LOG_PATH = Path(settings.BASE_DIR) / "logs" / "hist_verify.log"
RESULT_PATH = Path(settings.BASE_DIR) / "logs" / "hist_result.json"

SETTLE_DAYS = 3          # 평가일 후 이 일수가 지나야 시세 확정으로 본다
TIERS = ["아주 강한 신호", "강한 신호", "배지없음"]
SAVE_FIELDS = ["radar_tier", "radar_rank", "verdict_met", "verdict_level"]


class Command(BaseCommand):
    help = "과거 주차 레이더 배지 재현 + 1차 조기상환 적중률 검증"

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int, default=2016)
        parser.add_argument("--end-year", type=int, default=2026)
        parser.add_argument("--limit", type=int, default=0, help="N건만 처리(시범용, 0=전체)")
        parser.add_argument("--delay", type=float, default=0.4,
                            help="신규 티커 조회 간격(초)")

    # ------------------------------------------------------------------ main
    def handle(self, *args, **opts):
        self.today = date.today()
        self.store = hist_radar.PriceStore(throttle=opts["delay"], logger=self._log)
        self.base_fallback = 0    # 공시 기준가 → 발행일 종가로 대체한 자산 수
        self.base_disclosed = 0   # 공시 기준가를 그대로 쓴 자산 수

        qs = HistoricalIssue.objects.filter(
            product_type="ELS", recu_whcd="공모",
            detail_fetched=True, parse_error="",
            issue_date__isnull=False,
            issue_date__year__range=(opts["start_year"], opts["end_year"]),
        )
        ids = list(qs.order_by("issue_date").values_list("id", flat=True))
        if opts["limit"]:
            ids = ids[: opts["limit"]]
        total = len(ids)
        self.stdout.write(f"검증 대상: {total}건 "
                          f"({opts['start_year']}~{opts['end_year']}, 공모 ELS)")
        if not total:
            return
        self._log(f"=== 시작 {total}건 ({opts['start_year']}~{opts['end_year']}) ===")

        # 집계 버킷: (스코프, 등급) → [건수, 적중]
        self.agg = defaultdict(lambda: [0, 0])
        stats = {"groups": 0, "badge": 0, "judged": 0, "wait": 0, "noprice": 0}
        started = time.time()
        status = "완료"
        done = 0

        try:
            # 발행일 순으로 읽어 주차가 바뀔 때마다 그 주차를 통째로 처리한다
            buf, buf_monday = [], None
            for i in range(0, total, 500):
                for issue in HistoricalIssue.objects.filter(
                        id__in=ids[i:i + 500]).order_by("issue_date"):
                    monday = hist_radar.week_monday(issue.issue_date)
                    if buf_monday is not None and monday != buf_monday:
                        done += self._flush_week(buf, buf_monday, stats)
                        self._progress(done, total, stats, started)
                        buf = []
                    buf_monday = monday
                    buf.append(issue)
            if buf:
                done += self._flush_week(buf, buf_monday, stats)
        except KeyboardInterrupt:
            status = "중단(사용자)"
            self.stdout.write(self.style.WARNING("\n중단 요청 — 처리분 저장 후 종료합니다."))

        elapsed = (time.time() - started) / 60
        summary = (
            f"{status}: 처리 {done}/{total} / 주차그룹 {stats['groups']} "
            f"/ 배지 {stats['badge']} / 판정확정 {stats['judged']} "
            f"/ 평가일미도래 {stats['wait']} / 시세미확보 {stats['noprice']} "
            f"/ 기준가대체 {self.base_fallback}(공시사용 {self.base_disclosed}) "
            f"/ 경과 {elapsed:.0f}m"
        )
        self.stdout.write(self.style.SUCCESS(summary))
        self._log(summary)

        self._report(opts)

    # ------------------------------------------------------------------ 주차 처리
    def _flush_week(self, issues, monday, stats):
        """한 주차를 유형별로 나눠 배지 재현 + 결과 판정 후 저장. 처리 건수 반환."""
        if not issues:
            return 0
        by_type = defaultdict(list)
        for p in issues:
            by_type[hist_radar.asset_type_of(p)].append(p)

        for asset_type, members in by_type.items():
            stats["groups"] += 1
            tiers = hist_radar.reproduce_radar(members, asset_type)
            for p in members:
                tier, rank = tiers.get(p.isin, ("", None))
                p.radar_tier = tier
                p.radar_rank = rank
                if tier:
                    stats["badge"] += 1

                if p.verdict_met is None:      # 재개: 이미 확정된 건은 다시 값을 안 매긴다
                    self._judge(p, stats)
                if p.verdict_met is not None:
                    stats["judged"] += 1
                    self._count(p, tier or "배지없음", asset_type)

        HistoricalIssue.objects.bulk_update(issues, SAVE_FIELDS)
        return len(issues)

    def _judge(self, issue, stats):
        """1차 평가일 워스트 레벨 판정. 판정 불가면 값을 남기지 않는다(null 유지)."""
        evals = issue.eval_dates or []
        bars = issue.stepdown_barriers or []
        if not evals or not bars or not isinstance(bars[0], (int, float)):
            return
        try:
            eval_date = date.fromisoformat(str(evals[0])[:10])
        except ValueError:
            return
        if eval_date > self.today - timedelta(days=SETTLE_DAYS):
            stats["wait"] += 1
            return

        worst = None
        for asset in (issue.assets or []):
            if not isinstance(asset, dict):
                return
            ticker = hist_radar.resolve_asset_ticker(asset)
            if not ticker or self.store.get(ticker) is None:
                stats["noprice"] += 1
                return
            issue_close = self.store.close_on(ticker, issue.issue_date)
            # 공시 기준가가 정규화 값(0·1·100·1000 등)이면 발행일 종가로 대체
            if issue_close is None:
                stats["noprice"] += 1
                return
            if market.is_normalized_std_price(asset.get("std_price"), issue_close):
                ref = issue_close
                self.base_fallback += 1
            else:
                ref = float(asset["std_price"])
                self.base_disclosed += 1
            ev = self.store.close_on(ticker, eval_date)
            if not ev or ref <= 0:
                stats["noprice"] += 1
                return
            level = ev / ref * 100
            if worst is None or level < worst:
                worst = level

        if worst is None:
            return
        issue.verdict_level = round(worst, 1)
        issue.verdict_met = issue.verdict_level >= bars[0]

    # ------------------------------------------------------------------ 집계
    def _count(self, issue, tier, asset_type):
        hit = 1 if issue.verdict_met else 0
        for scope in ("전체", f"연도:{issue.issue_date.year}", f"유형:{asset_type}"):
            cell = self.agg[(scope, tier)]
            cell[0] += 1
            cell[1] += hit

    def _report(self, opts):
        """전체/연도별/유형별 적중률 + 배지 vs 대조군 격차(%p) 출력 및 JSON 저장."""
        scopes = sorted({s for s, _ in self.agg},
                        key=lambda s: (s.split(":")[0] != "전체", s))
        out = {}
        lines = ["", "=" * 72,
                 "레이더 재현 성과 (1차 조기상환 적중률)", "=" * 72,
                 f"{'구분':<12}{'등급':<12}{'건수':>8}{'적중':>8}{'적중률':>9}"]

        for scope in scopes:
            cells = {t: self.agg.get((scope, t), [0, 0]) for t in TIERS}
            badge_n = cells[TIERS[0]][0] + cells[TIERS[1]][0]
            badge_hit = cells[TIERS[0]][1] + cells[TIERS[1]][1]
            ctrl_n, ctrl_hit = cells["배지없음"]
            badge_rate = badge_hit / badge_n * 100 if badge_n else None
            ctrl_rate = ctrl_hit / ctrl_n * 100 if ctrl_n else None
            gap = (round(badge_rate - ctrl_rate, 1)
                   if badge_rate is not None and ctrl_rate is not None else None)

            entry = {"tiers": {}, "badge": _cell(badge_n, badge_hit),
                     "control": _cell(ctrl_n, ctrl_hit), "gap_pp": gap}
            for t in TIERS:
                n, hit = cells[t]
                entry["tiers"][t] = _cell(n, hit)
                lines.append(f"{scope:<12}{t:<12}{n:>8}{hit:>8}"
                             f"{(f'{hit / n * 100:.1f}%' if n else '-'):>9}")
            lines.append(f"{scope:<12}{'배지합계':<12}{badge_n:>8}{badge_hit:>8}"
                         f"{(f'{badge_rate:.1f}%' if badge_n else '-'):>9}"
                         f"   대조군 대비 {gap if gap is not None else '-'}%p")
            lines.append("-" * 72)
            out[scope] = entry

        for line in lines:
            self.stdout.write(line)
            self._log(line)

        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "range": [opts["start_year"], opts["end_year"]],
            "settle_days": SETTLE_DAYS,
            "base_price": {"공시값사용": self.base_disclosed,
                           "발행일종가대체": self.base_fallback},
            "scopes": out,
        }
        try:
            RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESULT_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.stdout.write(f"요약 저장: {RESULT_PATH}")
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"요약 저장 실패: {e}"))

    # ------------------------------------------------------------------ 로그
    def _progress(self, done, total, stats, started):
        # 주차 단위(수십 건)로 호출되므로 500건마다만 찍는다
        if done - getattr(self, "_last_log", 0) < 500:
            return
        self._last_log = done
        elapsed = (time.time() - started) / 60
        remain = (elapsed / done) * (total - done) if done else 0
        line = (f"진행 {done}/{total}, 그룹 {stats['groups']}, 배지 {stats['badge']}, "
                f"판정 {stats['judged']}, 대기 {stats['wait']}, "
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


def _cell(n, hit):
    return {"n": n, "hit": hit, "rate": round(hit / n * 100, 1) if n else None}
