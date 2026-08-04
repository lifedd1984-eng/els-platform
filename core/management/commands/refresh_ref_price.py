"""보유 투자의 최초기준가(KnockInStatus.ref_price)를 확정 규칙으로 재계산한다.

기준가 결정 순서 (실체는 core.market에 있다)
  ① SEIBro 공시 최초기준가격(HistoricalIssue.assets[].std_price) — 발행사 신고 공식값
  ② 없거나 못 믿을 값이면 → 폴백 = market.fallback_ref_basis()가 정한 기준일의 종가

값이 바뀌는 원인은 둘뿐이라 **원인별로 나눠서** 집계한다.
  · 공시채택 — 폴백으로 추론하던 값을 공시 공식값으로 교체
  · 폴백규칙 — 공시값이 없어 폴백을 쓰는데, 폴백 기준일 규칙 자체가 바뀌어 값이 달라짐
              (거래일 오프셋 폐지 → 기준일을 날짜 하나로 확정)
원인이 섞이면 무엇 때문에 레벨이 움직였는지 판단할 수 없다.

레벨은 저장된 현재가를 그대로 쓰고 기준가만 갈아끼워 재계산한다
  (레벨 = 현재가 / 기준가 × 100). 현재가는 이 커맨드가 건드리지 않는다.

기본은 --dry-run(아무것도 저장 안 함). 실제 저장은 --apply를 줘야만 한다.
--no-fetch를 주면 외부 시세를 한 번도 부르지 않고 공시채택 효과만 본다
  (폴백은 저장된 기존 값을 그대로 폴백 결과로 간주).
"""

from django.core.management.base import BaseCommand, CommandError

from core import market
from core.management.commands.update_prices import BANDS
from core.models import Investment, KnockInStatus, Product


CAUSE_DISCLOSED = "공시채택"
CAUSE_FALLBACK = "폴백규칙"


def _band(buffer):
    """낙인 경보 구간 라벨. 해당 없으면 None. (update_prices와 같은 기준을 공유)"""
    if buffer is None:
        return None
    for name, threshold in BANDS:
        if buffer <= threshold:
            return name
    return None


