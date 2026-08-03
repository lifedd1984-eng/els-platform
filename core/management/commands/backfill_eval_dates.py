"""SEIBro 발행이력(HistoricalIssue)의 실제 조기상환 평가일을 Product.eval_dates에 채운다.

왜 필요한가
  Investment.schedule은 '기준일 + N개월'로 평가일을 계산해 왔는데, 실제 평가일은
  설명서에 개별 지정되는 값이라 공식으로 맞출 수 없다. 보유 153종을 SEIBro 실측과
  대조한 결과 정확히 일치한 것은 8종뿐이고 나머지는 -7~+3일 어긋났다.
  실제로 키움 1863은 계산 8/4 vs 실제 7/30이라 조기상환을 5일 늦게 인지했다.

매칭
  ① ISIN(product_code == HistoricalIssue.isin) — 정확. KOFIA 수집분에 있다.
  ② 발행사 + 상품번호 + 발행일 ±7일 — 엑셀 수입분(product_code 없음)용 폴백.
     같은 번호가 여러 차수 존재하므로 발행일 근접을 반드시 함께 본다.
  배리어 개수와 평가일 개수가 다르면 저장하지 않는다(부분 일치는 신뢰 불가).

사용:
  python manage.py backfill_eval_dates --dry-run   # 대상만 집계
  python manage.py backfill_eval_dates             # 미채움분 저장
  python manage.py backfill_eval_dates --force     # 이미 채운 것도 재매칭
"""

from datetime import timedelta

from django.core.management.base import BaseCommand

from core.models import HistoricalIssue, Product


def find_match(p):
    """상품에 대응하는 SEIBro 이력 반환. 못 찾으면 None."""
    if p.product_code:
        h = HistoricalIssue.objects.filter(isin=p.product_code).first()
        if h and h.eval_dates:
            return h
    if not (p.product_no and p.issuer):
        return None
    anchor = p.issued_on
    if not anchor:
        return None
    qs = HistoricalIssue.objects.filter(
        issuer__contains=p.issuer[:2], name__contains=str(p.product_no))
    for h in qs:
        if (h.issue_date and h.eval_dates
                and abs((h.issue_date - anchor).days) <= 7):
            return h
    return None


class Command(BaseCommand):
    help = "SEIBro 실제 조기상환 평가일을 Product.eval_dates에 백필"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true",
                            help="이미 채워진 상품도 재매칭")
        parser.add_argument("--held-only", action="store_true",
                            help="보유 중인 상품만 (급한 건 먼저)")

    def handle(self, *args, **opts):
        qs = Product.objects.exclude(barriers_raw=None)
        if opts["held_only"]:
            from core.models import Investment
            ids = Investment.objects.filter(status="보유중").values_list(
                "product_id", flat=True)
            qs = qs.filter(id__in=ids)
        if not opts["force"]:
            qs = qs.filter(eval_dates__isnull=True)

        total = qs.count()
        saved = skipped_count = no_match = 0
        shifted = []
        for p in qs.iterator(chunk_size=500):
            h = find_match(p)
            if not h:
                no_match += 1
                continue
            dates = [str(d)[:10] for d in (h.eval_dates or [])]
            nb = len(p.barriers_raw or [])
            # SEIBro eval_dates는 '조기상환' 평가일만 담고 만기 평가는 뺀다.
            # (마지막 평가일 + 한 주기 = 만기일임을 보유 136종 전수로 확인)
            if len(dates) == nb - 1:
                exp = h.expiry_date or p.expiry_date
                if exp:
                    dates = dates + [str(exp)[:10]]
            if len(dates) != nb:
                skipped_count += 1
                continue
            # 기존 근사 대비 얼마나 이동하는지 기록 (첫 회차 기준)
            old = None
            inv = p.investments.filter(status="보유중").first()
            if inv:
                sched = inv.schedule
                old = sched[0]["date"] if sched else None
            if not opts["dry_run"]:
                p.eval_dates = dates
                p.save(update_fields=["eval_dates"])
            saved += 1
            if old and dates:
                from datetime import date as _d
                new = _d.fromisoformat(dates[0])
                if new != old:
                    shifted.append((p, old, new))

        verb = "대상" if opts["dry_run"] else "저장"
        self.stdout.write(
            f"[평가일 백필] 후보 {total}건 → {verb} {saved}건 / "
            f"회차수 불일치 {skipped_count}건 / SEIBro 미수집 {no_match}건")
        if shifted:
            self.stdout.write(f"\n보유 상품 1차 평가일 변경 {len(shifted)}건:")
            for p, old, new in shifted[:20]:
                self.stdout.write(
                    f"  {p.issuer} {p.product_no}: {old} → {new} "
                    f"({(new - old).days:+d}일)")
