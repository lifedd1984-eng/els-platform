"""SEIBro 발행이력(HistoricalIssue.isin)을 Product.product_code에 백필한다.

왜 필요한가
  서비스 테이블(Product)과 SEIBro 수집분(HistoricalIssue)을 잇는 유일한 키가
  product_code == isin인데, 운영 4,656건 중 885건만 값이 있고 그중 SEIBro와
  실제로 이어지는 건 469건뿐이다(2026-08-05 실측). 나머지는 엑셀 수입분이라
  코드가 비어 있어 SEIBro의 공시 기준가·확정 조기상환 평가일을 못 쓴다.
  이 커맨드가 코드를 채우면 backfill_eval_dates 등 후속 배치가 ISIN 직결로 붙는다.

매칭 키 — 확정 ISIN 469쌍을 정답지로 삼아 실측해서 정했다 (2026-08-05)
  ① 발행사 정규화: 표기가 두 테이블에서 다르다 (KB증권↔케이비증권,
     DB증권↔디비증권, 에스케이증권↔SK증권). ISSUER_ALIAS로 흡수한다.
  ② 회차번호: Product.product_no를 그대로 쓴다 — 운영 4,656건 전부 순수 숫자이고
     정답지 469쌍에서 SEIBro 추출값과 불일치가 0이었다. Product.name은 형태가
     제각각("1863" / "키움증권 제4156회 …")이라 파싱하지 않는다.
     SEIBro쪽은 종목명에서 뽑는다: "키움증권1863(ELS)", "NH투자증권2599(공모/ELB)",
     "한국투자증권트루온(ELS)446"(괄호 뒤), "다올투자증권(사모/ELB)58" 형태를 모두 처리.
  ③ 발행일: Product.real_issue_date가 있으면 그것(정답지 421/421 정확 일치),
     없으면 Product.issue_date(=청약종료일, 정답지 469/469가 ±1일 이내).
     그래서 허용오차를 ±1일로 잡는다.
  ④ 만기일 교차검증: 정답지 469쌍에서 만기가 469/469 정확 일치했다.
     다만 KOFIA와 SEIBro가 같은 상품 만기를 며칠 다르게 적는 경우가 실재하므로
     backfill_eval_dates와 같은 ±3일까지 허용한다(그 밖은 다른 상품으로 보고 버림).

  날짜 허용오차를 ±1에서 ±30까지 넓혀도 결과가 동일했다 —
  즉 매칭 실패는 날짜 오차가 아니라 SEIBro에 그 회차가 없어서다.

동명이인 방어 (실제 사례)
  키움증권 1863은 SEIBro에 두 건 있다.
    KR6KW0000SG0 키움증권1863(ELS)             발행 2022-02-18 ~ 2025-02-18
    KR6KW0005DP2 키움증권뉴글로벌100조1863(ELS) 발행 2026-05-04 ~ 2029-05-04
  (발행사, 회차) 키만 보면 4년 전 상품을 집는다. 실제로 SEIBro 293,088행 중
  같은 (발행사, 회차) 키에 2건 이상 몰린 키가 51,764개다. 그래서 발행일과
  만기일을 반드시 함께 보고, 그러고도 후보가 2건 이상이면 아무것도 쓰지 않는다.
  틀린 ISIN을 넣는 것이 비워두는 것보다 훨씬 나쁘다.

폴백(접미 일치)이 필요한 이유
  NH투자증권은 브랜드 접두가 숫자를 물고 있다 — 회차 145가 "N2145(공모/ELS)"로
  적혀 종목명 숫자열이 "2145"로 읽힌다. 그래서 ① 정확 일치가 실패했을 때만
  '숫자열이 product_no로 끝나는' 후보를 본다. 이 폴백은 발행일·만기 검증을
  똑같이 통과해야 하고 유일할 때만 채택한다. 정답지 469쌍 역검증에서 이 폴백이
  19건을 정확히 복원했고 오답은 0이었다. 3자리 미만 회차는 접미가 우연히
  걸릴 수 있어 제외한다(MIN_SUFFIX_DIGITS).

역검증 (2026-08-05 운영 DB)
  이미 ISIN이 확정된 469쌍에 이 알고리즘을 그대로 돌려 469/469 정답,
  오답 0, 모호 0, 미검출 0.

사용:
  python manage.py backfill_product_code              # 기본 = dry-run, 저장 안 함
  python manage.py backfill_product_code --held-only  # 보유중 상품만
  python manage.py backfill_product_code --apply      # 실제 저장 (이것만 씀)
"""

