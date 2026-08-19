"""확정 평가일의 **마지막 한 칸**을 만기일(지급일)에서 만기평가일로 바로잡는다.

무엇이 틀렸나
  SEIBro 공시는 조기상환 회차만 주고 만기평가일은 안 준다. 그래서
  backfill_eval_dates가 스케줄 마지막 칸을 expiry_date(만기일 = 상환 지급일)로
  대신 채워 왔다. 실제 만기평가일은 그보다 2~12일 빠르다(발행사별 상환 영업일 규칙).
  2026-08-13 실측: 설명서로 대조 가능한 457건 **전부** 어긋났고 일치는 0건이었다.
  즉 지금 화면·알림이 '마지막 평가일'로 쓰는 값은 판정일이 아니라 지급일이다.

무엇을 고치나 — 마지막 칸 하나뿐이다
  조기상환 회차는 **절대 건드리지 않는다**. 그건 SEIBro 확정값이고 설명서 전수
  대조에서 완벽히 일치한 값이다. 저장값은 항상 `기존[:-1] + [설명서 만기평가일]`로
  만든다. 회차 수가 구조적으로 안 바뀌므로 Product.fixed_eval_dates도 그대로 참이다.

무엇을 근거로 바꾸나
  파싱은 parse_prospectus_dates.extract_schedule을 **그대로** 쓴다. 새로 구현하지
  않는다. 그 함수가 이미 회차수·오름차순·기준일·만기간격 검사를 전부 들고 있다.
  만기평가일이 여러 날 나열되면 가장 늦은 날을 쓴다(미래에셋 제37977호 원문
  '만기평가일이 복수인 경우 그 중 마지막 날', 후보 2개 이상 설명서 75건 전수 검증).

  그 위에 이 커맨드만의 가드를 하나 더 얹는다 — **설명서에서 읽은 조기상환 회차가
  저장된 앞 회차와 글자 그대로 같아야 한다.** 다르면 다른 차수의 설명서를 읽었거나
  둘 중 하나가 틀린 것이므로 손대지 않는다. 같은 상품번호가 차수마다 재사용되는
  발행사가 있어(메리츠 등) 이 대조가 유일하게 확실한 신원 확인이다.

설명서가 없는 상품은 대상이 아니다
  2026-07-18 KOFIA 수집 이전 발행분 1,866건은 설명서 URL이 없어 실제 만기평가일을
  확인할 방법이 없다. 만기일에서 영업일을 역산하면 채울 수는 있지만 그건 추정이고,
  eval_dates는 '확정' 자리다. 확정 자리에 추정을 넣으면 fixed_eval_dates가 거짓말을
  하게 되므로 그대로 둔다.

읽기 전용이 기본이다
  아무 옵션 없이 실행하면 무엇이 바뀔지 표로만 낸다. 실제 저장은 --apply를 붙일
  때만 한다. 확정 데이터를 고치는 작업이라 반대로 두지 않았다.

사용:
  python manage.py fix_maturity_eval_date --limit 20            (시범, 저장 안 함)
  python manage.py fix_maturity_eval_date                       (전체 dry-run)
  python manage.py fix_maturity_eval_date --cache-dir <경로>    (설명서 텍스트 캐시)
  python manage.py fix_maturity_eval_date --apply               (실제 저장)
"""

import json
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand

from core.models import Investment, Product

from .parse_prospectus_dates import Command as ParseCommand
from .parse_prospectus_dates import extract_schedule


