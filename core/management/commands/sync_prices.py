"""기초자산 일별 시세(PriceBar) 적재 — 백필과 증분 갱신을 한 명령으로.

왜 필요한가
    지금 상품상세는 **요청 처리 중에** yfinance를 동기 호출한다(core/views.py).
    자산 4개면 왕복 최대 8회고, 캐시는 워커 프로세스 메모리 dict 하나뿐이라
    재시작하면 사라진다(core/market.py `_history_cache`).
    "10년 추이·누적수익률·최대낙폭"을 자유질의로 답하려면 시세가 **DB에**
    있어야 한다. 이 명령이 그 적재기다.

핵심 정책 — auto_adjust=False 고정
    yfinance는 auto_adjust=False일 때 Close(미조정)와 Adj Close(조정)를
    **함께** 준다(2026-08-06 실측 확인). 그래서 한 번 호출로 둘 다 저장한다.
      · 수익률·최대낙폭 → adj_close  (배당락을 손실로 오인하지 않음)
      · 낙인·기준가     → close      (증권사 고시 종가와 같은 계열)
    하나만 저장하면 나중에 다른 쪽을 복원할 수 없다. PriceBar 주석 참고.

실패를 삼키지 않는다
    기존 `market.fetch_history`는 실패해도 빈 리스트만 돌려줘서
    "시세 없음"과 "네트워크 실패"가 구분되지 않았다(market.py:709-710).
    이 명령은 티커마다 수신/빈응답/실패를 분류해 집계하고, 실패율이
    임계치를 넘으면 종료코드 1 + 텔레그램 경보로 눈에 띄게 만든다.
    추가로 **정체(stale)** 도 본다 — 같은 실행의 다른 티커들은 최신인데
    혼자만 며칠 뒤처진 계열을 잡아낸다(^KS200이 2026-07-17부터 멈춘 사례).

사용 예
    python manage.py sync_prices                          # 증분, dry-run
    python manage.py sync_prices --apply                  # 증분, 실제 저장
    python manage.py sync_prices --mode backfill --years 10 --apply
    python manage.py sync_prices --scope all --mode backfill --apply
    python manage.py sync_prices --tickers ^KS200,069500.KS --apply
"""

import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from core import market
from core.models import HistoricalIssue, Investment, PriceBar, Product

# ── 주 계열 → 결측을 메울 보조 계열 ──────────────────────────────────
# 코스피200: 지수 ^KS200이 주 계열, KODEX200 ETF(069500.KS)가 보조.
#   지수는 최근 구간이 통째로 비고(실측 2026-07-17~08-05 13거래일) ETF는
#   2023-07~08에 비어 있다 — **서로 보완된다**.
#   누적수익률은 계열 선택이 결정적이라(10년 지수 +325.7% vs ETF조정 +401.4%)
#   지수를 주 계열로 둔다. 최대낙폭은 어느 쪽이든 0.4%p 차이라 무관하다.
#
# ⚠ 코스닥150(229200.KS)은 **일부러 넣지 않았다.**
#   야후에 코스닥150 지수 티커가 없다(^KQ150·^KOSDAQ150 모두 404, 2026-08-06 확인).
#   ^KQ11은 코스닥 **종합**지수라 다른 지수다 — 229200.KS와 일간수익률 상관
#   0.9484, 10년 누적 +13.6% vs +29.6%로 궤적이 갈린다. 이걸로 메우면
#   남의 지수 움직임을 섞는 셈이라, 229200.KS는 단독으로 둔다.
#   (조정·미조정을 함께 저장하는 이득은 229200.KS도 똑같이 받는다.)
FILL_PAIRS = {"^KS200": "069500.KS"}

# 지수 계열을 ETF로 메울 때 쓸 스케일 계수의 상식 범위.
# 코스피200:KODEX200은 ~1/100 (실측 비율 99.35~102.05).
# 이 범위를 벗어나면 계열이 뒤바뀌었거나 데이터가 깨진 것 → 메우지 않는다.
FILL_SCALE_MIN, FILL_SCALE_MAX = 1e-4, 1e4
# 접합 기준일이 이보다 오래되면 메우지 않는다 (오래된 비율은 못 믿는다)
FILL_ANCHOR_MAX_GAP_DAYS = 90

