"""이미 확정된 평가일의 **마지막 회차**가 실제 만기평가일과 며칠 어긋나는지 집계한다.

무엇이 문제인가
  SEIBro 공시는 조기상환 회차만 주고 만기평가일은 안 준다. 그래서
  backfill_eval_dates가 스케줄 마지막 칸에 만기일(= 상환 지급일)을 대신 넣어 왔다.
  실제 만기평가일은 그보다 2~6영업일 빠르다. 예: 신한 27859호는 설명서상
  만기평가일이 2029-08-07인데 저장된 값은 2029-08-10(만기일)이다.

  즉 지금 화면·알림이 쓰는 '마지막 평가일'은 판정일이 아니라 지급일이다.
  만기 판정을 며칠 늦게 인지하게 된다.

이 커맨드는 **읽기만 한다**
  고칠지 말지는 사람이 판단한다. 여기서는 몇 건이 며칠 어긋나고 그중 만기가
  임박한 건이 몇 건인지만 표로 낸다. 저장·수정은 하지 않는다.

  설명서 URL이 없는 상품(2026-07-18 이전 발행분)은 실제 만기평가일을 알 방법이
  없어 '확인 못 함'으로 따로 센다 — 어긋난 건 확실하지만 며칠인지는 모른다.

사용:
  python manage.py report_maturity_eval_gap --limit 30      (시범)
  python manage.py report_maturity_eval_gap                 (설명서 있는 확정분 전체)
  python manage.py report_maturity_eval_gap --within 90     (만기 임박 기준일수)
"""

import time
from collections import Counter, defaultdict
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from core.models import Product

from .parse_prospectus_dates import (
    SAMSUNG_FORMATS,
    extract_early_dates,
    extract_maturity_date,
)


class Command(BaseCommand):
    help = "확정 평가일의 마지막 회차가 실제 만기평가일과 며칠 다른지 집계(읽기 전용)"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="N건만(시범용, 0=전체)")
        parser.add_argument("--within", type=int, default=90,
                            help="만기 임박 기준 일수(기본 90일)")
        parser.add_argument("--delay", type=float, default=1.0, help="요청 간격(초)")
        parser.add_argument("--issuer", type=str, default="", help="특정 발행사만")

    def handle(self, *args, **opts):
        from .parse_prospectus_dates import Command as ParseCommand

        today = date.today()
        horizon = today + timedelta(days=opts["within"])

        qs = Product.objects.exclude(eval_dates__isnull=True)
        if opts["issuer"]:
            qs = qs.filter(issuer=opts["issuer"])
        confirmed = [p for p in qs.order_by("id") if p.fixed_eval_dates]
        with_pdf = [p for p in confirmed if p.prospectus_url]
        without_pdf = [p for p in confirmed if not p.prospectus_url]

        self.stdout.write(f"확정 평가일 보유 {len(confirmed)}건 "
                          f"(설명서 있음 {len(with_pdf)} / 없음 {len(without_pdf)})")
        self.stdout.write(f"설명서 없는 {len(without_pdf)}건은 실제 만기평가일을 "
                          f"확인할 방법이 없어 차이를 못 잰다")

        targets = with_pdf[: opts["limit"]] if opts["limit"] else with_pdf
        fetcher = ParseCommand()
        gaps = Counter()                 # 차이 일수 → 건수
        by_issuer = defaultdict(Counter)  # 발행사 → {차이: 건수}
        soon = []                        # 만기 임박 + 차이 있는 건
        same = unknown = 0
        rows = []

        for i, p in enumerate(targets, 1):
            try:
                text, tables = fetcher._fetch_text(p.prospectus_url)
            except Exception as e:
                unknown += 1
                self.stdout.write(f"  [{p.id}] {p.issuer} {p.product_no} 다운로드 실패: {e}")
                time.sleep(opts["delay"])
                continue
            early, fmt = extract_early_dates(text, tables)
            # 삼성증권은 만기평가일을 '최종기준가격'으로 적는다. 월수익형도 같은 표기라
            # 포맷 하나만 보면(예전 코드) 월수익형이 통째로 '확인 못 함'으로 빠졌다.
            maturity = extract_maturity_date(text, samsung=(fmt in SAMSUNG_FORMATS))
            if maturity is None:
                unknown += 1
                time.sleep(opts["delay"])
                continue
            stored = p.fixed_eval_dates[-1]
            gap = (stored - maturity).days
            if gap == 0:
                same += 1
            else:
                gaps[gap] += 1
                by_issuer[p.issuer][gap] += 1
                rows.append((p, stored, maturity, gap))
                if p.expiry_date and today <= p.expiry_date <= horizon:
                    soon.append((p, stored, maturity, gap))
            if i % 25 == 0 or i == len(targets):
                self.stdout.write(f"  진행 {i}/{len(targets)}")
            time.sleep(opts["delay"])

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"[대조 결과] 설명서 대조 {len(targets)}건 / 마지막 회차가 다른 건 {len(rows)}건 / "
            f"같은 건 {same}건 / 확인 못 함 {unknown}건"))

        self.stdout.write("\n차이 일수 분포 (저장값 − 설명서 만기평가일)")
        self.stdout.write("  일수  건수")
        for g, c in sorted(gaps.items()):
            self.stdout.write(f"  {g:+4d}일 {c:5d}건")

        self.stdout.write("\n발행사별 차이 분포")
        for issuer in sorted(by_issuer):
            detail = ", ".join(f"{g:+d}일 {c}건" for g, c in sorted(by_issuer[issuer].items()))
            self.stdout.write(f"  {issuer}: {detail}")

        self.stdout.write(f"\n만기가 {today}~{horizon}({opts['within']}일) 안에 오는 "
                          f"실질 영향 건: {len(soon)}건")
        for p, stored, maturity, gap in soon[:50]:
            self.stdout.write(f"  {p.issuer} {p.product_no} 만기 {p.expiry_date} / "
                              f"저장 {stored} → 실제 {maturity} ({gap:+d}일)")

        self.stdout.write("\n이 커맨드는 아무것도 저장하지 않았다. 적용 여부는 사람이 판단한다.")