import re
from collections import Counter, defaultdict
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from core import parsers
from core.models import HistoricalIssue, Investment, Product

# Product.issue_date는 실제로는 청약종료일이라 SEIBro 발행일과 0~1일 어긋난다.
# real_issue_date가 있으면 정확히 일치한다. 둘 중 하나라도 이 안에 들면 통과.
ISSUE_DATE_TOLERANCE_DAYS = 1
# 만기는 정답지에서 100% 정확 일치했지만, KOFIA와 SEIBro가 같은 상품 만기를
# 1~3일 다르게 공시하는 사례가 있어 backfill_eval_dates와 같은 폭을 쓴다.
EXPIRY_TOLERANCE_DAYS = 3
# 접미 폴백을 허용할 최소 회차 자릿수. 2자리 이하는 우연 일치 위험이 크다.
MIN_SUFFIX_DIGITS = 3

# 두 테이블의 발행사 표기 차이 (Product 표기 → SEIBro 표기).
# 운영 실측으로 확인된 3건 + 향후 엑셀 수입분에서 나올 법한 표기를 함께 둔다.
ISSUER_ALIAS = {
    "KB증권": "케이비증권",
    "DB증권": "디비증권",
    "에스케이증권": "SK증권",
    "IBK투자증권": "아이비케이투자증권",
    "BNK투자증권": "비엔케이투자증권",
    "하이투자증권": "아이엠증권",
    "iM증권": "아이엠증권",
}

# SEIBro 종목명 안의 상품유형 표기 — "(ELS)" "(공모/ELS)" "(사모/ELB)"
_KIND = r"(?:공모/|사모/)?(?:ELS|ELB|DLS|DLB)"
_RE_SEQ_BEFORE = re.compile(r"(\d+)\s*\(" + _KIND + r"\)")   # 키움증권1863(ELS)
_RE_SEQ_AFTER = re.compile(r"\(" + _KIND + r"\)\s*(\d+)")    # 한국투자증권트루온(ELS)446
_RE_SEQ_TAIL = re.compile(r"(\d+)\D*$")                      # 괄호가 아예 없는 경우

def is_principal_protected(product_type, name, ki):
    """원금보장형(ELB·DLB)이면 True — ISIN 체계가 ELS와 다를 수 있는 부류다.

    왜 product_type만 보면 안 되나 (2026-08-06 운영 실측)
      product_type은 양방향으로 틀려 있어 단독으로는 못 쓴다.
        · '신한투자-ELB-4118' 등 이름이 명백히 ELB·DLB인 26건이 product_type='ELS'
        · 반대로 product_type='ELB'인데 '교보증권K(ELS) 23'처럼 ki=40이 박힌
          진짜 ELS가 6건 있다 (엑셀 수입분에는 name이 회차번호뿐인 294건도 있어
          이름만 봐도 안 된다).
      그래서 세 신호를 OR로 합치고, ki로 한 번 거른다.

    ki 조건이 안전장치다
      원금보장형에는 낙인 배리어라는 개념 자체가 없다. 운영 실측에서
      이름에 ELB·DLB 토큰이 있으면서 ki가 채워진 상품은 0건이었다.
      반대로 ki가 있으면 그건 낙인이 걸린 원금비보장 상품이므로,
      product_type이 'ELB'라 적혀 있어도 원금보장형으로 보지 않는다.
      이 한 줄이 위 6건(진짜 ELS)을 예외에서 도로 빼낸다.

    적용 범위를 좁게 둔 이유
      이 판정은 '기존 product_code 점검'의 오염 분류에만 쓴다. 매칭 로직은
      건드리지 않는다 — SEIBro에도 ELB는 정상 ISIN으로 실려 있고(운영에서
      ELB 298건 중 250건이 SEIBro와 이어진다) 매칭 대상에서 뺄 이유가 없다.

    이름 판정은 parsers.has_bond_name 한 곳에 있다. 여기서 설명(description)까지
    보지 않는 것은 이 커맨드가 상품유형을 고치는 자리가 아니기 때문이다 —
    설명까지 쓰는 전체 판정은 parsers.classify_product_type이 맡고,
    reparse_products가 그것으로 product_type을 소급 교정한다.
    """
    if ki is not None:
        return False
    if (product_type or "").strip().upper() in ("ELB", "DLB"):
        return True
    return parsers.has_bond_name(name)