# ── 정체(stale) 판정 ────────────────────────────────────────────────
# 조용히 멈춘 계열을 잡되, **매일 울리는 경보는 만들지 않는다.**
# 알림이 매일 오면 사람은 곧 무시하고, 그러면 진짜 고장을 놓친다.
#
# 설계 셋:
#  ① 시장별로 비교한다. 전체 최신일과 비교하면 추석·골든위크 때 그 시장
#     티커가 전부 뒤처져 보인다 (실측: 2017-09-29~10-10 국내 11일 연휴,
#     2019-04-26~05-07 일본 11일). 같은 시장 동료들과 비교하면 연휴는
#     다 같이 밀리므로 아무도 튀지 않고, 혼자 멈춘 계열만 드러난다.
#  ② 보완으로 메워진 계열은 정체가 아니다. ^KS200이 야후에서 안 내려오는 건
#     고장이 아니라 **이 심볼의 평소 성질**이고(core/market.py:11 — 그래서
#     서비스가 원래 KODEX200 ETF를 쓴다), 069500.KS로 메워 데이터에 구멍이
#     없으면 조치할 게 없다. 경보 대상은 '메울 대체 계열이 없는데 멈춘 것'이다.
#  ③ 동료가 없는 단독 시장(^N225 등)은 전체 최신일과 비교하되 임계를 넉넉히.
#  ④ **현재 쓰는 티커만** 본다. --scope historical로 끌어오는 과거 자산에는
#     상장폐지·합병으로 계열이 정상 종료된 종목이 섞여 있다(실측 294티커 중 11개:
#     130960.KS 2018-07-17, 016170.KS 2018-09-17, 192530.KS 2018-10-25 …).
#     이건 고장이 아니라 사실이고, 매일 경보로 울리면 진짜 고장이 묻힌다.
#     현행 상품·보유에 걸린 티커가 멈추면 그건 진짜 경보다.
STALE_PEER_DAYS = 5       # 같은 시장 동료 최신일 대비 (연휴는 동료도 같이 밀린다)
STALE_SOLO_DAYS = 14      # 동료 없는 시장 — 최장 연휴(11일)를 넘겨 잡는다

# 티커 → 시장. 거래 달력이 같은 것끼리 묶는 게 목적이라 정밀한 분류는 필요 없다.
_MARKET_INDEX = {
    "^KS200": "KR", "^N225": "JP", "^HSI": "HK", "^HSCE": "HK",
    "^STOXX50E": "EU", "^GDAXI": "EU", "^FCHI": "EU", "^FTSE": "EU", "^SX7E": "EU",
    "^TWII": "TW", "^AXJO": "AU",
}


def market_of(ticker: str) -> str:
    """티커가 속한 거래 달력 그룹. 정체 판정에서 같은 그룹끼리 비교한다."""
    if ticker in _MARKET_INDEX:
        return _MARKET_INDEX[ticker]
    if ticker.endswith((".KS", ".KQ", ".KN")):
        return "KR"
    if ticker.endswith((".SS", ".SZ")):
        return "CN"
    if ticker.endswith(".T"):
        return "JP"
    if ticker.endswith(".HK"):
        return "HK"
    return "US"

UPDATE_FIELDS = ["open", "high", "low", "close", "adj_close", "volume",
                 "source", "source_ticker", "scale", "updated_at"]