class Command(BaseCommand):
    help = "확정 평가일의 마지막 회차를 만기일에서 설명서상 만기평가일로 교체(기본은 dry-run)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="실제로 저장한다. 없으면 무엇이 바뀔지 보여주기만 한다")
        parser.add_argument("--limit", type=int, default=0, help="N건만(시범용, 0=전체)")
        parser.add_argument("--issuer", type=str, default="", help="특정 발행사만")
        parser.add_argument("--within", type=int, default=90,
                            help="만기 임박 표시 기준 일수(기본 90일)")
        parser.add_argument("--show", type=int, default=10, help="샘플로 출력할 건수")
        parser.add_argument("--delay", type=float, default=1.0,
                            help="요청 간격(초) — KOFIA 부담 최소화")
        parser.add_argument("--cache-dir", type=str, default="",
                            help="설명서 텍스트 캐시 경로. dry-run과 --apply가 같은 "
                                 "PDF를 두 번 내려받지 않게 한다")

    # ------------------------------------------------------------------ 대상
    def _targets(self, opts):
        """확정 평가일 + 설명서 URL이 있고, 마지막 칸이 아직 만기일인 상품."""
        qs = Product.objects.exclude(eval_dates__isnull=True).exclude(prospectus_url="")
        if opts["issuer"]:
            qs = qs.filter(issuer=opts["issuer"])
        out = []
        for p in qs.order_by("id"):
            fixed = p.fixed_eval_dates
            if not fixed:
                continue
            # 마지막 칸이 만기일과 같은 건만 고친다. 이미 설명서로 채워진 건
            # (마지막 칸 < 만기일)은 손댈 이유가 없다.
            if p.expiry_date and fixed[-1] == p.expiry_date:
                out.append(p)
        return out[: opts["limit"]] if opts["limit"] else out

    # ------------------------------------------------------------------ 수집
    def _text(self, p, fetcher, cache):
        """설명서 텍스트·표. 캐시가 있으면 내려받지 않는다."""
        if cache:
            txt = cache / f"{p.id}.txt"
            tbl = cache / f"{p.id}.tables.json"
            if txt.exists() and tbl.exists():
                return (txt.read_text(encoding="utf-8"),
                        json.loads(tbl.read_text(encoding="utf-8")), True)
        text, tables = fetcher._fetch_text(p.prospectus_url)
        if cache:
            cache.mkdir(parents=True, exist_ok=True)
            (cache / f"{p.id}.txt").write_text(text, encoding="utf-8")
            (cache / f"{p.id}.tables.json").write_text(
                json.dumps(tables, ensure_ascii=False), encoding="utf-8")
        return text, tables, False

    # ------------------------------------------------------------------ 판정
    def _verdict(self, p, text, tables):
        """이 상품을 어떻게 할지 결정한다. 반환 (판정, 새 목록 또는 None, 사유).

        판정
          교체   — 설명서 조기상환 회차가 저장값과 같고 만기평가일도 정상
          동일   — 이미 만기평가일이 들어 있다(고칠 게 없다)
          상충   — 설명서 조기상환 회차가 저장값과 다르다. 손대지 않는다
          미저장 — 설명서에서 읽지 못했다. 손대지 않는다
        """
        stored = p.fixed_eval_dates
        dates, why = extract_schedule(p, text, tables)
        if dates is None:
            return "미저장", None, why
        # 조기상환 회차가 한 날이라도 다르면 이 설명서가 이 상품의 것이라고
        # 단정할 수 없다. 마지막 칸만 바꾸는 작업이라도 근거가 없으면 안 한다.
        if [d.isoformat() for d in dates[:-1]] != [d.isoformat() for d in stored[:-1]]:
            diff = [i + 1 for i, (a, b) in enumerate(zip(stored, dates)) if a != b]
            return "상충", None, f"조기상환 회차 불일치(회차 {diff})"
        new = stored[:-1] + [dates[-1]]
        if new == stored:
            return "동일", None, why
        # 회차 수는 구조적으로 안 바뀌지만, 바뀌면 fixed_eval_dates가 거짓이 되므로
        # 저장 직전에 한 번 더 못 박는다.
        if len(new) != len(stored) or len(new) != len(p.barriers_raw or []):
            return "미저장", None, "회차 수가 바뀐다"
        return "교체", new, why

    # ------------------------------------------------------------------ 실행
    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        cache = Path(opts["cache_dir"]) if opts["cache_dir"] else None
        today = date.today()
        horizon = today + timedelta(days=opts["within"])

        targets = self._targets(opts)
        self.stdout.write(
            f"대상: {len(targets)}건 (확정 평가일 + 설명서 URL + 마지막 칸이 아직 만기일)")
        self.stdout.write("모드: " + ("실제 저장(--apply)" if apply_ else "dry-run — 저장하지 않음"))
        if not targets:
            return

        fetcher = ParseCommand()
        gaps = Counter()                  # 차이 일수 → 건수
        by_issuer = defaultdict(Counter)  # 발행사 → {차이: 건수}
        reasons = Counter()               # 미저장·상충 사유 → 건수
        changes = []                      # (product, 이전, 이후, 차이)
        same = conflict = skipped = failed = 0

        for i, p in enumerate(targets, 1):
            try:
                text, tables, cached = self._text(p, fetcher, cache)
            except Exception as e:
                failed += 1
                reasons["다운로드/PDF오류"] += 1
                self.stdout.write(f"  [{p.id}] {p.issuer} {p.product_no} 다운로드 실패: {e}")
                time.sleep(opts["delay"])
                continue

            verdict, new, why = self._verdict(p, text, tables)
            if verdict == "동일":
                same += 1
            elif verdict == "상충":
                conflict += 1
                reasons[why] += 1
                self.stdout.write(f"  [{p.id}] {p.issuer} {p.product_no} {why} — 손대지 않음")
            elif verdict == "미저장":
                skipped += 1
                reasons[why] += 1
            else:
                old, now = p.fixed_eval_dates[-1], new[-1]
                gap = (old - now).days
                gaps[gap] += 1
                by_issuer[p.issuer][gap] += 1
                changes.append((p, old, now, gap))
                if apply_:
                    p.eval_dates = [d.isoformat() for d in new]
                    p.save(update_fields=["eval_dates"])
                    # 회차 수가 유지돼 여전히 확정으로 읽히는지 저장 직후 확인한다
                    p.refresh_from_db(fields=["eval_dates"])
                    if not p.fixed_eval_dates:
                        raise RuntimeError(
                            f"[{p.id}] 저장 뒤 확정 평가일이 깨졌다 — 즉시 중단")

            if i % 25 == 0 or i == len(targets):
                self.stdout.write(f"  진행 {i}/{len(targets)} (교체 {len(changes)})")
            if not cached:
                time.sleep(opts["delay"])

        self._report(changes, gaps, by_issuer, reasons, same, conflict, skipped,
                     failed, today, horizon, opts, apply_)

    # ------------------------------------------------------------------ 보고
    def _report(self, changes, gaps, by_issuer, reasons, same, conflict, skipped,
                failed, today, horizon, opts, apply_):
        verb = "교체함" if apply_ else "교체 예정"
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"[마지막 회차] {verb} {len(changes)}건 / 이미 만기평가일 {same}건 / "
            f"조기상환 회차 상충 {conflict}건 / 설명서에서 못 읽음 {skipped}건 / "
            f"다운로드 실패 {failed}건"))

        if gaps:
            self.stdout.write("\n차이 일수 분포 (기존 저장값 − 설명서 만기평가일)")
            self.stdout.write("  일수  건수")
            for g, c in sorted(gaps.items()):
                self.stdout.write(f"  {g:+4d}일 {c:5d}건")

        if by_issuer:
            self.stdout.write("\n발행사별 차이 분포")
            for issuer in sorted(by_issuer):
                detail = ", ".join(f"{g:+d}일 {c}건"
                                   for g, c in sorted(by_issuer[issuer].items()))
                self.stdout.write(f"  {issuer}: {detail}")

        if reasons:
            self.stdout.write("\n손대지 않은 사유")
            for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
                self.stdout.write(f"  {r}: {c}건")

        n = opts["show"]
        if changes:
            self.stdout.write(f"\n샘플 {min(n, len(changes))}건 (마지막 회차 이전 → 이후)")
            self.stdout.write(f"  {'발행사':<12} {'상품번호':>8} {'회차':>4} "
                              f"{'이전':>10} {'이후':>10} {'차이':>5}  만기일")
            for p, old, now, gap in changes[:n]:
                self.stdout.write(
                    f"  {p.issuer:<12} {p.product_no:>8} {len(p.fixed_eval_dates):>4} "
                    f"{old!s:>10} {now!s:>10} {gap:+4d}일  {p.expiry_date}")

        soon = [c for c in changes
                if c[0].expiry_date and today <= c[0].expiry_date <= horizon]
        self.stdout.write(f"\n만기가 {today}~{horizon}({opts['within']}일) 안인 건: {len(soon)}건")
        for p, old, now, gap in soon[:20]:
            self.stdout.write(f"  {p.issuer} {p.product_no} 만기 {p.expiry_date} / "
                              f"{old} → {now} ({gap:+d}일)")

        # 보유중 투자에서 '다음 평가일'이 실제로 바뀌는 건만 센다. 마지막 회차가
        # 아직 다음 차례가 아니면 알림·화면에 오늘 당장 보이는 변화는 없다.
        ids = {p.id for p, _o, _n, _g in changes}
        if ids:
            moved = []
            for inv in Investment.objects.filter(
                    status="보유중", product_id__in=ids).select_related("product"):
                nxt = inv.next_evaluation
                if nxt and nxt["n"] == len(inv.product.fixed_eval_dates or []):
                    moved.append(inv)
            self.stdout.write(
                f"\n보유중 투자 {Investment.objects.filter(status='보유중', product_id__in=ids).count()}건이 "
                f"이 상품들을 들고 있고, 그중 다음 평가일이 마지막 회차인 건 {len(moved)}건")
            for inv in moved[:20]:
                self.stdout.write(f"  {inv.product.issuer} {inv.product.product_no} "
                                  f"다음 평가일이 바뀐다")

        if not apply_:
            self.stdout.write("\n아무것도 저장하지 않았다. 실제 적용은 --apply를 붙여 실행한다.")
