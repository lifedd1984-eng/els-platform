"""
KOFIA 전자공시에서 청약중인 ELS/DLS/ELB/DLB를 직접 수집해 Product로 저장.

ELS_Curator.exe 수동 실행 없이 자동 수집하는 경로.
기존 import_els(엑셀 업로드)와 병행 가능 — 같은 Product 테이블을 공유하며
product_code(KOFIA 고유코드)가 있으면 그것으로, 없으면 (issuer, product_no, sub_end)로 upsert.
"""

from django.core.management.base import BaseCommand

from core import kofia_scraper, notify, parsers, telegram
from core.models import ImportLog, Product


class Command(BaseCommand):
    help = "KOFIA 전자공시에서 청약중인 ELS/DLS/ELB/DLB 자동 수집"

    def add_arguments(self, parser):
        parser.add_argument("--no-notify", action="store_true", help="텔레그램 발송 생략")

    def _upsert_product(self, row, defaults) -> bool:
        """product_code(고유)와 (issuer,product_no,sub_end)(레거시 exe키) 둘 다로
        기존 행을 찾아본 뒤 있으면 갱신, 없으면 새로 만든다. True면 신규 생성."""
        existing = None
        if row["product_code"]:
            existing = Product.objects.filter(product_code=row["product_code"]).first()
        if existing is None:
            # 폴백은 exe 소스(product_code 빈값) 행에만 병합 —
            # 다른 KOFIA 상품(코드 있음)을 실수로 덮어쓰지 않도록 제한
            existing = Product.objects.filter(
                issuer=row["issuer"], product_no=row["product_no"], sub_end=row["sub_end"],
                product_code="",
            ).first()

        if existing:
            for k, v in defaults.items():
                setattr(existing, k, v)
            existing.product_code = row["product_code"]
            existing.save()
            return False

        Product.objects.create(product_code=row["product_code"], **defaults)
        return True

    def handle(self, *args, **opts):
        should_notify = not opts["no_notify"]

        try:
            rows = kofia_scraper.fetch_subscribing()
        except kofia_scraper.KofiaFetchError as e:
            self.stderr.write(f"[실패] {e}")
            if should_notify:
                telegram.send_message(f"[ELS 레이더] KOFIA 자동수집 실패\n{e}")
            return

        if not rows:
            self.stdout.write("수집된 데이터 없음 (KOFIA 응답 구조 변경 가능성 — 확인 필요)")
            if should_notify:
                telegram.send_message(
                    "[ELS 레이더] KOFIA 자동수집: 0건 수집됨\n"
                    "사이트 구조가 바뀌었을 수 있습니다. 확인이 필요합니다."
                )
            return

        n_new = 0
        missing_assets = []   # KOFIA가 기초자산을 빈 값으로 내려준 상품 (경보용)
        for row in rows:
            desc = row["description"]
            ki = parsers.extract_ki(desc)
            barriers = parsers.extract_barriers(desc)
            period = parsers.extract_period(desc, row["issue_date"], row["expiry_date"], barriers)

            # KOFIA 결손 대응: 기초자산이 빈 값이면 기존 DB의 수동 보정값을 보존
            # (매 배치 upsert가 보정을 빈 값으로 되돌리는 것 방지 — NH 25004 사례)
            if not (row["assets_raw"] or "").strip():
                prev = None
                if row["product_code"]:
                    prev = Product.objects.filter(product_code=row["product_code"]).first()
                if prev and (prev.assets_raw or "").strip():
                    row["assets_raw"] = prev.assets_raw
                else:
                    missing_assets.append(f"{row['issuer']} {row['product_no']} (~{row['sub_end']})")
            asset_type = parsers.classify_asset(row["assets_raw"]) or ""

            is_no_ki = ki == "NoKI"
            ki_val = None if (ki is None or is_no_ki) else int(ki)

            product_type = "ELS"
            if "ELB" in desc.upper() or "원금지급형" in desc:
                product_type = "ELB"

            currency = "USD" if ("USD" in desc.upper() or "달러" in desc) else "KRW"

            defaults = dict(
                issuer=row["issuer"],
                product_no=row["product_no"],
                name=row["name"],
                product_type=product_type,
                yield_rate=row["yield_rate"],
                max_loss=row["max_loss"],
                ki=ki_val,
                is_no_ki=is_no_ki,
                barrier_first=int(barriers[0]) if barriers else None,
                barrier_last=int(barriers[-1]) if barriers else None,
                barriers_raw=[int(b) for b in barriers] if barriers else None,
                period_months=period,
                asset_type=asset_type,
                assets_raw=row["assets_raw"],
                issue_date=row["issue_date"],
                expiry_date=row["expiry_date"],
                sub_start=row["sub_start"],
                sub_end=row["sub_end"],
                currency=currency,
                description=desc,
            )

            created = self._upsert_product(row, defaults)
            if created:
                n_new += 1

        self.stdout.write(f"[자동수집] KOFIA {len(rows)}건 중 신규 {n_new}건")

        # 기초자산 결손 경보 — 보정 전까지 매 배치 상기
        if missing_assets and should_notify:
            telegram.send_message(
                "[기초자산 누락] KOFIA가 기초자산을 빈 값으로 내려준 상품 "
                f"{len(missing_assets)}건\n"
                + "\n".join(f"- {m}" for m in missing_assets)
                + "\nKOFIA 웹에서 실물 확인 후 보정 필요 (유형·손실확률 계산 불가 상태)"
            )
            self.stdout.write(f"[기초자산 누락 경보] {len(missing_assets)}건 발송")
        ImportLog.objects.create(
            filename=f"kofia_auto_{timezone_today()}", row_count=len(rows), new_count=n_new
        )

        if should_notify and n_new:
            notify.notify_preset_matches(self.stdout)
        if should_notify:
            notify.notify_redemptions(self.stdout)

        if should_notify:
            from django.conf import settings
            telegram.send_message(
                f"[ELS 레이더] KOFIA 자동수집 완료\n"
                f"전체 {len(rows)}건 / 신규 {n_new}건\n"
                f"대시보드: {settings.SITE_URL}"
            )


def timezone_today():
    from django.utils import timezone
    return timezone.localtime().strftime("%Y%m%d_%H%M%S")