class Command(BaseCommand):
    help = "기초자산 일별 시세를 PriceBar에 적재 (기본 dry-run, 저장은 --apply)"

    # ──────────────────────────────────────────────────────────
    def add_arguments(self, parser):
        parser.add_argument(
            "--mode", choices=["daily", "backfill"], default="daily",
            help="daily=마지막 저장일 이후만(기본) / backfill=--years 전 구간")
        parser.add_argument("--years", type=int, default=10,
                            help="backfill 창 (기본 10년)")
        parser.add_argument(
            "--scope", choices=["current", "historical", "all"], default="current",
            help="current=현행 상품+보유(기본) / historical=SEIBro 발행이력 / all=둘 다")
        parser.add_argument("--since-year", type=int, default=2016,
                            help="historical 스코프의 발행연도 하한 (기본 2016)")
        parser.add_argument("--tickers", default="",
                            help="쉼표 구분 티커 직접 지정 (지정 시 --scope 무시)")
        parser.add_argument("--apply", action="store_true",
                            help="실제 저장. 없으면 dry-run(조회만)")
        parser.add_argument("--batch-size", type=int, default=40,
                            help="yf.download 한 묶음 티커 수 (기본 40)")
        parser.add_argument("--overlap-days", type=int, default=7,
                            help="증분 시 마지막 저장일에서 되짚을 달력일 (확정치 정정 반영)")
        parser.add_argument("--include-today", action="store_true",
                            help="오늘 봉도 저장. 기본은 제외 — 배치가 09:30 KST에 "
                                 "돌아 장중 미완성 봉이 종가로 들어가기 때문")
        parser.add_argument("--fail-pct", type=float, default=10.0,
                            help="실패율(%%) 임계치. 넘으면 종료코드 1 + 경보")
        # ⚠ 텔레그램은 **명시적으로 켜야만** 나간다 (기본 발송 안 함).
        #   2026-08-06 로컬 dry-run 테스트가 운영 채널로 실제 알림을 쐈다.
        #   .env의 토큰·chat_id가 로컬에도 그대로 있어 개발/운영 구분이 없다.
        #   그래서 옵트인으로 뒤집고, dry-run에서는 --notify를 줘도 막는다.
        parser.add_argument("--notify", action="store_true",
                            help="임계치 초과 시 텔레그램 발송. 기본은 발송 안 함. "
                                 "--apply 없이는 무시된다(테스트 오발송 방지)")
        parser.add_argument("--throttle", type=float, default=0.0,
                            help="배치 사이 대기 초")

    # ──────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        self.apply = opts["apply"]
        # dry-run에서도 보완 결과를 보여주려고 이번 실행에서 받은 종가를 들고 있는다.
        # FILL_PAIRS에 걸린 티커만 담아 메모리를 아낀다.
        self._pending = {}
        started = time.time()
        today = date.today()

        targets = self._resolve_targets(opts)
        if not targets:
            raise CommandError("적재 대상 티커가 없다 — --scope/--tickers 확인")

        mode = opts["mode"]
        self.stdout.write(
            f"[대상] {len(targets)}티커 · mode={mode} · scope={opts['scope']} · "
            f"{'APPLY(저장)' if self.apply else 'DRY-RUN(조회만)'}")

        # ── 티커별 조회 시작일 결정 ────────────────────────────
        backfill_start = today - timedelta(days=int(opts["years"] * 365.25) + 1)
        # primary_only — 보완 행은 이어받기 기준으로 쓰지 않는다.
        # ^KS200처럼 결측을 메운 계열은 보완 행 날짜가 더 최신이라, 그걸 기준으로
        # 삼으면 원본 시세가 복구돼도 그 구간을 다시 조회하지 않아 보완값이 남는다.
        last_dates = PriceBar.last_dates(targets, primary_only=True)
        starts = {}
        for tk in targets:
            if mode == "backfill" or tk not in last_dates:
                starts[tk] = backfill_start
            else:
                starts[tk] = last_dates[tk] - timedelta(days=opts["overlap_days"])
        new_tickers = [t for t in targets if t not in last_dates]
        if mode == "daily" and new_tickers:
            self.stdout.write(f"  · 신규 티커 {len(new_tickers)}개는 {backfill_start}부터 전체 적재")

        # ── 시작일이 같은 것끼리 묶어 배치 조회 ────────────────
        buckets = {}
        for tk, s in starts.items():
            buckets.setdefault(s, []).append(tk)

        stats = {}          # 티커 → dict(rows, saved, first, last, status, note)
        # yfinance end는 배타적 → end=today면 **어제까지**.
        # 09:30 KST 배치 시점에 KRX는 개장 직후라 오늘 봉은 장중 미완성값이다.
        # 그걸 종가로 저장하면 이 테이블의 존재 이유(믿을 수 있는 원본)가 깨진다.
        # 장 마감 후 수동 실행할 때만 --include-today.
        end = today + timedelta(days=1) if opts["include_today"] else today
        for start in sorted(buckets):
            group = sorted(buckets[start])
            for i in range(0, len(group), opts["batch_size"]):
                chunk = group[i:i + opts["batch_size"]]
                self._run_chunk(chunk, start, end, stats)
                if opts["throttle"]:
                    time.sleep(opts["throttle"])

        # ── 실패·빈응답 티커 개별 재시도 1회 ────────────────────
        retry = [t for t, s in stats.items() if s["status"] in ("실패", "빈응답")]
        if retry:
            self.stdout.write(f"[재시도] {len(retry)}티커 개별 조회")
            for tk in retry:
                self._run_chunk([tk], starts[tk], end, stats, retry=True)

        # ── 코스피200 두 계열 보완 ──────────────────────────────
        fills = self._fill_gaps(targets, stats)

        # ── 집계 ───────────────────────────────────────────────
        self._report(stats, fills, targets, started, opts)

    # ──────────────────────────────────────────────────────────
    # 1. 적재 대상 티커
    # ──────────────────────────────────────────────────────────
    def _resolve_targets(self, opts):
        raw = (opts["tickers"] or "").strip()
        if raw:
            tickers = {t.strip() for t in raw.split(",") if t.strip()}
            # 직접 지정한 티커는 '보고 있는 것'으로 본다 (정체 판정 대상)
            self.live = set(tickers)
        else:
            tickers = set()
            self.live = set()
            if opts["scope"] in ("current", "all"):
                self.live = self._current_tickers()
                tickers |= self.live
            if opts["scope"] in ("historical", "all"):
                tickers |= self._historical_tickers(opts["since_year"])

        # 보완 쌍은 양쪽이 다 있어야 메울 수 있다 → 한쪽만 지정돼도 짝을 끌어온다
        for primary, secondary in FILL_PAIRS.items():
            if primary in tickers or secondary in tickers:
                tickers |= {primary, secondary}
                if primary in self.live or secondary in self.live:
                    self.live |= {primary, secondary}
        return sorted(tickers)

    def _current_tickers(self):
        """현행 상품(Product) + 보유 투자(Investment)의 기초자산 티커.

        일일 배치가 매일 갱신해야 할 **최소 집합**이다. 실측 42개.
        미해결 자산은 환율·금리형(DLS 계열)이 대부분이라 yfinance 대상이 아니다.
        """
        out, unresolved = set(), set()
        rows = list(Product.objects.values_list("assets_raw", flat=True))
        rows += list(Investment.objects.filter(status="보유중")
                     .values_list("product__assets_raw", flat=True))
        for raw in rows:
            for name in market.split_assets(raw or ""):
                tk = market.resolve_ticker(name)
                (out.add(tk) if tk else unresolved.add(name))
        if unresolved:
            self.stdout.write(
                f"  · 티커 미해결 자산명 {len(unresolved)}개(적재 제외): "
                + ", ".join(sorted(unresolved)[:12])
                + (" …" if len(unresolved) > 12 else ""))
        return out

    def _historical_tickers(self, since_year):
        """SEIBro 발행이력(HistoricalIssue)의 기초자산 티커.

        자유질의가 과거 상품까지 다루려면 필요하지만 티커 수가 크게 는다
        (전체 355 / 2016년 이후 293 — 2026-08-06 실측). 그래서 기본 스코프에서
        빼고 --scope historical|all 로만 켠다.
        """
        qs = HistoricalIssue.objects.all()
        if since_year:
            qs = qs.filter(issue_date__gte=date(since_year, 1, 1))
        out = set()
        for assets in qs.values_list("assets", flat=True).iterator(chunk_size=5000):
            if not isinstance(assets, (list, tuple)):
                continue
            for a in assets:
                tk = market.resolve_seibro_ticker(a)
                if tk:
                    out.add(tk)
        return out

    # ──────────────────────────────────────────────────────────
    # 2. 다운로드 + 저장
    # ──────────────────────────────────────────────────────────
    def _run_chunk(self, chunk, start, end, stats, retry=False):
        import pandas as pd
        import yfinance as yf

        t0 = time.time()
        try:
            df = yf.download(
                chunk, start=start.isoformat(), end=end.isoformat(),
                auto_adjust=False,          # ← Close(미조정)+Adj Close(조정) 동시 수신
                actions=False, threads=True, progress=False,
                group_by="column",
            )
        except Exception as e:                        # noqa: BLE001
            for tk in chunk:
                stats[tk] = dict(rows=0, saved=0, first=None, last=None,
                                 status="실패", note=f"{type(e).__name__}: {e}")
            self.stderr.write(self.style.ERROR(
                f"  [배치실패] {len(chunk)}티커 {type(e).__name__}: {e}"))
            return

        if df is None or df.empty:
            for tk in chunk:
                stats[tk] = dict(rows=0, saved=0, first=None, last=None,
                                 status="빈응답", note="응답 비어 있음")
            return

        multi = isinstance(df.columns, pd.MultiIndex)
        for tk in chunk:
            try:
                sub = self._slice(df, tk, multi)
            except KeyError:
                stats[tk] = dict(rows=0, saved=0, first=None, last=None,
                                 status="빈응답", note="응답에 티커 없음")
                continue
            bars, note = self._to_bars(tk, sub)
            if tk in FILL_PAIRS or tk in FILL_PAIRS.values():
                self._pending[tk] = {b.date: b.close for b in bars}
            saved = self._save(bars)
            stats[tk] = dict(
                rows=len(bars), saved=saved,
                first=bars[0].date if bars else None,
                last=bars[-1].date if bars else None,
                status="수신" if bars else "빈응답",
                note=note if bars else "유효 종가 0행",
            )
        self.stdout.write(
            f"  [{'재시도' if retry else '배치'}] {len(chunk)}티커 "
            f"{time.time() - t0:.1f}s "
            f"수신 {sum(1 for t in chunk if stats[t]['status'] == '수신')}")

    @staticmethod
    def _slice(df, ticker, multi):
        """배치 응답에서 티커 하나의 OHLCV 프레임을 뽑는다."""
        if not multi:
            return df
        lv0 = set(df.columns.get_level_values(0))
        # group_by="column" → level0=Price(Close/Adj Close/…), level1=Ticker
        if "Close" in lv0 or "Adj Close" in lv0:
            sub = df.xs(ticker, axis=1, level=1)
        else:                                  # group_by="ticker" 형태 대비
            sub = df[ticker]
        return sub

    def _to_bars(self, ticker, sub):
        """OHLCV 프레임 → PriceBar 인스턴스 리스트.

        · close(미조정)가 없는 행은 버린다 — 종가가 앵커라 없으면 못 쓴다.
          (실측: ^KS200은 거래일 행은 있는데 Close만 NaN인 날이 있다)
        · 날짜는 tz 제거 후 date로 — 거래소 타임존이 섞이면 같은 날이 갈린다.
        """
        import pandas as pd

        cols = set(sub.columns)
        if "Close" not in cols:
            return [], "Close 열 없음"
        idx = pd.DatetimeIndex(sub.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        idx = idx.normalize()

        def col(name):
            return sub[name].to_numpy() if name in cols else [None] * len(sub)

        o, h, lo = col("Open"), col("High"), col("Low")
        c, ac, v = col("Close"), col("Adj Close"), col("Volume")

        bars, no_adj = [], 0
        for i, ts in enumerate(idx):
            close = _f(c[i])
            if close is None:
                continue                     # 종가 없는 행은 저장하지 않는다
            adj = _f(ac[i])
            if adj is None:
                no_adj += 1
            bars.append(PriceBar(
                ticker=ticker, date=ts.date(),
                open=_f(o[i]), high=_f(h[i]), low=_f(lo[i]),
                close=close, adj_close=adj, volume=_i(v[i]),
                source=PriceBar.SOURCE_PRIMARY, source_ticker="", scale=None,
            ))
        note = f"조정종가 결측 {no_adj}행" if no_adj else ""
        return bars, note

    def _save(self, bars):
        """(ticker, date) 유니크 기준 업서트. dry-run이면 0."""
        if not self.apply or not bars:
            return 0
        PriceBar.objects.bulk_create(
            bars, batch_size=2000, update_conflicts=True,
            unique_fields=["ticker", "date"], update_fields=UPDATE_FIELDS,
        )
        return len(bars)

    # ──────────────────────────────────────────────────────────
    # 3. 코스피200 두 계열 보완
    # ──────────────────────────────────────────────────────────
    def _fill_gaps(self, targets, stats):
        """주 계열 결측일을 보조 계열로 메운다 (스케일 비율 접합).

        ⚠ 값을 조작하는 유일한 지점이다. 그래서
          · 원본(source=원본) 행은 **절대 덮어쓰지 않는다**
          · 메운 행은 source=보완 / source_ticker / scale 로 표시해
            나중에 원본과 구분할 수 있게 한다
          · 스케일은 **직전 공통 거래일** 비율로 잡는다. 비율 접합이라
            보조계열의 일간수익률이 그대로 보존된다(누적수익률·낙폭이 목적).
          · 거래량은 계열이 다르면 의미가 달라지므로 옮기지 않는다(None).
        """
        out = []
        for primary, secondary in FILL_PAIRS.items():
            if primary not in targets or secondary not in targets:
                continue
            info = self._fill_one(primary, secondary)
            info["primary"], info["secondary"] = primary, secondary
            out.append(info)
        return out

    def _fill_one(self, primary, secondary):
        prim = {d: c for d, c in PriceBar.objects.filter(
            ticker=primary, close__isnull=False).values_list("date", "close")}
        prim_src = dict(PriceBar.objects.filter(ticker=primary)
                        .values_list("date", "source"))
        sec = {d: c for d, c in PriceBar.objects.filter(
            ticker=secondary, close__isnull=False).values_list("date", "close")}
        # dry-run이면 DB에 아직 아무것도 없다 → 이번에 받은 값으로 시뮬레이션한다.
        # apply 모드에선 이미 저장된 값과 같아 결과가 달라지지 않는다.
        for d, c in self._pending.get(primary, {}).items():
            if c is not None and d not in prim:
                prim[d] = c
                prim_src.setdefault(d, PriceBar.SOURCE_PRIMARY)
        for d, c in self._pending.get(secondary, {}).items():
            if c is not None:
                sec.setdefault(d, c)
        info = dict(primary_rows=len(prim), secondary_rows=len(sec),
                    filled=0, skipped=0, dates=[], scale_min=None, scale_max=None,
                    gap_diagnostics=None)
        if not prim or not sec:
            return info

        # 원본 구간 밖(보조계열이 더 이른 시작)은 메우지 않는다 —
        # 접합 기준으로 삼을 앞선 공통일이 없어 스케일을 못 정한다.
        prim_first = min(prim)
        # 이미 '보완'으로 채워 둔 날은 다시 계산해도 되지만, '원본' 날은 건드리지 않는다.
        candidates = sorted(d for d in sec
                            if d >= prim_first
                            and (d not in prim or prim_src.get(d) == PriceBar.SOURCE_FILLED))

        common = sorted(d for d in prim if d in sec and prim_src.get(d) != PriceBar.SOURCE_FILLED)
        if not common:
            return info

        import bisect
        bars, scales = [], []
        for d in candidates:
            j = bisect.bisect_right(common, d) - 1
            if j < 0:
                info["skipped"] += 1
                continue
            anchor = common[j]
            if (d - anchor).days > FILL_ANCHOR_MAX_GAP_DAYS:
                info["skipped"] += 1
                continue
            if not sec.get(anchor):
                info["skipped"] += 1
                continue
            k = prim[anchor] / sec[anchor]
            if not (FILL_SCALE_MIN <= k <= FILL_SCALE_MAX):
                info["skipped"] += 1
                continue
            val = sec[d] * k
            bars.append(PriceBar(
                ticker=primary, date=d,
                open=None, high=None, low=None,
                close=val,
                # 주 계열이 가격지수(배당 미반영)라 조정=미조정이다.
                # 실측: ^KS200은 Close와 Adj Close가 전 구간 완전히 같다.
                adj_close=val,
                volume=None,                      # 다른 계열의 거래량은 옮기지 않는다
                source=PriceBar.SOURCE_FILLED,
                source_ticker=secondary, scale=k,
            ))
            scales.append(k)

        if scales:
            info["scale_min"], info["scale_max"] = min(scales), max(scales)
        info["filled"] = len(bars)
        info["dates"] = [b.date for b in bars]
        info["gap_diagnostics"] = self._gap_diagnostics(prim, sec, common)
        if bars:
            self._save(bars)
        return info

    @staticmethod
    def _gap_diagnostics(prim, sec, common):
        """두 계열이 얼마나 벌어지는지 실측 — 보고용."""
        if len(common) < 2:
            return None
        ratios = [prim[d] / sec[d] for d in common if sec[d]]
        # 일간수익률 절대차 (같은 날 둘 다 있는 연속 구간만)
        diffs = []
        for a, b in zip(common, common[1:]):
            if prim.get(a) and prim.get(b) and sec.get(a) and sec.get(b):
                diffs.append(abs((prim[b] / prim[a]) - (sec[b] / sec[a])) * 100)
        return dict(
            common_days=len(common),
            ratio_min=min(ratios) if ratios else None,
            ratio_max=max(ratios) if ratios else None,
            ret_diff_mean=(sum(diffs) / len(diffs)) if diffs else None,
            ret_diff_max=max(diffs) if diffs else None,
        )

    # ──────────────────────────────────────────────────────────
    # 4. 집계 보고 + 경보
    # ──────────────────────────────────────────────────────────
    def _report(self, stats, fills, targets, started, opts):
        ok = [t for t, s in stats.items() if s["status"] == "수신"]
        empty = [t for t, s in stats.items() if s["status"] == "빈응답"]
        failed = [t for t, s in stats.items() if s["status"] == "실패"]
        rows = sum(s["rows"] for s in stats.values())
        saved = sum(s["saved"] for s in stats.values())

        # ── 정체 감지 (상수 옆 주석에 설계 근거) ─────────────────
        # 판정은 **보완 후 유효 최신일**로, 비교는 **같은 시장 동료**와 한다.
        filled_last = {}
        for f in fills:
            if f["dates"]:
                filled_last[f["primary"]] = max(f["dates"])
        effective = {}
        for t, s in stats.items():
            cands = [d for d in (s["last"], filled_last.get(t)) if d]
            if cands:
                effective[t] = max(cands)
        cohort = max(effective.values()) if effective else None

        peer_max = {}
        for t, last in effective.items():
            m = market_of(t)
            peer_max[m] = max(peer_max.get(m, last), last)
        peer_n = {}
        for t in effective:
            m = market_of(t)
            peer_n[m] = peer_n.get(m, 0) + 1

        stale, ended = [], []
        for t, last in effective.items():
            m = market_of(t)
            if peer_n[m] >= 2:                      # 동료 있음 → 시장 안에서 비교
                gap, limit, basis = (peer_max[m] - last).days, STALE_PEER_DAYS, f"{m}동료"
            elif cohort:                            # 단독 시장 → 전체와 비교, 임계 완화
                gap, limit, basis = (cohort - last).days, STALE_SOLO_DAYS, "전체"
            else:
                continue
            if gap <= limit:
                continue
            if t in getattr(self, "live", set()):
                stale.append((t, last, gap, basis, limit))   # 지금 쓰는 계열 → 진짜 경보
            else:
                ended.append((t, last, gap))                 # 이력 전용 → 정상 종료로 본다

        # 보완으로 메워진 계열은 경보가 아니라 정보다
        covered = [(t, stats[t]["last"], filled_last[t])
                   for t in filled_last
                   if t in stats and stats[t]["last"]
                   and filled_last[t] > stats[t]["last"]]

        self.stdout.write("")
        self.stdout.write("━" * 62)
        self.stdout.write(
            f"[결과] 대상 {len(targets)} / 수신 {len(ok)} / 빈응답 {len(empty)} / 실패 {len(failed)}")
        self.stdout.write(
            f"       수신 행 {rows:,} / 저장 {saved:,}"
            f"{'  (dry-run — 저장 안 함)' if not self.apply else ''}"
            f" / {time.time() - started:.1f}s")
        if ok:
            firsts = [stats[t]["first"] for t in ok if stats[t]["first"]]
            self.stdout.write(f"       구간 {min(firsts)} ~ {cohort}")

        for label, group in (("빈응답", empty), ("실패", failed)):
            if not group:
                continue
            writer = self.stderr.write if label == "실패" else self.stdout.write
            writer(f"  [{label} {len(group)}]")
            for t in sorted(group)[:30]:
                writer(f"    - {t}: {stats[t]['note']}")
            if len(group) > 30:
                writer(f"    … 외 {len(group) - 30}개")

        notes = [(t, s["note"]) for t, s in stats.items() if s["status"] == "수신" and s["note"]]
        if notes:
            self.stdout.write(f"  [주의 {len(notes)}]")
            for t, n in sorted(notes)[:20]:
                self.stdout.write(f"    - {t}: {n}")

        if covered:
            for t, src_last, fill_last in covered:
                self.stdout.write(
                    f"  [보완으로 해소] {t}: 원본 {src_last}까지 → 보완 후 {fill_last} "
                    f"(경보 아님 — 이 계열의 평소 성질)")
        if ended:
            self.stdout.write(
                f"  [종료된 계열 {len(ended)}] 이력 전용 티커 — 상장폐지·합병으로 "
                f"시세가 끝난 것으로 본다 (경보 아님)")
            for t, last, gap in sorted(ended, key=lambda x: -x[2])[:10]:
                self.stdout.write(f"    - {t}: 마지막 {last}")
            if len(ended) > 10:
                self.stdout.write(f"    … 외 {len(ended) - 10}개")
        if stale:
            self.stdout.write(self.style.WARNING(
                f"  [정체 {len(stale)}] 현행 상품·보유에 걸린 계열이 동료보다 뒤처짐 "
                f"(보완으로도 못 메움)"))
            for t, last, gap, basis, limit in sorted(stale, key=lambda x: -x[2])[:20]:
                self.stdout.write(self.style.WARNING(
                    f"    - {t}: 마지막 {last} ({basis} 대비 {gap}일, 임계 {limit}일)"))

        for f in fills:
            self.stdout.write("")
            self.stdout.write(
                f"[보완] {f['primary']} ← {f['secondary']}  "
                f"주계열 {f['primary_rows']:,}행 / 보조 {f['secondary_rows']:,}행")
            self.stdout.write(
                f"       메운 날 {f['filled']}일 / 건너뜀 {f['skipped']}일"
                + (f" / 스케일 {f['scale_min']:.5f}~{f['scale_max']:.5f}"
                   if f["scale_min"] else ""))
            if f["dates"]:
                ds = [d.isoformat() for d in f["dates"]]
                self.stdout.write("       " + ", ".join(ds[:8])
                                  + (f" … {ds[-1]}" if len(ds) > 8 else ""))
            g = f["gap_diagnostics"]
            if g:
                self.stdout.write(
                    f"       두 계열 괴리 — 공통 {g['common_days']}일 / "
                    f"비율 {g['ratio_min']:.5f}~{g['ratio_max']:.5f} / "
                    f"일간수익률 절대차 평균 {g['ret_diff_mean']:.4f}%p "
                    f"최대 {g['ret_diff_max']:.4f}%p")
        self.stdout.write("━" * 62)

        # ── 임계치 판정 ────────────────────────────────────────
        # 실패 = 예외. 빈응답은 증분 모드에선 정상(휴장·신규 데이터 없음)이라
        # 백필에서만 실패로 함께 센다.
        bad = list(failed)
        if opts["mode"] == "backfill":
            bad += empty
        pct = len(bad) / len(targets) * 100 if targets else 0
        if pct <= opts["fail_pct"] and not stale:
            return

        msg = (f"[시세적재 경보] 대상 {len(targets)} 중 실패 {len(failed)}"
               f"/빈응답 {len(empty)} (실패율 {pct:.1f}%, 임계 {opts['fail_pct']}%)")
        if stale:
            msg += "\n정체 계열: " + ", ".join(
                f"{t}(마지막 {last}, {basis} 대비 {gap}일)"
                for t, last, gap, basis, _ in stale[:10])
        self.stderr.write(self.style.ERROR(msg))

        # ⚠ 발송은 **옵트인 + apply 모드**에서만.
        #   로컬 dry-run 테스트가 운영 텔레그램으로 나가는 사고를 막는다
        #   (.env의 토큰·chat_id가 로컬에도 그대로라 개발/운영 구분이 없다).
        if opts["notify"] and self.apply:
            from core import telegram
            telegram.send_message("ELS 레이더 " + msg)
            self.stdout.write("  (텔레그램 발송함)")
        else:
            why = "--notify 없음" if not opts["notify"] else "dry-run이라 발송 안 함"
            self.stdout.write(f"  (텔레그램 미발송 — {why})")
        raise SystemExit(1)


def _f(v):
    """NaN·None·비수치 → None, 그 외 float."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN 체크


def _i(v):
    f = _f(v)
    return None if f is None else int(f)
