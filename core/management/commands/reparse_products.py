"""
저장된 모든 Product의 description을 최신 파서로 재파싱해 파생 필드를 갱신.

scrape_kofia는 매 실행 시 재파싱하지만, import_els(엑셀)로 들어온 상품은
새 파일만 처리하므로 파서 개선이 반영되지 않은 채 방치된다.
이 커맨드로 전체를 일괄 재계산한다. (원문 description 자체는 건드리지 않음)
"""

from django.core.management.base import BaseCommand

from core import parsers
from core.models import Product


class Command(BaseCommand):
    help = "모든 Product를 최신 파서로 재파싱 (KI/배리어/주기/자산유형 갱신)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="변경 건수만 집계하고 저장 안 함")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        changed = 0
        total = 0

        for p in Product.objects.all().iterator(chunk_size=500):
            total += 1
            desc = p.description or ""

            ki = parsers.extract_ki(desc)
            barriers = parsers.extract_barriers(desc)
            period = parsers.extract_period(desc, p.issue_date, p.expiry_date, barriers)
            asset_type = parsers.classify_asset(p.assets_raw) or ""

            is_no_ki = ki == "NoKI"
            ki_val = None if (ki is None or is_no_ki) else int(ki)

            new = dict(
                ki=ki_val,
                is_no_ki=is_no_ki,
                barrier_first=int(barriers[0]) if barriers else None,
                barrier_last=int(barriers[-1]) if barriers else None,
                barriers_raw=[int(b) for b in barriers] if barriers else None,
                period_months=period,
                asset_type=asset_type,
            )

            # 변경 여부 판단
            dirty = any(getattr(p, k) != v for k, v in new.items())
            if not dirty:
                continue
            changed += 1
            if not dry:
                for k, v in new.items():
                    setattr(p, k, v)
                p.save(update_fields=list(new.keys()))

        verb = "변경 예정" if dry else "갱신"
        self.stdout.write(f"[재파싱] 전체 {total}건 중 {changed}건 {verb}")