def normalize_issuer(issuer):
    """Product 발행사 표기를 SEIBro 표기로 맞춘다."""
    issuer = (issuer or "").strip()
    return ISSUER_ALIAS.get(issuer, issuer)


def normalize_seq(value):
    """회차번호 정규화 — 앞자리 0 제거. 값이 없으면 None."""
    s = str(value or "").strip().lstrip("0")
    if not s:
        # "0" 또는 "000"이었다면 0회차, 애초에 비어 있었다면 None
        return "0" if str(value or "").strip() else None
    return s


def extract_seq(name):
    """SEIBro 종목명에서 회차번호를 뽑는다. 못 뽑으면 None.

    괄호 앞 숫자열을 최우선으로 본다 — "키움증권뉴글로벌100조1863(ELS)"에서
    브랜드에 섞인 100이 아니라 1863이 나와야 하기 때문이다.
    """
    name = name or ""
    for rx in (_RE_SEQ_BEFORE, _RE_SEQ_AFTER):
        found = rx.findall(name)
        if found:
            return normalize_seq(found[-1])
    tail = _RE_SEQ_TAIL.search(name)
    return normalize_seq(tail.group(1)) if tail else None


class SeibroIndex:
    """SEIBro 발행이력을 (발행사, 회차) / (발행사, 발행일)로 색인한다.

    293,088행 전체를 메모리에 올리면 운영 서버(RAM 1.8GB)에 부담이라
    대상 상품의 발행일 범위 밖은 애초에 읽지 않는다. 날짜 조건을 만족할 수
    없는 행이므로 결과는 전체 색인과 동일하다.
    """

    def __init__(self, rows):
        self.by_seq = defaultdict(list)
        self.by_date = defaultdict(list)
        self.rows = 0
        self.no_seq = 0
        for isin, name, issuer, issue_date, expiry_date in rows:
            self.rows += 1
            seq = extract_seq(name)
            if seq is None:
                self.no_seq += 1
                continue
            rec = (isin, name, issuer, issue_date, expiry_date, seq)
            self.by_seq[(issuer, seq)].append(rec)
            if issue_date:
                self.by_date[(issuer, issue_date)].append(rec)

    @classmethod
    def for_anchors(cls, anchors):
        """대상 상품들의 기준일 목록에 필요한 구간만 읽어 색인을 만든다."""
        if not anchors:
            return cls([])
        lo = min(anchors) - timedelta(days=ISSUE_DATE_TOLERANCE_DAYS)
        hi = max(anchors) + timedelta(days=ISSUE_DATE_TOLERANCE_DAYS)
        qs = HistoricalIssue.objects.filter(issue_date__gte=lo, issue_date__lte=hi)
        return cls(qs.values_list("isin", "name", "issuer", "issue_date", "expiry_date"))


def product_anchors(p):
    """상품의 발행일 후보 — 정확한 real_issue_date 우선, 없으면 issue_date."""
    return [d for d in (p.real_issue_date, p.issue_date) if d]


