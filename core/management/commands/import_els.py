"""
downloads 폴더의 청약중인상품_*.xlsx를 감지해 Product로 임포트.

- ALL 시트만 파싱 (다른 시트는 부분집합)
- ImportLog로 동일 파일 재처리 방지
- 프리셋 매칭 신규 상품 텔레그램 알림 (NotifiedMatch로 중복 방지)
- 보유 Investment 평가일 D-7/D-1 알림 (RedemptionAlert로 중복 방지)
- 월~수 새 파일 없으면 목요일에 리마인더
"""

import glob
import os
from datetime import date, timedelta

import openpyxl
from django.conf import settings
from django.core.management.base import BaseCommand

from core import notify, parsers, telegram
from core.models import ImportLog, Product


def _to_date(val):
    """엑셀의 20260323 형식 int/str → date."""
    if val is None:
        return None
    s = str(val).strip().replace(".", "").replace("-", "")
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def _to_float(val):
    try:
        return float(str(val).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


class Command(BaseCommand):
    help = "ELS 엑셀 임포트 + 프리셋 매칭 알림 + 상환 평가일 알림"

    def add_arguments(self, parser):
        parser.add_argument("--file", help="특정 파일만 임포트 (경로)")
        parser.add_argument("--no-notify", action="store_true", help="텔레그램 발송 생략")

    def handle(self, *args, **opts):
        should_notify = not opts["no_notify"]

        if opts.get("file"):
            files = [opts["file"]]
        else:
            pattern = os.path.join(settings.ELS_DOWNLOADS_DIR, "청약중인상품_*.xlsx")
            # _수정/_서식테스트 등 가공본은 제외 (원본만 임포트)
            files = sorted(
                f for f in glob.glob(pattern)
                if "_수정" not in os.path.basename(f)
                and "테스트" not in os.path.basename(f)
            )

        processed = ImportLog.objects.values_list("filename", flat=True)
        new_files = [f for f in files if os.path.basename(f) not in processed]

        total_new_products = 0
        for path in new_files:
            n_rows, n_new = self._import_file(path)
            total_new_products += n_new
            self.stdout.write(f"[임포트] {os.path.basename(path)}: {n_rows}행 중 신규 {n_new}건")

        if not new_files:
            self.stdout.write("새 파일 없음")
            self._maybe_remind(should_notify)

        # 프리셋 매칭 알림
        if should_notify and total_new_products:
            notify.notify_preset_matches(self.stdout)

        # 상환 평가일 알림
        if should_notify:
            notify.notify_redemptions(self.stdout)

        if new_files and should_notify:
            telegram.send_message(
                f"[ELS 플랫폼] 임포트 완료\n"
                f"파일 {len(new_files)}개 / 신규 상품 {total_new_products}건\n"
                f"대시보드: {settings.SITE_URL}"
            )

    # ── 파일 임포트 ─────────────────────────────
    def _import_file(self, path):
        wb = openpyxl.load_workbook(path, data_only=True)
        if "ALL" not in wb.sheetnames:
            self.stderr.write(f"ALL 시트 없음: {path}")
            return 0, 0
        ws = wb["ALL"]

        n_rows = n_new = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            # 열: (빈), 발행회사, 신용등급, 상품명, 기초자산, 발행일, 만기일,
            #      연수익률, 최대손실률, 청약시작일, 청약마감일, 상품유형(설명)
            issuer = str(row[1] or "").strip()
            if not issuer:
                continue
            n_rows += 1

            desc = str(row[11] or "")
            ki = parsers.extract_ki(desc)
            barriers = parsers.extract_barriers(desc)
            period = parsers.extract_period(desc, row[5], row[6], barriers)
            asset_type = parsers.classify_asset(str(row[4] or "")) or ""

            is_no_ki = ki == "NoKI"
            ki_val = None if (ki is None or is_no_ki) else int(ki)

            product_type = "ELS"
            if "ELB" in desc.upper() or "원금지급형" in desc:
                product_type = "ELB"

            currency = "USD" if ("USD" in desc.upper() or "달러" in desc) else "KRW"

            _, created = Product.objects.update_or_create(
                issuer=issuer,
                product_no=str(row[3] or "").strip(),
                sub_end=_to_date(row[10]),
                defaults=dict(
                    name=str(row[3] or "").strip(),
                    product_type=product_type,
                    yield_rate=_to_float(row[7]),
                    max_loss=_to_float(row[8]),
                    ki=ki_val,
                    is_no_ki=is_no_ki,
                    barrier_first=int(barriers[0]) if barriers else None,
                    barrier_last=int(barriers[-1]) if barriers else None,
                    barriers_raw=[int(b) for b in barriers] if barriers else None,
                    period_months=period,
                    asset_type=asset_type,
                    assets_raw=str(row[4] or "").strip(),
                    issue_date=_to_date(row[5]),
                    expiry_date=_to_date(row[6]),
                    sub_start=_to_date(row[9]),
                    currency=currency,
                    description=desc,
                ),
            )
            if created:
                n_new += 1

        ImportLog.objects.create(
            filename=os.path.basename(path), row_count=n_rows, new_count=n_new
        )
        return n_rows, n_new

    # ── 목요일 리마인더 ──────────────────────────
    def _maybe_remind(self, should_notify):
        today = date.today()
        if today.weekday() != 3:  # 목요일
            return
        monday = today - timedelta(days=3)
        recent = ImportLog.objects.filter(imported_at__date__gte=monday).exists()
        if not recent and should_notify:
            telegram.send_message(
                "[리마인더] 이번 주 ELS 데이터가 아직 없습니다.\n"
                "ELS_Curator를 실행하거나 자동수집(scrape_kofia)을 확인해주세요."
            )
