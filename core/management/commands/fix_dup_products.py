"""
2026-07-16 시딩 배치가 만든 sub_end=NULL Product 369행을 정리한다.

원인: 엑셀 청약중인상품_20260616_0925.xlsx에 '청약종료일' 열이 아예 없었는데
import_els가 열 번호를 고정으로 읽어 그 자리(낙인조건)를 sub_end로 넣으려다 전부 None이 됐다.
SQLite는 유니크 비교에서 NULL을 서로 다르게 보므로 (issuer, product_no, sub_end)
제약을 통과했고, 같은 상품이 두 행으로 갈라졌다.

  · 274행 — 같은 상품의 정상 행(sub_end 있음)이 따로 있는 중복. 정상 행을 남기고 지운다.
  · 95행  — 짝이 없는 단독 행. 지우면 상품이 사라지므로 sub_end를 복구한다.

기본은 조회만 하는 리포트다. 실제로 쓰려면 --apply를 붙인다.
    python manage.py fix_dup_products                      # 리포트만
    python manage.py fix_dup_products --dedupe --apply     # 중복 274행 삭제
    python manage.py fix_dup_products --fill --apply       # 단독 95행 sub_end 복구
"""

from collections import Counter, defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Investment, NotifiedMatch, Product, RadarVerdict, WatchItem

# 중복 274쌍에서 관측한 발행사별 (issue_date − sub_end) 일수.
# 274쌍 전부에서 발행사마다 값이 하나로 떨어졌다(혼재 0건). 같은 파일에서 나온
# 단독 95행에도 그대로 쓴다. 런타임에 쌍이 남아 있으면 그쪽을 우선 사용하고,
# 이미 --dedupe로 지운 뒤라 쌍이 없으면 이 표로 폴백한다.
# 한화투자증권은 274쌍에 표본이 없어 비워 둔다 — 추정하지 않고 사람이 확인한다.
ISSUER_OFFSET_DAYS = {
    "한국투자증권": 0, "신한투자증권": 0, "KB증권": 0, "하나증권": 0, "메리츠증권": 0,
    "비엔케이투자증권": 0, "DB증권": 0, "교보증권": 0, "아이비케이투자증권": 0, "유안타증권": 0,
    "키움증권": 1, "삼성증권": 1, "미래에셋증권": 1, "NH투자증권": 1, "현대차증권": 1,
    "유진투자증권": 3,
}


