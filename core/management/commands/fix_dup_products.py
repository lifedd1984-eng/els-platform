"""
2026-07-16 시딩 배치가 만든 sub_end=NULL Product 369행을 정리한다.

원인: 엑셀 청약중인상품_20260616_0925.xlsx에 '청약종료일' 열이 아예 없었는데
import_els가 열 번호를 고정으로 읽어 그 자리(낙인조건)를 sub_end로 넣으려다 전부 None이 됐다.
SQLite는 유니크 비교에서 NULL을 서로 다르게 보므로 (issuer, product_no, sub_end)
제약을 통과했고, 같은 상품이 두 행으로 갈라졌다.

  · 274행 — 같은 상품의 정상 행(sub_end 있음)이 따로 있는 중복. 정상 행을 남기고 지운다.
  · 95행  — 짝이 없는 단독 행. 지우면 상품이 사라지므로 sub_end를 복구한다.

⚠ 지우려는 쪽이 남기려는 쪽보다 데이터가 풍부하다. 274쌍 전량 대조 결과,
  남길 행이 비었는데 지울 행에 값이 있는 필드가 eval_dates 127 · 배리어 73 ·
  시뮬 73 · sub_start 21 · ki 18건 있었고, 그 반대 방향은 **0건**이었다
  (남길 행은 지울 행의 부분집합). 그냥 지우면 영구 손실이다 — 이 상품들은
  청약이 끝나 다시 수집되지 않는다. 그래서 삭제 전에 병합 단계를 둔다.

세 단계이고 순서가 있다. 기본은 조회만 하는 리포트다. 반영하려면 --apply를 붙인다.

    python manage.py fix_dup_products                    # 3단계 전부 리포트만
    python manage.py fix_dup_products --merge  --apply   # ① 남길 행의 빈 필드를 채움
    python manage.py fix_dup_products --dedupe --apply   # ② 중복 274행 삭제
    python manage.py fix_dup_products --fill   --apply   # ③ 단독 95행 sub_end 복구
    python manage.py fix_dup_products --merge --dedupe --fill --apply   # 한 번에

②는 ①이 끝난 쌍만 지운다. 아직 옮기지 않은 데이터가 남은 쌍은 자동으로 보류되므로
--merge를 건너뛰고 --dedupe만 돌려도 데이터가 사라지지 않는다.

①이 description을 채우거나 바꾼 뒤에는 파생 필드(배리어·낙인·상품유형·주기)를
다시 계산해야 한다. 이 커맨드는 원문만 옮기고 파싱은 하지 않는다.

    python manage.py reparse_products     # ① 다음에 반드시
"""

from collections import Counter, defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from core import parsers
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

# ── 병합 대상 필드 ────────────────────────────────────────────────
#   id          — 기본키
#   sub_end     — 병합의 전제. 남길 행의 값이 정답이고 지울 행은 NULL이다
#   collected_at— auto_now_add. 수집 시각이라 옮길 의미가 없다
# 나머지는 전부 대상이다. 목록을 모델에서 뽑으므로 필드가 늘어도 자동으로 따라간다.
MERGE_SKIP = {"id", "sub_end", "collected_at"}
MERGE_FIELDS = [f.name for f in Product._meta.concrete_fields if f.name not in MERGE_SKIP]

# ── description 파편 판정 ─────────────────────────────────────────
# 남길 행(엑셀 수입분)의 description이 상품 설명이 아니라 엉뚱한 열 값인 경우가 있다.
# import_els는 헤더 '상품유형' 열을 설명으로 읽는데, ELS_Curator 엑셀은 주차마다
# 열 구성이 달라 같은 자리에 낙인조건('KI25%')·노낙인 표기('NoKI')·여부 표시('X')가
# 번갈아 들어왔다(import_els COLUMNS 주석 참고). 그 값이 그대로 description이 됐다.
# 지울 행(KOFIA 수집분)에는 배리어·낙인·쿠폰이 다 들어 있는 전문이 있다.
# 운영 274쌍 실측: 설명이 서로 다른 65건 전부가 이 꼴이었다
#   'X' 19 · 'KI30%' 11 · 'KI35%' 10 · 'KI25%' 9 · 'NoKI' 7 · 'KI40%' 6 · 그 외 3
# 그리고 'KI<n>%' 39건은 전부 전문에서 파싱한 낙인과 숫자가 같았다(불일치 0건).
DESC_FRAGMENT_MAX_LEN = 8   # 파편으로 인정할 최대 길이. 실측 최장 5자('KI25%')

