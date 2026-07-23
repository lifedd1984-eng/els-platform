"""
ELS 수익률 모의실험(롤링 백테스트).

과거 기초자산 종가로, 매 영업일을 발행일로 가정하고 만기까지 전개하여
회차별 조기상환/만기손실 분포를 집계한다.

payoff 규칙 (사용자 확정):
  - 낙인(KI): 매일 종가 상시관찰. 발행일~만기 중 워스트오브 종가가 KI배리어 이하로
    한 번이라도 내려가면 낙인 발생. (조기상환된 표본은 KI 무관 — 이미 쿠폰 확정)
  - 조기상환: n회차 평가일에 워스트오브 레벨 >= barriers[n-1] 이면 상환.
    수익률(누적, 세전) = 연수익률 × (period_months × n / 12)
  - 만기: 어느 회차에서도 상환 안 됨 →
      · 낙인 미터치(또는 NoKI) → 최대 쿠폰(만기까지 누적) 지급, 손실 아님
      · 낙인 터치 → 손실 확정, 손실률 = 만기 워스트오브 레벨 - 100 (음수)

핵심: `simulate(prices, ...)`는 순수 함수(시세 DataFrame 주입) → 단위 검증 가능.
      `simulate_product(product)`가 yfinance 연동 래퍼.
"""

from datetime import date

import numpy as np

# 손실 분포 밴드 폭(%)
LOSS_BAND = 4
# 시뮬 신뢰를 위한 최소 표본 수
MIN_SAMPLES = 200


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = d.day
    while True:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1


def simulate(prices, barriers, ki, is_no_ki, period_months, yield_rate,
             sample_years=None):
    """
    Parameters
    ----------
    prices : pandas.DataFrame  (index=정렬된 날짜, columns=기초자산, 결측 없음, 공통구간)
    barriers : list[int]       회차별 조기상환 배리어 (예: [95,90,85,80,75,70])
    ki : int | None            낙인 배리어(%). is_no_ki=True면 무시.
    is_no_ki : bool
    period_months : int        조기상환 평가 주기(개월)
    yield_rate : float          연수익률(%)
    sample_years : int | None  가상 발행일 표본 구간(년). 마지막 유효 발행일
                               (데이터 끝 − 상품기간)에서 거슬러 이 기간만 표본으로.
                               None이면 데이터 전 구간 사용.

    Returns
    -------
    dict (available, samples, period_start/end, rounds, loss_buckets, loss_prob_pct, ...)
    """
    if not barriers or not period_months or yield_rate is None:
        return {"available": False, "reason": "상품 조건(배리어/주기/수익률) 부족"}
    if prices is None or len(prices.columns) == 0 or len(prices) < 60:
        return {"available": False, "reason": "기초자산 시세 데이터 부족"}

    n_rounds = len(barriers)
    total_months = period_months * n_rounds

    dates = list(prices.index.date) if hasattr(prices.index, "date") else list(prices.index)
    ordinals = np.array([d.toordinal() for d in dates])
    arr = prices.values.astype(float)  # (T, A)
    T = len(arr)

    # 표본 시작 하한: (마지막 유효 발행일 − sample_years년)
    # 마지막 유효 발행일 = 만기(total_months)가 데이터 끝 안에 들어오는 마지막 날짜.
    start_i = 0
    if sample_years:
        last_end = dates[-1]
        last_valid_start = _add_months(last_end, -total_months)
        min_start = _add_months(last_valid_start, -sample_years * 12)
        start_i = int(np.searchsorted(ordinals, min_start.toordinal(), side="left"))

    # 각 회차 평가일까지 필요한 개월수
    eval_months = [period_months * n for n in range(1, n_rounds + 1)]

    round_counts = [0] * n_rounds       # 회차별 조기상환 건수
    maturity_noki = 0                    # 만기 무손실(최대쿠폰)
    losses = []                          # 만기 손실률 리스트(음수 %)
    samples = 0
    first_start = None
    last_start = None

    for i in range(start_i, T):
        start_d = dates[i]
        # 만기 평가일이 데이터 범위 안에 있어야 유효 표본
        maturity_target = _add_months(start_d, total_months).toordinal()
        if maturity_target > ordinals[-1]:
            break  # 이후 시작일은 모두 만기 초과 (정렬돼 있으므로 종료)

        ref = arr[i]  # (A,)
        if np.any(ref <= 0):
            continue

        # 회차별 평가일 인덱스
        eval_idx = []
        ok = True
        for em in eval_months:
            target = _add_months(start_d, em).toordinal()
            j = int(np.searchsorted(ordinals, target, side="left"))
            if j >= T:
                ok = False
                break
            eval_idx.append(j)
        if not ok:
            continue

        samples += 1
        if first_start is None:
            first_start = start_d
        last_start = start_d

        # 조기상환 판정
        redeemed_round = None
        for n in range(n_rounds):
            j = eval_idx[n]
            worst_level = float(np.min(arr[j] / ref)) * 100.0
            if worst_level >= barriers[n]:
                redeemed_round = n
                break

        if redeemed_round is not None:
            round_counts[redeemed_round] += 1
            continue

        # 만기까지 미상환 → 낙인 판정
        mat_j = eval_idx[-1]
        if is_no_ki or ki is None:
            ki_touched = False
        else:
            window = arr[i:mat_j + 1] / ref            # (w, A)
            worst_series = np.min(window, axis=1) * 100.0
            ki_touched = float(np.min(worst_series)) <= ki

        if not ki_touched:
            maturity_noki += 1
        else:
            worst_maturity = float(np.min(arr[mat_j] / ref)) * 100.0
            losses.append(worst_maturity - 100.0)

    if samples == 0:
        return {"available": False, "reason": "만기까지 전개 가능한 표본 없음"}

    # ── 집계 ──
    rounds = []
    for n in range(n_rounds):
        ret = round(yield_rate * (period_months * (n + 1)) / 12, 2)
        rounds.append({
            "round": n + 1,
            "return_pct": ret,
            "count": round_counts[n],
            "freq_pct": round(round_counts[n] / samples * 100, 3),
        })

    # 손실 분포 밴드 (0 ~ 최저 손실, LOSS_BAND% 폭)
    loss_buckets = []
    if losses:
        worst = min(losses)
        # band high 값들: 0, -4, -8, ... worst 이하까지
        band_high = 0
        while band_high > worst - LOSS_BAND:
            low = band_high - LOSS_BAND
            cnt = sum(1 for r in losses if low < r <= band_high)
            if cnt > 0:
                loss_buckets.append({
                    "label": f"{low}% ~ {band_high}%",
                    "count": cnt,
                    "freq_pct": round(cnt / samples * 100, 3),
                })
            band_high -= LOSS_BAND

    loss_count = len(losses)
    early_count = sum(round_counts)
    # 투자 후 1년(12개월) 이내에 상환되는 회차만 집계 — 헤드라인 지표용
    early_1y = sum(round_counts[n] for n in range(n_rounds)
                   if period_months * (n + 1) <= 12)

    return {
        "available": True,
        "samples": samples,
        "period_start": first_start,
        "period_end": last_start,
        "rounds": rounds,
        "maturity_noki_count": maturity_noki,
        "maturity_noki_pct": round(maturity_noki / samples * 100, 3),
        "loss_buckets": loss_buckets,
        "loss_count": loss_count,
        "loss_prob_pct": round(loss_count / samples * 100, 2),
        "early_redemp_pct": round(early_count / samples * 100, 2),
        "early_1y_pct": round(early_1y / samples * 100, 2),
        "avg_loss_pct": round(sum(losses) / loss_count, 2) if losses else None,
        "low_confidence": samples < MIN_SAMPLES,
    }


