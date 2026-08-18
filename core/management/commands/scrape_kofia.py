"""
KOFIA 전자공시에서 청약중인 ELS/DLS/ELB/DLB를 직접 수집해 Product로 저장.

ELS_Curator.exe 수동 실행 없이 자동 수집하는 경로.
기존 import_els(엑셀 업로드)와 병행 가능 — 같은 Product 테이블을 공유하며
product_code(KOFIA 고유코드)가 있으면 그것으로, 없으면 (issuer, product_no, sub_end)로 upsert.
"""

from django.core.management.base import BaseCommand

from core import kofia_scraper, notify, parsers, telegram
from core.models import ImportLog, Product

# 수집 완료 알림에 나열할 신규 상품 최대 건수. 넘으면 "... 외 N건"으로 접는다.
# 다른 알림(notify.notify_preset_matches·기초자산 누락 경보)과 같은 상한이다 —
# 신규가 수십 건 나오는 날 메시지가 텔레그램 상한(4096자)까지 늘어나는 것을 막는다.
NEW_LIST_LIMIT = 10


class Command(BaseCommand):
    help = "KOFIA 전자공시에서 청약중인 ELS/DLS/ELB/DLB 자동 수집"

    def add_arguments(self, parser):
        parser.add_argument("--no-notify", action="store_true", help="텔레그램 발송 생략")

    def _find_existing(self, row):
        """이 row가 갱신할 기존 Product를 찾는다. 없으면 None.

        product_code(고유)와 (issuer,product_no,sub_end)(레거시 exe키) 둘 다로 본다.
        upsert와 기초자산 보존이 **같은 행**을 가리켜야 하므로 한 곳으로 모았다.
        (예전엔 보존 쪽이 product_code로만 찾아, exe 소스(코드 빈값) 행에 손으로
         넣어 둔 기초자산이 다음 배치에서 빈 값으로 되돌아갈 수 있었다.)
        """
        if row["product_code"]:
            existing = Product.objects.filter(product_code=row["product_code"]).first()
            if existing:
                return existing
        # 폴백은 exe 소스(product_code 빈값) 행에만 병합 —
        # 다른 KOFIA 상품(코드 있음)을 실수로 덮어쓰지 않도록 제한
        return Product.objects.filter(
            issuer=row["issuer"], product_no=row["product_no"], sub_end=row["sub_end"],
            product_code="",
        ).first()

    def _upsert_product(self, row, defaults) -> bool:
        """기존 행이 있으면 갱신, 없으면 새로 만든다. True면 신규 생성."""
        existing = self._find_existing(row)

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
            # 청약 중인 상품이 실제로 0건인 기간이다 — 응답 구조가 깨진 경우는
            # fetch_subscribing이 KofiaFetchError로 올리므로 여기까지 오지 않는다.
            #
            # ▣ 0건이어도 ImportLog를 남긴다 (2026-08-18)
            #   예전엔 그냥 return이라 실행 기록이 안 남았다. 그래서 청약이 없는
            #   기간에는 배치가 매일 정상 실행되는데도 화면 신선도 배지가 마지막으로
            #   상품이 있던 날에 멈춰 "N일 전 데이터"로 굳었고, 방문자에게는
            #   수집이 멈춘 것과 구분되지 않았다. row_count=0이 그 구분자다
            #   (views._freshness의 'quiet' 상태).
            ImportLog.objects.create(
                filename=f"kofia_auto_{timezone_today()}", row_count=0, new_count=0
            )
            self.stdout.write("[자동수집] 청약 중인 상품 0건 (KOFIA 정상 응답)")
            if should_notify:
                telegram.send_message(
                    "[ELS 레이더] KOFIA 자동수집: 청약 중인 상품 0건\n"
                    "응답은 정상이며 청약 목록이 비어 있습니다. 수집 기록은 남겼습니다."
                )
            return

        n_new = 0
        new_rows = []         # 이번 배치에서 새로 만들어진 상품 (완료 알림 목록용)
        missing_assets = []   # KOFIA가 기초자산을 빈 값으로 내려준 상품 (경보용)
        odd_period = []       # 상품기간÷배리어단계로 역산한 주기와 어긋난 상품 (경보용)
        for row in rows:
            desc = row["description"]
            ki = parsers.extract_ki(desc)
            barriers = parsers.extract_barriers(desc)
            period = parsers.extract_period(desc, row["issue_date"], row["expiry_date"], barriers)

            # KOFIA 결손 대응: 기초자산이 빈 값이면 기존 DB의 수동 보정값을 보존
            # (매 배치 upsert가 보정을 빈 값으로 되돌리는 것 방지 — NH 25004 사례)
            #
            # 이 분기는 **KOFIA가 기초자산을 빈 값으로 준 상품에서만** 돈다.
            # 값이 정상인 상품은 여기 들어오지 않으므로 수집 결과가 달라지지 않는다.
            if not (row["assets_raw"] or "").strip():
                prev = self._find_existing(row)
                if prev and (prev.assets_raw or "").strip():
                    row["assets_raw"] = prev.assets_raw
                else:
                    missing_assets.append(row)
            asset_type = parsers.classify_asset(row["assets_raw"]) or ""

            # 조기상환주기 정합성 — 설명 원문에 주기가 명시된 경우에만 대조한다.
            # 기간÷배리어수 역산은 쓰지 않는다: 원문이 만기 회차를 생략하거나
            # 괄호로 묶어 배리어가 실제보다 적게 파싱되는 일이 흔해 오탐이 대부분이었다.
            # schedule(평가일)이 이 값으로 만들어져 알림·캘린더가 통째로 밀리므로
            # 원문과 어긋나는 건은 조용히 넘기지 않는다. (2026-07-31)
            stated = parsers.extract_period_text(desc)
            if period and stated and stated != period:
                odd_period.append(
                    f"{row['issuer']} {row['product_no']}: 저장 {period}개월 / 원문 {stated}개월"
                )

            is_no_ki = ki == "NoKI"
            ki_val = None if (ki is None or is_no_ki) else int(ki)

            # 상품유형은 **상품명까지** 봐야 한다. 예전엔 설명만 봤는데
            #   · 신한 ELB·DLB는 설명이 '원금**추가**지급형'이라 안 걸렸고
            #   · '스텝다운 (65-65-…) 월지급 하이파이브'처럼 설명에 단서가
            #     아예 없는 상품도 있어 전부 기본값 ELS로 저장됐다.
            # 판정은 parsers 한 곳에 있고 import_els·reparse_products도 같은 것을 쓴다.
            product_type = parsers.classify_product_type(row["name"], desc, ki_val)

            # 발행통화는 설명 원문이 **밝힌 경우에만** 정한다.
            #
            # 예전 한 줄은 못 읽으면 무조건 KRW로 단정했다. KOFIA 응답에는 통화
            # 필드가 없고(val 전수 확인) 설명이 통화를 말하지 않는 상품이 대부분이라,
            # 외화 상품이 그대로 원화로 굳었다 — 2026-08-07 실측 55건.
            # 게다가 이 값은 매 배치 upsert가 덮어써서, 사람이 fix_currency로
            # 바로잡아 놔도 청약중이면 다음 배치에 KRW로 되돌아갈 수 있었다.
            # 이제 못 읽으면 defaults에서 빼고, 통화는
            #   · 신규 → 모델 기본값 KRW
            #   · 기존 → 이미 들어 있는 값 보존 (assets_raw와 같은 원칙)
            # 확정은 발행 후 설명서·SEIBro 근거로 parse_prospectus_dates·fix_currency가 한다.
            currency = parsers.currency_from_description(desc)

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
                description=desc,
                broker_url=row.get("broker_url", ""),
                prospectus_url=row.get("prospectus_url", ""),
            )
            if currency:
                defaults["currency"] = currency

            created = self._upsert_product(row, defaults)
            if created:
                n_new += 1
                # row 자체는 안 건드린다 — 위 upsert가 이미 끝났어도 아래에서
                # 쓰는 건 완료 알림용 사본 하나뿐이다.
                new_rows.append({**row, "ki_val": ki_val, "is_no_ki": is_no_ki})

        self.stdout.write(f"[자동수집] KOFIA {len(rows)}건 중 신규 {n_new}건")

        # 기초자산 결손 경보 — 보정 전까지 매 배치 상기
        #
        # ⚠ 예전 문구는 "KOFIA 웹에서 실물 확인"이었는데 이는 헛걸음이다.
        #   빈 값의 출처가 KOFIA 목록(val8) 자체라 KOFIA 웹 화면에서도 그 칸이 비어 있다.
        #   실제 답은 같은 응답에 딸려 오는 **간이투자설명서**의 '상품개요' 표에 있다.
        #   (2026-08-06 실측: NH투자증권 25034·25065 — 설명서엔 'KOSDAQ150 지수' 명시)
        if missing_assets and should_notify:
            lines = [
                f"[기초자산 누락] KOFIA가 기초자산을 빈 값으로 내려준 상품 "
                f"{len(missing_assets)}건",
            ]
            for r in missing_assets[:10]:
                lines.append(f"- {r['issuer']} {r['product_no']} (~{r['sub_end']})")
                if r.get("prospectus_url"):
                    lines.append(f"  설명서: {r['prospectus_url']}")
            if len(missing_assets) > 10:
                lines.append(f"... 외 {len(missing_assets) - 10}건")
            lines.append(
                "KOFIA 목록 자체가 빈 값이라 웹 화면에도 안 나옵니다. "
                "간이투자설명서 '상품개요'로 확인 후 보정하세요 "
                "(유형·손실확률 계산 불가 상태).\n"
                "  python manage.py fix_missing_assets            # 근거·후보 확인\n"
                "  python manage.py fix_missing_assets --id <id> "
                "--assets \"...\" --apply"
            )
            telegram.send_message("\n".join(lines))
            self.stdout.write(f"[기초자산 누락 경보] {len(missing_assets)}건 발송")

        # 조기상환주기 이상 경보 — 평가일 계산의 뿌리라 즉시 확인 필요
        if odd_period and should_notify:
            lines = [f"[주기 이상] 조기상환주기가 상품기간과 맞지 않는 상품 {len(odd_period)}건"]
            lines += [f"- {m}" for m in odd_period[:10]]
            if len(odd_period) > 10:
                lines.append(f"... 외 {len(odd_period)-10}건")
            lines.append("설명서 확인 후 period_months 보정 필요 "
                         "(평가일·알림 날짜가 어긋납니다)")
            telegram.send_message("\n".join(lines))
            self.stdout.write(f"[주기 이상 경보] {len(odd_period)}건 발송")

        ImportLog.objects.create(
            filename=f"kofia_auto_{timezone_today()}", row_count=len(rows), new_count=n_new
        )

        if should_notify and n_new:
            notify.notify_preset_matches(self.stdout)
        # 상환 평가일 D-1 알림은 send_redemption_alert(아침 크론)로 분리 —
        # 새벽 배치에서 보내면 웹 푸시가 한밤중에 울린다.

        if should_notify:
            from django.conf import settings
            # 건수만 오던 알림에 신규 상품 목록(발행사·상품번호)을 싣는다.
            # 2026-08-11 조 팀장 지시 — 몇 건인지만 보고 무엇이 들어왔는지 알 수 없었다.
            lines = [
                "[ELS 레이더] KOFIA 자동수집 완료",
                f"전체 {len(rows)}건 / 신규 {n_new}건",
            ]
            for r in new_rows[:NEW_LIST_LIMIT]:
                ki_txt = "없음" if r["is_no_ki"] else (
                    f"{r['ki_val']}%" if r["ki_val"] is not None else "-")
                yld_txt = f"{r['yield_rate']:g}%" if r["yield_rate"] is not None else "-"
                lines.append(
                    f"- {r['issuer']} {r['product_no']} 낙인{ki_txt} 쿠폰{yld_txt}")
            if len(new_rows) > NEW_LIST_LIMIT:
                lines.append(f"... 외 {len(new_rows) - NEW_LIST_LIMIT}건")
            lines.append(f"대시보드: {settings.SITE_URL}")
            telegram.send_message("\n".join(lines))


def timezone_today():
    from django.utils import timezone
    return timezone.localtime().strftime("%Y%m%d_%H%M%S")