# reparse_products가 description·assets_raw에서 다시 계산하는 필드.
# 이 필드들은 충돌로 지울 행 값을 버려도 손실이 아니다 — 병합으로 옮긴 설명 전문을
# 재파싱하면 같은 값이 다시 나온다. 실제로 운영 사본에서 충돌로 남았던
# is_no_ki 9건·product_type 3건이 재파싱 후 전부 지울 행 값과 같아졌다.
# (재파싱 단독으로는 4,495건 중 0건 변경 — 바뀐 건 순전히 병합이 준 설명 덕분이다.)
REPARSE_DERIVED = {
    "product_type", "ki", "is_no_ki", "barrier_first", "barrier_last",
    "barriers_raw", "period_months", "first_eval_months", "schedule_estimated",
    "asset_type",
}


def _is_empty(v):
    """'값이 없다'의 정의. 0·False·0.0은 값이 있는 것이다 (덮어쓰면 안 된다)."""
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _desc_replaceable(keep_desc, dup_desc):
    """남길 행 description을 지울 행 전문으로 바꿔도 되는가 → (가부, 사유).

    바꿔도 되는 건 남길 값이 '파편'일 때뿐이다. 파편이란 그 문자열이 담은 정보가
    전문에 통째로 들어 있어, 버려도 잃을 게 없는 값을 말한다. 판정은 세 갈래다.

      ① 남길 값이 전문의 부분문자열      → 전문이 상위집합이므로 순이득
      ② 남길 값이 아주 짧고(≤8자) 낙인 정보가 전문과 일치 → 파편
      ③ 그 외                            → 바꾸지 않고 충돌로 보고

    ②에서 낙인을 대조하는 이유: 'KI25%'인데 전문이 KI30이면 두 행이 같은 상품이
    아니거나 어느 한쪽이 오염된 것이다. 그때는 사람이 봐야 한다.
    낙인 정보가 아예 없는 파편('X')은 대조할 게 없지만, 한 글자짜리 여부 표시라
    상품 설명일 수가 없다 — 길이 컷으로 거른다.
    """
    keep = (keep_desc or "").strip()
    dup = (dup_desc or "").strip()
    if not dup:
        return False, "지울 행 설명 없음"
    if not keep:
        return True, "남길 행 비어 있음"
    if keep == dup:
        return False, "같은 값"
    if len(keep) >= len(dup):
        return False, "남길 값이 더 길다"
    if keep in dup:
        return True, "부분문자열"
    if len(keep) > DESC_FRAGMENT_MAX_LEN:
        return False, "파편으로 보기엔 길다"
    keep_ki = parsers.extract_ki(keep)
    dup_ki = parsers.extract_ki(dup)
    if keep_ki is not None and keep_ki != dup_ki:
        return False, f"파편 낙인 {keep_ki} ≠ 전문 낙인 {dup_ki}"
    return True, "파편"


def _merge_plan(dup, keep):
    """이 쌍에서 남길 행에 옮길 것 → ({필드: 값}, [(필드, 사유), ...] 충돌).

    기본 규칙은 fill-empty-only다 — 남길 행이 비어 있을 때만 채운다.
    둘 다 값이 있으면 남길 행을 유지하고 충돌로 보고한다.
    description만 예외로, 남길 값이 파편이면 전문으로 바꾼다(_desc_replaceable).
    """
    updates, conflicts = {}, []
    for f in MERGE_FIELDS:
        dv, kv = getattr(dup, f), getattr(keep, f)
        if _is_empty(dv):
            continue                      # 지울 행에 값이 없으면 줄 게 없다
        if _is_empty(kv):
            updates[f] = dv               # ← fill-empty (역방향 0건 실측)
            continue
        if dv == kv:
            continue
        if f == "description":
            ok, why = _desc_replaceable(kv, dv)
            if ok:
                updates[f] = dv
                continue
            conflicts.append((f, why))
        else:
            conflicts.append((f, ""))
    return updates, conflicts


