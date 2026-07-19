"""포트폴리오 데이터 정합성 보수 (FIX_SPEC B + D).

B. 그룹B 자동복구: 원본 설명이 손상된 상품에 온전한 설명을 채워 재파싱 가능하게 함.
D. 중복 Product 행 정리: 배리어 없는 '껍데기' 행에 연결된 투자를 배리어 있는
   '정상 행'으로 relink 하고, 그 결과 아무도 안 쓰는 빈 중복 행만 안전 삭제.

멱등(idempotent): 여러 번 실행해도 결과 동일. --dry-run 지원.
※ reparse_products는 이 커맨드 이후 별도로 실행해 파생필드를 재계산할 것.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Investment, Product

# B. 엑셀 원본에서 확인된 온전한 설명 (FIX_SPEC B 명시값)
GROUP_B_DESC = {
    ("미래에셋증권", "37858"): "조기상환형, 75-75-75-75-70-70, KI25, 3년만기 6개월 평가, 쿠폰 연32.6%",
    ("삼성증권", "30924"): "[스텝다운] 3년/3개월,45KI(90,90,90,90,90,90,85,85,85,80,80,75)%,세전 연 22%",
    ("삼성증권", "30994"): "[월지급식] 3년/3개월,25KI(85,85,85,85,80,80,80,80,75,75,75,70)%,월수익행사율 65%",
}


class Command(BaseCommand):
    help = "포트폴리오 데이터 보수 (그룹B 설명 복구 + 중복행 투자 relink/정리)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="변경 없이 계획만 출력")

    def _has_barriers(self, p):
        return bool(p.barriers_raw) and len(p.barriers_raw) > 0

    @transaction.atomic
    def handle(self, *args, **opts):
        dry = opts["dry_run"]

        # ── B. 그룹B 설명 복구 ──────────────────────────
        # 투자가 연결된 행(있으면)을 우선, 없으면 배리어 없는 행에 채운다.
        b_filled = 0
        for (issuer, no), desc in GROUP_B_DESC.items():
            rows = list(Product.objects.filter(issuer=issuer, product_no=no))
            if not rows:
                self.stdout.write(f"[B] {issuer} {no}: 상품 없음 — 건너뜀")
                continue
            # 이미 정상 행(배리어 존재)이 있으면 B 불필요 (D가 relink로 처리)
            if any(self._has_barriers(p) for p in rows):
                self.stdout.write(f"[B] {issuer} {no}: 정상 행 존재 — 설명복구 불필요(D가 처리)")
                continue
            # 투자 연결된 행 우선, 없으면 첫 행
            target = None
            for p in rows:
                if Investment.objects.filter(product=p).exists():
                    target = p
                    break
            if target is None:
                target = rows[0]
            if (target.description or "").strip() == desc:
                self.stdout.write(f"[B] {issuer} {no}: 설명 이미 최신 — 건너뜀")
                continue
            self.stdout.write(f"[B] {issuer} {no}: pid={target.id} 설명 채움")
            if not dry:
                target.description = desc
                target.save(update_fields=["description"])
            b_filled += 1

        # ── D. 중복행 relink + 정리 ─────────────────────
        groups = defaultdict(list)
        for p in Product.objects.all():
            groups[(p.issuer, p.product_no)].append(p)

        relinked = 0
        deleted = 0
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            normal_rows = [p for p in rows if self._has_barriers(p)]
            if not normal_rows:
                continue  # 안전장치: 정상 행 없으면 건드리지 않음
            # 정상 행 중 최적: 배리어 있음 > 최신 sub_end > 최대 id
            best = max(
                normal_rows,
                key=lambda p: (p.sub_end or __import__("datetime").date.min, p.id),
            )
            touched_sources = []
            for p in rows:
                if p.id == best.id or self._has_barriers(p):
                    continue  # 정상 행(자기 자신 포함)은 relink 대상 아님
                invs = list(Investment.objects.filter(product=p))
                if not invs:
                    continue
                for inv in invs:
                    self.stdout.write(
                        f"[D] relink inv={inv.id} {key[0]} {key[1]}: pid {p.id}(빈) → {best.id}(정상)"
                    )
                    if not dry:
                        inv.product = best
                        inv.save(update_fields=["product"])
                    relinked += 1
                touched_sources.append(p)
            # relink로 비워진 껍데기 행만 안전 삭제 (배리어 없음 + 이제 투자 0건)
            for p in touched_sources:
                if not dry and Investment.objects.filter(product=p).exists():
                    continue
                self.stdout.write(f"[D] delete empty dup pid={p.id} {key[0]} {key[1]}")
                if not dry:
                    p.delete()
                deleted += 1

        verb = "(dry-run) " if dry else ""
        self.stdout.write(
            f"{verb}완료: B설명복구 {b_filled}건 / relink {relinked}건 / 빈행삭제 {deleted}건"
        )