def resolve(index, issuer, product_no, anchors, expiry):
    """(상태, 매칭행, 매칭근거)를 돌려준다.

    상태: "matched" | "ambiguous" | "expiry_conflict" | "no_candidate"
    매칭행: (isin, name, issuer, issue_date, expiry_date, seq)
    """
    seq = normalize_seq(product_no)
    if not seq or not anchors:
        return "no_candidate", None, ""
    issuer = normalize_issuer(issuer)

    def date_ok(rec):
        if not rec[3]:
            return False
        return any(abs((rec[3] - a).days) <= ISSUE_DATE_TOLERANCE_DAYS for a in anchors)

    def expiry_ok(rec):
        # 어느 한쪽에 만기가 없으면 이 검증은 건너뛴다(정답지에선 결측이 없었다)
        if not expiry or not rec[4]:
            return True
        return abs((expiry - rec[4]).days) <= EXPIRY_TOLERANCE_DAYS

    # ── ① 발행사 + 회차 정확일치 + 발행일 ± 1일 (+ 만기 교차검증) ──
    dated = [r for r in index.by_seq.get((issuer, seq), []) if date_ok(r)]
    hits = {r[0]: r for r in dated if expiry_ok(r)}
    if len(hits) == 1:
        return "matched", next(iter(hits.values())), "발행사+회차+발행일±1(만기교차검증)"
    if len(hits) > 1:
        return "ambiguous", None, "발행사+회차+발행일±1"
    # 날짜까지 맞은 후보가 있었는데 만기에서 전부 탈락 = 다른 상품일 가능성이 크다
    expiry_conflict = bool(dated)

    # ── ② 접미 폴백: 브랜드 접두가 숫자를 물고 있는 발행사(NH의 "N2…") ──
    if len(seq) >= MIN_SUFFIX_DIGITS:
        cands = {}
        for anchor in anchors:
            for delta in range(-ISSUE_DATE_TOLERANCE_DAYS, ISSUE_DATE_TOLERANCE_DAYS + 1):
                for rec in index.by_date.get((issuer, anchor + timedelta(days=delta)), []):
                    if rec[5] != seq and rec[5].endswith(seq) and expiry_ok(rec):
                        cands[rec[0]] = rec
        if len(cands) == 1:
            return "matched", next(iter(cands.values())), "발행사+발행일+만기+회차접미일치"
        if len(cands) > 1:
            return "ambiguous", None, "발행사+발행일+만기+회차접미일치"

    return ("expiry_conflict" if expiry_conflict else "no_candidate"), None, ""