class Command(BaseCommand):
    help = "sub_end=NULL Product 정리 (중복 삭제 · 단독행 sub_end 복구)"

    def add_arguments(self, parser):
        parser.add_argument("--dedupe", action="store_true", help="중복 행 삭제")
        parser.add_argument("--fill", action="store_true", help="단독 행 sub_end 복구")
        parser.add_argument("--apply", action="store_true", help="실제 DB에 반영 (없으면 조회만)")

    def handle(self, *args, **opts):
        do_dedupe, do_fill = opts["dedupe"], opts["fill"]
        if not (do_dedupe or do_fill):
            do_dedupe = do_fill = True   # 플래그 없으면 둘 다 리포트
        apply_ = opts["apply"]

        offsets = self._derive_offsets()
        pairs, solos = self._split()

        self.stdout.write(
            f"sub_end=NULL 총 {pairs.__len__() + len(solos)}행 "
            f"- 중복 {len(pairs)}행 / 단독 {len(solos)}행"
        )

        if do_dedupe:
            self._dedupe(pairs, apply_)
        if do_fill:
            self._fill(solos, offsets, apply_)

        if not apply_:
            self.stdout.write(self.style.WARNING("\n조회만 했습니다. 반영하려면 --apply를 붙이세요."))

    # ── 분류 ────────────────────────────────────
    def _split(self):
        """sub_end=NULL 행을 (중복쌍, 단독)으로 나눈다."""
        pairs, solos = [], []
        for p in Product.objects.filter(sub_end__isnull=True).order_by("id"):
            sibs = list(Product.objects.filter(
                issuer=p.issuer, product_no=p.product_no,
                issue_date=p.issue_date, sub_end__isnull=False,
            ))
            if len(sibs) == 1:
                pairs.append((p, sibs[0]))
            elif len(sibs) > 1:
                self.stderr.write(
                    f"  [건너뜀] P{p.id} {p.issuer} {p.product_no}: 정상 행이 {len(sibs)}개 - 수동 확인"
                )
            else:
                solos.append(p)
        return pairs, solos

    def _derive_offsets(self):
        """남아 있는 중복쌍에서 발행사별 (issue_date − sub_end)를 뽑는다.
        발행사 안에서 값이 갈리면 그 발행사는 버린다(추정하지 않는다)."""
        obs = defaultdict(Counter)
        for p in Product.objects.filter(sub_end__isnull=True):
            sib = Product.objects.filter(
                issuer=p.issuer, product_no=p.product_no,
                issue_date=p.issue_date, sub_end__isnull=False,
            ).first()
            if sib and p.issue_date and sib.sub_end:
                obs[p.issuer][(p.issue_date - sib.sub_end).days] += 1
        derived = {i: list(c)[0] for i, c in obs.items() if len(c) == 1}
        if derived:
            self.stdout.write(f"발행사 오프셋 {len(derived)}개를 현재 중복쌍에서 도출")
            return derived
        self.stdout.write("중복쌍이 없어 ISSUER_OFFSET_DAYS 상수표를 사용")
        return dict(ISSUER_OFFSET_DAYS)

    # ── 1단계: 중복 삭제 ────────────────────────
    def _dedupe(self, pairs, apply_):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n[1] 중복 {len(pairs)}행 정리"))
        if not pairs:
            return

        blocked, deletable = [], []
        for dup, keep in pairs:
            refs = {
                "Investment": Investment.objects.filter(product=dup).count(),
                "WatchItem": WatchItem.objects.filter(product=dup).count(),
                "NotifiedMatch": NotifiedMatch.objects.filter(product=dup).count(),
                "RadarVerdict": RadarVerdict.objects.filter(product=dup).count(),
            }
            if any(refs.values()):
                blocked.append((dup, keep, refs))
            else:
                deletable.append((dup, keep))

        self.stdout.write(f"  삭제 가능 {len(deletable)}행 / 참조가 붙어 보류 {len(blocked)}행")
        for dup, keep, refs in blocked:
            live = ", ".join(f"{k}={v}" for k, v in refs.items() if v)
            self.stdout.write(self.style.WARNING(
                f"  [보류] P{dup.id} {dup.issuer} {dup.product_no} - {live} "
                f"(남길 행 P{keep.id}로 옮긴 뒤 다시 실행)"
            ))
        for dup, keep in deletable[:5]:
            self.stdout.write(
                f"  삭제 P{dup.id} → 유지 P{keep.id} ({keep.issuer} {keep.product_no}, "
                f"sub_end={keep.sub_end})"
            )
        if len(deletable) > 5:
            self.stdout.write(f"  ... 외 {len(deletable) - 5}행")

        if apply_ and deletable:
            with transaction.atomic():
                ids = [d.id for d, _ in deletable]
                Product.objects.filter(id__in=ids).delete()
                self.stdout.write(self.style.SUCCESS(f"  삭제 완료: Product {len(ids)}행"))

    # ── 2단계: 단독 행 sub_end 복구 ─────────────
    def _fill(self, solos, offsets, apply_):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n[2] 단독 {len(solos)}행 sub_end 복구"))
        if not solos:
            return

        planned, unresolved, collided = [], [], []
        for p in solos:
            value, how = self._guess_sub_end(p, offsets)
            if value is None:
                unresolved.append((p, how))
                continue
            clash = Product.objects.filter(
                issuer=p.issuer, product_no=p.product_no, sub_end=value
            ).exclude(id=p.id).first()
            if clash:
                collided.append((p, value, clash))
            else:
                planned.append((p, value, how))

        by_how = Counter(h for _, _, h in planned)
        self.stdout.write(f"  복구 가능 {len(planned)}행  {dict(by_how)}")
        self.stdout.write(f"  근거 없음 {len(unresolved)}행 / 유니크 충돌 {len(collided)}행")

        for p, value, how in planned[:5]:
            self.stdout.write(f"  P{p.id} {p.issuer} {p.product_no}: sub_end=NULL → {value} ({how})")
        if len(planned) > 5:
            self.stdout.write(f"  ... 외 {len(planned) - 5}행")

        for p, value, clash in collided:
            self.stdout.write(self.style.WARNING(
                f"  [충돌] P{p.id} {p.issuer} {p.product_no} → {value} 가 P{clash.id}와 겹침 - 수동 확인"
            ))
        if unresolved:
            iss = Counter(p.issuer for p, _ in unresolved)
            self.stdout.write(self.style.WARNING(
                f"  [근거없음] {dict(iss)} - 발행사 오프셋 표본이 없습니다. "
                "KOFIA/증권사 공시에서 청약종료일을 확인해 직접 채워야 합니다."
            ))
            held = [p for p, _ in unresolved if Investment.objects.filter(product=p).exists()]
            if held:
                self.stdout.write(self.style.ERROR(
                    f"  그 중 보유 Investment가 붙은 행: {', '.join('P%d' % p.id for p in held)}"
                ))

        if apply_ and planned:
            with transaction.atomic():
                for p, value, _ in planned:
                    Product.objects.filter(id=p.id).update(sub_end=value)
                self.stdout.write(self.style.SUCCESS(f"  복구 완료: {len(planned)}행"))

    def _guess_sub_end(self, p, offsets):
        """(값, 근거). 확정할 수 없으면 (None, 사유)."""
        if not p.issue_date:
            return None, "issue_date 없음"

        # 1순위 — 같은 (발행사, 발행일) 상품들의 sub_end가 만장일치면 그 값.
        #          274쌍으로 자기제외 검증했을 때 만장일치 270건 전부 정답이었다.
        cohort = set(Product.objects.filter(
            issuer=p.issuer, issue_date=p.issue_date, sub_end__isnull=False,
        ).values_list("sub_end", flat=True))
        if len(cohort) == 1:
            return cohort.pop(), "코호트만장일치"

        # 2순위 — 발행사 오프셋
        off = offsets.get(p.issuer)
        if off is not None:
            return p.issue_date - timedelta(days=off), "발행사오프셋"

        return None, "오프셋표본없음"