class Command(BaseCommand):
    help = "sub_end=NULL Product 정리 (병합 · 중복 삭제 · 단독행 sub_end 복구)"

    def add_arguments(self, parser):
        parser.add_argument("--merge", action="store_true",
                            help="삭제 전에 남길 행의 빈 필드를 지울 행 값으로 채움")
        parser.add_argument("--dedupe", action="store_true", help="중복 행 삭제")
        parser.add_argument("--fill", action="store_true", help="단독 행 sub_end 복구")
        parser.add_argument("--apply", action="store_true", help="실제 DB에 반영 (없으면 조회만)")

    def handle(self, *args, **opts):
        do_merge, do_dedupe, do_fill = opts["merge"], opts["dedupe"], opts["fill"]
        if not (do_merge or do_dedupe or do_fill):
            do_merge = do_dedupe = do_fill = True   # 플래그 없으면 셋 다 리포트
        apply_ = opts["apply"]

        # 오프셋은 중복쌍에서 뽑는다 — --dedupe가 쌍을 지우기 전에 먼저 확보한다.
        offsets = self._derive_offsets()
        pairs, solos = self._split()

        self.stdout.write(
            f"sub_end=NULL 총 {len(pairs) + len(solos)}행 "
            f"- 중복 {len(pairs)}행 / 단독 {len(solos)}행"
        )

        if do_merge:
            self._merge(pairs, apply_)
            if apply_:
                pairs, _ = self._split()      # 병합 결과를 반영해 다시 읽는다
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

    # ── 1단계: 병합 ─────────────────────────────
    def _merge(self, pairs, apply_):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n[1] 중복 {len(pairs)}쌍 병합 (남길 행의 빈 필드를 지울 행 값으로)"))
        if not pairs:
            return

        plans = []
        filled = Counter()          # 필드별 채운 건수
        replaced = Counter()        # description 교체 사유별 건수
        conflicts = defaultdict(list)

        for dup, keep in pairs:
            updates, conf = _merge_plan(dup, keep)
            for f, why in conf:
                conflicts[f].append((dup, keep, why))
            if not updates:
                continue
            for f in updates:
                if f == "description" and not _is_empty(keep.description):
                    replaced[_desc_replaceable(keep.description, dup.description)[1]] += 1
                else:
                    filled[f] += 1
            plans.append((keep, dup, updates))

        self.stdout.write(f"  손댈 쌍 {len(plans)}쌍 / 손댈 것 없는 쌍 {len(pairs) - len(plans)}쌍")
        self.stdout.write("  ── 채울 필드 (남길 행이 비어 있던 것) ──")
        for f, n in filled.most_common():
            self.stdout.write(f"     {f:22s} {n}")
        if not filled:
            self.stdout.write("     없음")
        if replaced:
            self.stdout.write("  ── description 교체 (남길 값이 파편) ──")
            for why, n in replaced.most_common():
                self.stdout.write(f"     {why:22s} {n}")

        if conflicts:
            total = sum(len(v) for v in conflicts.values())
            self.stdout.write(self.style.WARNING(
                f"  ── 충돌 {total}건 — 남길 행 값을 그대로 둡니다 (사람이 판단할 것) ──"))
            for f, lst in sorted(conflicts.items(), key=lambda x: -len(x[1])):
                self.stdout.write(f"     {f} {len(lst)}건")
                for dup, keep, why in lst[:10]:
                    tail = f"  ({why})" if why else ""
                    self.stdout.write(
                        f"       {keep.issuer} {keep.product_no}: "
                        f"남길 P{keep.id}={getattr(keep, f)!r} / "
                        f"지울 P{dup.id}={getattr(dup, f)!r}{tail}"
                    )
                if len(lst) > 10:
                    self.stdout.write(f"       ... 외 {len(lst) - 10}건")

        for keep, dup, updates in plans[:5]:
            self.stdout.write(
                f"  P{dup.id} → P{keep.id} ({keep.issuer} {keep.product_no}): "
                f"{', '.join(sorted(updates))}"
            )
        if len(plans) > 5:
            self.stdout.write(f"  ... 외 {len(plans) - 5}쌍")

        if apply_ and plans:
            with transaction.atomic():
                for keep, _dup, updates in plans:
                    Product.objects.filter(id=keep.id).update(**updates)
            self.stdout.write(self.style.SUCCESS(
                f"  병합 완료: {len(plans)}쌍 / 채운 값 {sum(filled.values()) + sum(replaced.values())}개"))
            self.stdout.write(self.style.WARNING(
                "  description을 손댔다면 파생 필드를 다시 계산해야 합니다: "
                "python manage.py reparse_products"))

    # ── 2단계: 중복 삭제 ────────────────────────
    def _dedupe(self, pairs, apply_):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n[2] 중복 {len(pairs)}행 정리"))
        if not pairs:
            return

        blocked, unmerged, deletable = [], [], []
        dropped = Counter()      # 삭제와 함께 사라지는 충돌 값 (필드별)
        for dup, keep in pairs:
            refs = {
                "Investment": Investment.objects.filter(product=dup).count(),
                "WatchItem": WatchItem.objects.filter(product=dup).count(),
                "NotifiedMatch": NotifiedMatch.objects.filter(product=dup).count(),
                "RadarVerdict": RadarVerdict.objects.filter(product=dup).count(),
            }
            if any(refs.values()):
                blocked.append((dup, keep, refs))
                continue
            # 아직 옮기지 않은 데이터가 남아 있으면 지우지 않는다. --merge를 건너뛰고
            # --dedupe만 돌려도 영구 손실이 나지 않게 하는 안전장치다.
            left, conf = _merge_plan(dup, keep)
            if left:
                unmerged.append((dup, keep, sorted(left)))
            else:
                deletable.append((dup, keep))
                for f, _why in conf:
                    dropped[f] += 1

        self.stdout.write(
            f"  삭제 가능 {len(deletable)}행 / 참조가 붙어 보류 {len(blocked)}행 "
            f"/ 병합 안 돼 보류 {len(unmerged)}행"
        )

        # 충돌은 삭제를 막지 않는다(남길 행 값을 채택한다는 규칙). 다만 그 값들은
        # 지울 행과 함께 영영 사라지므로, 무엇을 버리는지 삭제 전에 보여 준다.
        # --merge를 건너뛰고 --dedupe만 돌린 사람에게는 여기가 유일한 고지다.
        if dropped:
            hard = {f: n for f, n in dropped.items() if f not in REPARSE_DERIVED}
            soft = {f: n for f, n in dropped.items() if f in REPARSE_DERIVED}
            self.stdout.write(self.style.WARNING(
                f"  ── 삭제와 함께 버려지는 값 {sum(dropped.values())}건 "
                "— 남길 행에 다른 값이 있어 채택하지 않은 것 ──"))
            for f, n in sorted(hard.items(), key=lambda x: -x[1]):
                self.stdout.write(f"     {f:22s} {n}")
            for f, n in sorted(soft.items(), key=lambda x: -x[1]):
                self.stdout.write(f"     {f:22s} {n}  (재파싱이 설명 원문에서 다시 계산)")
            self.stdout.write(
                "     내역은 --merge 리포트의 '충돌' 항목에서 확인하세요.")
        for dup, keep, refs in blocked:
            live = ", ".join(f"{k}={v}" for k, v in refs.items() if v)
            self.stdout.write(self.style.WARNING(
                f"  [보류] P{dup.id} {dup.issuer} {dup.product_no} - {live} "
                f"(남길 행 P{keep.id}로 옮긴 뒤 다시 실행)"
            ))
        if unmerged:
            self.stdout.write(self.style.WARNING(
                f"  [보류] 아직 남길 행에 없는 값이 있는 쌍 {len(unmerged)}건 — "
                "--merge --apply를 먼저 실행하세요."
            ))
            for dup, keep, fields in unmerged[:5]:
                self.stdout.write(
                    f"     P{dup.id} → P{keep.id} ({keep.issuer} {keep.product_no}): "
                    f"{', '.join(fields)}"
                )
            if len(unmerged) > 5:
                self.stdout.write(f"     ... 외 {len(unmerged) - 5}건")
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

    # ── 3단계: 단독 행 sub_end 복구 ─────────────
    def _fill(self, solos, offsets, apply_):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n[3] 단독 {len(solos)}행 sub_end 복구"))
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