class Command(BaseCommand):
    help = "SEIBro ISIN을 발행사+발행일+회차로 찾아 Product.product_code에 백필"

    def add_arguments(self, parser):
        # 기본이 dry-run이다. --dry-run은 의도를 드러내고 싶을 때 쓰는 표시용.
        parser.add_argument("--dry-run", action="store_true",
                            help="기본 동작(집계만). --apply 없이는 언제나 이 모드다")
        parser.add_argument("--apply", action="store_true",
                            help="실제로 product_code를 저장한다")
        parser.add_argument("--held-only", action="store_true",
                            help="보유중 투자가 걸린 상품만 대상으로 한다")
        parser.add_argument("--samples", type=int, default=20,
                            help="출력할 매칭 샘플 수 (기본 20)")

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        n_samples = opts["samples"]
        if apply_ and opts["dry_run"]:
            raise CommandError("--dry-run과 --apply를 함께 줄 수 없다. 의도를 정하고 다시 실행할 것")

        held_ids = set(Investment.objects.filter(status="보유중")
                       .values_list("product_id", flat=True))

        qs = Product.objects.filter(Q(product_code="") | Q(product_code__isnull=True))
        if opts["held_only"]:
            qs = qs.filter(id__in=held_ids)
        targets = list(qs.order_by("id"))
        total = len(targets)
        held_total = sum(1 for p in targets if p.id in held_ids)

        anchors_all = [d for p in targets for d in product_anchors(p)]
        index = SeibroIndex.for_anchors(anchors_all)
        self.stdout.write(
            f"[SEIBro 색인] 발행일 구간 {min(anchors_all) if anchors_all else '-'}"
            f" ~ {max(anchors_all) if anchors_all else '-'} / "
            f"{index.rows}행 (회차추출 실패 {index.no_seq}행) / 키 {len(index.by_seq)}개")

        stat = Counter()
        held_stat = Counter()
        by_reason = Counter()
        samples = []
        held_samples = []
        # 서로 다른 Product가 같은 ISIN을 집는지 감시한다 (Product 중복행 탐지)
        isin_owners = defaultdict(list)
        # 이미 다른 Product가 쓰고 있는 ISIN을 집으면 그건 채우지 않는다
        used_codes = set(Product.objects.exclude(product_code="")
                         .exclude(product_code__isnull=True)
                         .values_list("product_code", flat=True))

        to_save = []
        for p in targets:
            status, rec, reason = resolve(
                index, p.issuer, p.product_no, product_anchors(p), p.expiry_date)
            if status == "matched" and rec[0] in used_codes:
                # 이미 다른 상품이 이 ISIN을 갖고 있다 — 둘 중 하나는 틀렸으므로 비워 둔다
                status, rec, reason = "already_used", rec, reason
            stat[status] += 1
            if p.id in held_ids:
                held_stat[status] += 1
            if status != "matched":
                if status in ("ambiguous",):
                    by_reason[f"후보 다수({reason})"] += 1
                continue
            by_reason[reason] += 1
            isin_owners[rec[0]].append(p)
            to_save.append((p, rec, reason))
            row = (p.issuer, p.name or p.product_no, p.issued_on or p.issue_date,
                   rec[0], rec[1], reason)
            if len(samples) < n_samples:
                samples.append(row)
            if p.id in held_ids and len(held_samples) < n_samples:
                held_samples.append(row)

        collisions = {k: v for k, v in isin_owners.items() if len(v) > 1}

        # ── 저장 ──
        saved = 0
        if apply_:
            for p, rec, reason in to_save:
                p.product_code = rec[0]
                p.save(update_fields=["product_code"])
                saved += 1
                self.stdout.write(
                    f"  저장 P{p.id} {p.issuer} {p.product_no} → {rec[0]} "
                    f"({rec[1]}) [{reason}]")

        # ── 보고 ──
        mode = "저장" if apply_ else "DRY-RUN(저장 안 함)"
        self.stdout.write("")
        self.stdout.write(f"[product_code 백필 · {mode}]")
        self.stdout.write(
            f"  총 대상 {total}건 (보유중 {held_total}건) — product_code가 빈 상품")
        self.stdout.write(
            f"  유일 매칭 {stat['matched']}건 (보유중 {held_stat['matched']}건)"
            + (f" / 실제 저장 {saved}건" if apply_ else ""))
        self.stdout.write(
            f"  후보 다수라 건너뜀 {stat['ambiguous']}건 "
            f"(보유중 {held_stat['ambiguous']}건)")
        self.stdout.write(
            f"  회차·발행일은 맞으나 만기 {EXPIRY_TOLERANCE_DAYS}일 초과 상충 "
            f"{stat['expiry_conflict']}건 (보유중 {held_stat['expiry_conflict']}건)")
        self.stdout.write(
            f"  다른 상품이 이미 쓰는 ISIN이라 건너뜀 {stat['already_used']}건")
        self.stdout.write(
            f"  후보 없음 {stat['no_candidate']}건 (보유중 {held_stat['no_candidate']}건)")

        if by_reason:
            self.stdout.write("\n  매칭 근거별:")
            for k, v in by_reason.most_common():
                self.stdout.write(f"    {k}: {v}건")

        if collisions:
            self.stdout.write(
                f"\n  [주의] 서로 다른 Product {sum(len(v) for v in collisions.values())}건이 "
                f"같은 ISIN {len(collisions)}종을 가리킨다.")
            self.stdout.write(
                "  발행사·회차·발행일이 같은 Product 중복행이 원인일 수 있다 "
                "(sub_end만 다른 쌍이 실제로 존재한다). 검토 후 적용할 것.")
            for isin, ps in list(collisions.items())[:10]:
                who = " ; ".join(
                    f"P{p.id}({p.issuer} {p.product_no} sub_end={p.sub_end})" for p in ps)
                self.stdout.write(f"    {isin} ← {who}")

        if samples:
            self.stdout.write(f"\n  매칭 샘플 {len(samples)}건 "
                              "(발행사 | 상품명 | 발행일 | ISIN | SEIBro명 | 근거):")
            for s in samples:
                self.stdout.write(
                    f"    {s[0]} | {str(s[1])[:24]} | {s[2]} | {s[3]} | {s[4]} | {s[5]}")
        if held_samples:
            self.stdout.write(f"\n  보유중 매칭 샘플 {len(held_samples)}건:")
            for s in held_samples:
                self.stdout.write(
                    f"    {s[0]} | {str(s[1])[:24]} | {s[2]} | {s[3]} | {s[4]} | {s[5]}")

        self._audit_existing(held_ids)

    def _audit_existing(self, held_ids):
        """이미 채워진 product_code가 SEIBro에 실재하는지 점검(오염 확인용, 수정 안 함).

        ISIN 형식이 아닌 코드를 두 갈래로 나눠 센다.
          · ELB·DLB(원금보장형) — 코드 체계가 다를 뿐 오염이 아니다. 운영에서
            'BSH0009J5' 같은 9자리 코드가 붙은 3건이 전부 여기였다 (2026-08-06).
          · 그 밖 — 진짜 오염 의심. 여기가 0이 아니면 봐야 한다.
        어느 쪽이든 product_code 값은 지우지 않는다. SEIBro 매칭엔 못 써도
        증권사 상세페이지 연결 등 다른 쓰임이 있다.
        """
        existing = list(Product.objects.exclude(product_code="")
                        .exclude(product_code__isnull=True)
                        .values_list("id", "issuer", "product_no", "product_code",
                                     "issue_date", "product_type", "name", "ki"))
        if not existing:
            return
        codes = {e[3] for e in existing}
        known = set(HistoricalIssue.objects.filter(isin__in=codes)
                    .values_list("isin", flat=True))
        latest = HistoricalIssue.objects.order_by("-issue_date").values_list(
            "issue_date", flat=True).first()
        orphans = [e for e in existing if e[3] not in known]
        non_isin = [e for e in orphans
                    if not (len(e[3]) == 12 and e[3].upper().startswith("KR"))]
        # ELB·DLB는 헛경보다. 없애지 않고 아래에서 따로 센다.
        bond = [e for e in non_isin if is_principal_protected(e[5], e[6], e[7])]
        bad_format = [e for e in non_isin if e not in bond]
        # after_cut의 판정 범위는 예전과 같다(ISIN 형식이 아닌 건 전부 제외).
        after_cut = [e for e in orphans
                     if e not in non_isin and latest and e[4] and e[4] > latest]
        self.stdout.write("\n  [기존 product_code 점검] (이 커맨드는 기존 값을 건드리지 않는다)")
        self.stdout.write(
            f"    채워진 상품 {len(existing)}건 중 SEIBro에 없는 코드 {len(orphans)}건 "
            f"(보유중 {sum(1 for e in orphans if e[0] in held_ids)}건)")
        self.stdout.write(
            f"      · ISIN 형식이 아님 {len(bad_format)}건  ← 오염 의심")
        self.stdout.write(
            f"      · ELB·DLB {len(bond)}건 — ISIN 체계가 다름, 정상 "
            "(원금보장형 파생결합사채)")
        self.stdout.write(
            f"      · SEIBro 최신 수집일({latest}) 이후 발행 {len(after_cut)}건  "
            "← 아직 수집 안 된 것뿐")
        self.stdout.write(
            f"      · 형식은 ISIN인데 SEIBro에 없음 "
            f"{len(orphans) - len(non_isin) - len(after_cut)}건  ← 확인 필요")
        for e in bad_format[:10]:
            self.stdout.write(
                f"        형식이상 P{e[0]} {e[1]} {e[2]} code={e[3]!r} issue={e[4]}")
        # 예외로 뺀 것도 눈에 보이게 남긴다 — 조용히 사라지면 나중에 못 찾는다.
        for e in bond[:10]:
            self.stdout.write(
                f"        ELB·DLB P{e[0]} {e[1]} {e[2]} code={e[3]!r} "
                f"name={e[6]!r} issue={e[4]}")