class Command(BaseCommand):
    help = "보유 투자 기준가를 SEIBro 공시값 우선 + 확정 폴백규칙으로 재계산 (기본 dry-run)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="실제로 저장한다. 주지 않으면 dry-run(기본)으로 출력만 한다.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="기본값. 명시적으로 적어도 되며 --apply가 없으면 항상 dry-run이다.")
        parser.add_argument(
            "--no-fetch", action="store_true",
            help="외부 시세를 부르지 않는다. 폴백은 저장된 기존 기준가를 그대로 쓴다.")
        parser.add_argument(
            "--top", type=int, default=25, help="변경 상위 몇 건을 보여줄지 (기본 25)")

    def handle(self, *args, **opts):
        if opts["apply"] and opts["dry_run"]:
            raise CommandError("--dry-run 과 --apply 는 함께 줄 수 없다")
        apply_ = opts["apply"]
        fetch = not opts["no_fetch"]

        holdings = (Investment.objects.filter(status="보유중")
                    .select_related("product").prefetch_related("ki_status"))

        total = adopted = kept = 0
        skip_reasons = {}
        changes = []          # 값이 바뀌는 자산 행
        to_save = []
        band_flips, ki_flips = [], []
        price_cache = {}      # (ticker, 기준일) → 폴백 종가
        no_ticker = 0

        for inv in holdings:
            p = inv.product
            disclosed = market.disclosed_ref_prices(p)          # ①
            base_date, back = market.fallback_ref_basis(p)      # ②
            if not base_date:
                base_date, back = inv.invested_at, 0
            old_levels, new_levels = [], []

            for st in inv.ki_status.all():
                total += 1
                cand, reason = disclosed.get(st.asset_name, (None, market.REF_SKIP_UNMATCHED))

                # 폴백 — 새 규칙의 기준일 종가. --no-fetch면 저장된 기존값으로 대체.
                fallback = st.ref_price
                if fetch and base_date:
                    ticker = st.ticker or market.resolve_ticker(st.asset_name)
                    if not ticker:
                        no_ticker += 1
                    else:
                        key = (ticker, base_date, back)
                        if key not in price_cache:
                            price_cache[key] = market.fetch_price_on(
                                ticker, base_date, back=back)
                        fallback = price_cache[key] or st.ref_price

                new_ref, source = market.pick_ref_price(cand, fallback)
                if source == "공시":
                    adopted += 1
                else:
                    kept += 1
                    if cand and fallback:      # 공시값이 있었는데 시세와 동떨어져 기각됨
                        reason = market.REF_SKIP_NORMALIZED
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

                new_level = None
                if st.current_price and new_ref:
                    new_level = round(st.current_price / new_ref * 100, 1)
                old_levels.append(st.level_pct)
                new_levels.append(new_level)

                changed = (new_ref is not None and st.ref_price is not None
                           and abs(new_ref - st.ref_price) / st.ref_price > 5e-5)
                if changed:
                    changes.append({
                        "cause": CAUSE_DISCLOSED if source == "공시" else CAUSE_FALLBACK,
                        "issuer": p.issuer, "product_no": p.product_no,
                        "asset": st.asset_name, "base_date": base_date,
                        "old_ref": st.ref_price, "new_ref": new_ref,
                        "delta_pct": (new_ref - st.ref_price) / st.ref_price * 100,
                        "old_level": st.level_pct, "new_level": new_level, "ki": p.ki,
                    })
                if changed or new_level != st.level_pct:
                    st.ref_price = new_ref
                    st.level_pct = new_level
                    to_save.append(st)

            # 투자 단위 — 워스트오브 기준 판정이 뒤집히는지
            ki = p.ki
            ov = [v for v in old_levels if v is not None]
            nv = [v for v in new_levels if v is not None]
            if ki is None or not ov or not nv:
                continue
            ow, nw = min(ov), min(nv)
            ob, nb = _band(round(ow - ki, 1)), _band(round(nw - ki, 1))
            if ob != nb:
                band_flips.append((p, ow, nw, ki, ob, nb))
            if (ow <= ki) != (nw <= ki):
                ki_flips.append((p, ow, nw, ki))

        self._report(total, adopted, kept, skip_reasons, changes, band_flips, ki_flips,
                     opts["top"], apply_, fetch, no_ticker)

        if not apply_:
            self.stdout.write("\n※ dry-run — 아무것도 저장하지 않았다. 저장하려면 --apply")
            return
        KnockInStatus.objects.bulk_update(to_save, ["ref_price", "level_pct"])
        self.stdout.write(self.style.SUCCESS(f"\n저장 완료 — {len(to_save)}행 갱신"))

    # ── 출력 ────────────────────────────────────────────────────────
    def _report(self, total, adopted, kept, skip_reasons, changes, band_flips, ki_flips,
                top_n, apply_, fetch, no_ticker):
        mode = "APPLY" if apply_ else "DRY-RUN"
        self.stdout.write(f"[{mode}] 보유 자산 기준가 재계산"
                          f"{'' if fetch else ' (--no-fetch: 공시채택 효과만)'}")
        self.stdout.write("=" * 78)
        self.stdout.write(f"총 대상 자산      {total}")
        self.stdout.write(f"공시값 채택       {adopted}")
        self.stdout.write(f"폴백 유지         {kept}")
        for reason, n in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            self.stdout.write(f"    {reason:<10} {n}")
        if no_ticker:
            self.stdout.write(f"    (티커 미해결로 폴백 시세를 못 구한 자산 {no_ticker})")

        by_cause = {}
        for c in changes:
            by_cause.setdefault(c["cause"], []).append(c)
        self.stdout.write(f"\n기준가가 바뀌는 자산 {len(changes)} — 원인별")
        for cause in (CAUSE_DISCLOSED, CAUSE_FALLBACK):
            rows = by_cause.get(cause, [])
            moved = [c for c in rows if c["old_level"] is not None
                     and c["new_level"] is not None
                     and abs(c["new_level"] - c["old_level"]) >= 0.05]
            up = sum(1 for c in moved if c["new_level"] > c["old_level"])
            self.stdout.write(
                f"    {cause}  기준가 {len(rows)}건 / 레벨 변화 {len(moved)}건"
                f" (상승 {up} · 하락 {len(moved) - up})")
            if moved:
                mx = max(moved, key=lambda c: abs(c["new_level"] - c["old_level"]))
                self.stdout.write(
                    f"        최대 레벨 변화 {mx['new_level'] - mx['old_level']:+.1f}%p"
                    f" ({mx['issuer']} {mx['product_no']} {mx['asset']})")

        if changes:
            self.stdout.write(f"\n── 변경 상위 {min(top_n, len(changes))}건 "
                              f"(기준가 변화율 절대값 순) ──")
            self.stdout.write(
                f"{'원인':<6}{'발행사':<8}{'상품':>7} {'자산':<16}{'기준일':>11}"
                f"{'기존기준가':>12}{'→새기준가':>12}{'변화율':>9}"
                f"{'기존레벨':>8}{'→새레벨':>8}{'레벨변화':>9}{'KI여유변화':>11}")
            for c in sorted(changes, key=lambda x: -abs(x["delta_pct"]))[:top_n]:
                ol, nl = c["old_level"], c["new_level"]
                lv_old = f"{ol:.1f}" if ol is not None else "-"
                lv_new = f"{nl:.1f}" if nl is not None else "-"
                lv_d = f"{nl - ol:+.1f}%p" if (ol is not None and nl is not None) else "-"
                # 낙인 여유(레벨−KI)의 변화량은 KI가 안 변하므로 레벨 변화량과 같다
                ki_d = lv_d if c["ki"] is not None else "KI없음"
                self.stdout.write(
                    f"{c['cause']:<6}{c['issuer']:<8}{c['product_no']:>7} {c['asset']:<16}"
                    f"{str(c['base_date']):>11}{c['old_ref']:>12,.2f}{c['new_ref']:>12,.2f}"
                    f"{c['delta_pct']:>+8.2f}%{lv_old:>8}{lv_new:>8}{lv_d:>9}{ki_d:>11}")

        self.stdout.write(f"\n── 낙인 경보 판정이 뒤집히는 투자: {len(band_flips)}건 ──")
        for p, ow, nw, ki, ob, nb in band_flips:
            self.stdout.write(
                f"  {p.issuer} {p.product_no}  워스트 레벨 {ow:.1f}% → {nw:.1f}% "
                f"(KI {ki}%, 여유 {ow - ki:+.1f}%p → {nw - ki:+.1f}%p)  "
                f"{ob or '정상'} → {nb or '정상'}")

        self.stdout.write(f"\n── 낙인 돌파(레벨 ≤ KI) 판정이 뒤집히는 투자: {len(ki_flips)}건 ──")
        for p, ow, nw, ki in ki_flips:
            self.stdout.write(
                f"  {p.issuer} {p.product_no}  워스트 레벨 {ow:.1f}% → {nw:.1f}% (KI {ki}%)  "
                f"{'돌파' if ow <= ki else '미돌파'} → {'돌파' if nw <= ki else '미돌파'}")

        # 규칙화 불가 발행사 — 기준가를 None으로 둘지 여부는 조 팀장 판단 사항
        unstable = market.BASE_EVAL_UNSTABLE_ISSUERS
        n_all = Product.objects.filter(issuer__in=unstable).count()
        n_held = (Investment.objects.filter(status="보유중", product__issuer__in=unstable)
                  .count())
        self.stdout.write(
            f"\n── 규칙화 불가 발행사({'/'.join(sorted(unstable))}) — 옛 로직 유지 ──")
        self.stdout.write(f"  전체 상품 {n_all}건 / 보유중 투자 {n_held}건")
