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
     폴백은 부분 문자열 매칭이라 후보가 여럿 나올 수 있다. 후보가 2개 이상이면
     아무것도 쓰지 않는다 — 오매칭보다 미채움이 안전하다. (2026-08-04)
  배리어 개수와 평가일 개수가 다르면 저장하지 않는다(부분 일치는 신뢰 불가).
  만기 검증은 반드시 상품(p) 기준으로 한다. 자세한 이유는 handle() 안 주석 참조.

사용:
  python manage.py backfill_eval_dates --dry-run   # 대상만 집계
  python manage.py backfill_eval_dates             # 미채움분 저장
  python manage.py backfill_eval_dates --force     # 이미 채운 것도 재매칭
"""

from datetime import timedelta

# 상품(KOFIA)과 SEIBro 행의 만기가 이만큼까지 다른 건 같은 상품으로 본다.
# 공시 출처가 달라 며칠 어긋나는 경우가 실제로 있다.
EXPIRY_TOLERANCE_DAYS = 3

from django.core.management.base import BaseCommand

from core.models import HistoricalIssue, Product


def find_match(p):
    """상품에 대응하는 SEIBro 이력 반환. 못 찾으면 None, 폴백 후보가 여럿이면 'ambiguous'."""
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
    cands = [h for h in qs
             if h.issue_date and h.eval_dates
             and abs((h.issue_date - anchor).days) <= 7]
    # 예전엔 첫 후보를 그냥 집었다. name__contains는 부분 문자열이라 "1827"이
    # "11827"에도 걸리고, Meta.ordering이 ["-issue_date"]라 늘 나중 발행분을 집었다.
    # 운영 실측(2026-08-04): 후보가 2개였던 5건 중 4건이 틀린 행을 골랐다.
    # 어느 쪽이 맞는지 가릴 근거가 없으므로 둘 다 버린다 — 오매칭보다 미채움이 안전하다.
    if len(cands) != 1:
        return "ambiguous" if cands else None
    return cands[0]


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
        ambiguous = expiry_mismatch = 0
        shifted = []
        for p in qs.iterator(chunk_size=500):
            h = find_match(p)
            if h == "ambiguous":
                ambiguous += 1
                continue
            if not h:
                no_match += 1
                continue
            # 상품과 SEIBro 행의 만기가 둘 다 있는데 크게 다르면 다른 상품이다.
            # 다만 며칠 차이는 오매칭이 아니라 공시 출처 차이다 — KOFIA와 SEIBro가
            # 같은 상품의 만기를 1~3일 다르게 적는 경우가 있다. 실측(2026-08-04)에서
            # 걸린 5건 중 4건이 상품명·발행일이 정확히 일치하는 올바른 매칭이었고,
            # 진짜 오매칭은 361일 어긋난 1건뿐이었다. 그래서 ±3일까지는 같은
            # 상품으로 본다. (조 팀장 판단)
            if (p.expiry_date and h.expiry_date
                    and abs((p.expiry_date - h.expiry_date).days) > EXPIRY_TOLERANCE_DAYS):
                expiry_mismatch += 1
                continue
            # SEIBro는 (평가일, 배리어)를 같은 순서로 준다. 리자드 상품은 같은
            # 날짜에 리자드 배리어 행이 하나 더 붙는다(예: 키움 1827의
            # 80-80-75(L50)-75-70-60 → 3회차 날짜에 배리어 50 행이 추가).
            # 이 행은 조기상환 회차가 아니므로 스케줄에서 제외한다 — 날짜만 보고
            # 중복 제거하면 리자드 정보와 회차 정렬이 함께 깨진다.
            raw_d = [str(d)[:10] for d in (h.eval_dates or [])]
            raw_b = list(h.stepdown_barriers or [])
            bars = [float(b) for b in (p.barriers_raw or [])]
            nb = len(bars)
            dates = []
            if raw_b and len(raw_b) == len(raw_d):
                prev_d = prev_b = None
                for d, b in zip(raw_d, raw_b):
                    # 같은 날짜인데 배리어가 더 낮으면 리자드 행
                    if d == prev_d and prev_b is not None and float(b) < float(prev_b):
                        continue
                    dates.append(d)
                    prev_d, prev_b = d, b
            else:
                dates = raw_d
            # SEIBro eval_dates는 조기상환 평가일만 담고 만기 평가는 빠져 있다.
            # 만기 기준은 반드시 상품(p)을 먼저 본다. 예전 `h.expiry_date or p.expiry_date`는
            # SEIBro 행을 그 행 자신의 만기와 대조하는 자기참조라, 폴백이 다른 상품을
            # 물어도 무조건 통과했다. 운영 실측(2026-08-04)에서 만기가 최대 361일까지
            # 어긋난 행이 붙어 있었다.
            exp = p.expiry_date or h.expiry_date
            if exp and (not dates or dates[-1] != str(exp)[:10]) and len(dates) == nb - 1:
                dates = dates + [str(exp)[:10]]
            # 회차 수가 배리어와 맞고 마지막이 만기여야 신뢰할 수 있다
            if len(dates) != nb or not exp or dates[-1] != str(exp)[:10]:
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
            f"회차수·만기 불일치 {skipped_count}건 / 만기 상충 {expiry_mismatch}건 / "
            f"폴백 후보 다수 {ambiguous}건 / SEIBro 미수집 {no_match}건")
        if shifted:
            self.stdout.write(f"\n보유 상품 1차 평가일 변경 {len(shifted)}건:")
            for p, old, new in shifted[:20]:
                self.stdout.write(
                    f"  {p.issuer} {p.product_no}: {old} → {new} "
                    f"({(new - old).days:+d}일)")