# ──────────────────────────────────────────────
# yfinance 연동 래퍼
# ──────────────────────────────────────────────
def simulate_product(product, period_years=20):
    """Product의 기초자산을 티커로 해석 → yfinance 과거 종가 → simulate() 실행."""
    from core import market

    assets = market.split_assets(product.assets_raw)
    if not assets:
        return {"available": False, "reason": "기초자산 정보 없음"}

    tickers = {}
    for a in assets:
        tk = market.resolve_ticker(a)
        if not tk:
            return {"available": False, "reason": f"시세 없음: {a}"}
        tickers[a] = tk

    # +4년 여유 조회 — 표본 구간(period_years)은 마지막 유효 발행일 기준
    prices = _fetch_price_frame(list(tickers.values()), period_years + 4)
    if prices is None or prices.empty:
        return {"available": False, "reason": "시세 조회 실패"}

    return simulate(
        prices,
        barriers=product.barriers_raw,
        ki=product.ki,
        is_no_ki=product.is_no_ki,
        period_months=product.period_months,
        yield_rate=product.yield_rate,
        sample_years=period_years,
    )


def _fetch_price_frame(tickers, period_years):
    """여러 티커의 종가를 공통 구간으로 정렬한 DataFrame 반환."""
    import pandas as pd
    import yfinance as yf

    series = {}
    for tk in tickers:
        try:
            h = yf.Ticker(tk).history(period=f"{period_years}y")
            s = h["Close"].dropna()
            if len(s):
                s.index = s.index.tz_localize(None)
                series[tk] = s
        except Exception:
            return None
    if not series:
        return None
    df = pd.DataFrame(series).dropna()  # 공통 구간만
    return df
