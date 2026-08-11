"""발행통화(currency)가 실물 근거와 어긋난 상품을 확인·보정한다.

왜 필요한가
  Product.currency는 수집 단계에서 상품설명 문자열로부터 정해진다
  (parsers.currency_from_description — scrape_kofia·import_els가 같이 쓴다).
  설명이 통화를 말하지 않는 상품이 대부분이라 그 값은 '아직 모른다'에 가깝고,
  KOFIA 응답에는 통화 필드가 아예 없어 수집 시점에는 더 나은 근거가 없다.
  실물 근거로 확정하는 일이 이 명령의 몫이다.

  (2026-08-07 실측: 신한투자증권 27659[id 2796]이 그 경우.
   fix_dup_products --merge로 설명에 '발행통화 : USD'가 들어왔는데 currency는 KRW.
   병합은 남길 행의 **빈 칸**만 채우므로 이미 KRW가 들어 있던 칸은 건드리지 않는다.)

무엇을 근거로 보는가
  ① SEIBro 발행내역(HistoricalIssue) — 예탁결제원 등록 원부. product_code(ISIN)로
     맞물린다. currency_name이 '원화'/'외화' 둘 중 하나로 온다.
     ⚠ 어느 외화인지까지는 말해 주지 않는다. 원화냐 아니냐만 확정할 수 있다.
     ⚠ 발행 **뒤에** 들어온다. 청약중 상품에는 아직 없다.
  ② 간이투자설명서의 '1증권당 액면가액' — 발행 조건 자체라 통화코드가 붙어 나온다.
     외화: '1증권당 액면가액 미화 일천 달러(USD 1,000)'
     원화: '1증권당 액면가액 10,000원'
     (2026-08-11 실측 24건·16개사 전수 일치 — 원화 22건, 외화 2건 모두 정확)
  ③ KOFIA 상품설명 원문(description) — '발행통화 : USD' 표기.

⚠ 근거로 삼지 않는 것
  ③은 currency를 만들어 낸 바로 그 문자열이라 혼자서는 근거가 되지 못한다
  (같은 값을 되풀이하는 셈이다). 화면에 보여 주기만 한다.

  설명서 **본문**의 '통화' 낱말도 근거가 아니다. 위험등급 안내문에 통화 이름이
  그냥 나열돼 있어서(예: '해외표준통화인 미국, 스위스, 영국, 일본, 캐나다의
  법정통화 및 유로화…', '미국 통화(USD)로 투자하는 증권은…') 통화와 무관한
  원화 상품이 EUR·USD로 잡힌다 — 확정 원화 22건 중 2건이 그렇게 오탐이었다.
  그래서 설명서에서는 ②(액면가액)만 본다.

자동 판정
  ②는 통화코드를 직접 주므로 사람이 값을 옮겨 적지 않아도 된다. --auto를 쓰면
  ②가 읽힌 건에 한해 그 값을 쓴다. ①과 어긋나면 여전히 저장하지 않는다.
  ②가 안 읽히면 예전처럼 근거만 보여 주고 --currency를 기다린다.

사용:
  python manage.py fix_currency                              # 근거와 어긋난 상품 점검 (dry-run)
  python manage.py fix_currency --id 2796                    # 한 건만
  python manage.py fix_currency --product-code KR6SH0009A78
  python manage.py fix_currency --id 2796 --currency USD           # 미리보기
  python manage.py fix_currency --id 2796 --currency USD --apply   # 실제 저장
  python manage.py fix_currency --auto                             # 설명서 근거로 일괄 미리보기
  python manage.py fix_currency --auto --apply                     # 설명서 근거로 일괄 저장

보정 후
  currency는 화면 배지(USD)와 통화 필터·프리셋 조건에만 쓰인다.
  금액을 환산하는 로직은 없으므로 수익률·손실확률·평가금액은 달라지지 않는다.
"""

import re

from django.core.management.base import BaseCommand, CommandError

from core.models import HistoricalIssue, Product

# 설명서·설명 원문에서 통화를 적는 항목 라벨.
_CURRENCY_LABEL = re.compile(r"(?:발\s*행|표\s*시|투\s*자)?\s*통\s*화\s*(?:의\s*종류)?\s*[:：]?\s*")

# 파생결합증권 발행에 쓰이는 통화. 오타를 그대로 저장하지 않으려고 열어 두지 않는다
# (2026-08-07 기준 DB에 실재하는 값은 KRW·USD 둘뿐이다).
KNOWN_CURRENCIES = ("KRW", "USD", "EUR", "JPY", "CNY", "CNH", "HKD", "GBP", "AUD")

# 라벨 뒤에서 통화코드/한글 표기를 찾는다.
_CURRENCY_VALUE = re.compile(
    r"\b(" + "|".join(KNOWN_CURRENCIES) + r")\b|(원화|미국\s*달러|달러|엔화|유로)")

_KOREAN_TO_CODE = {
    "원화": "KRW", "미국 달러": "USD", "미국달러": "USD", "달러": "USD",
    "엔화": "JPY", "유로": "EUR",
}

# SEIBro가 쓰는 두 값. 통화코드와의 대응은 '원화 = KRW' 하나만 확정이다.
SEIBRO_KRW = "원화"
SEIBRO_FOREIGN = "외화"


def extract_currency(text: str):
    """설명서/설명 원문 → (통화코드, 근거 원문). 못 찾으면 (None, "")."""
    if not text:
        return None, ""
    for label in _CURRENCY_LABEL.finditer(text):
        tail = text[label.end(): label.end() + 40]
        m = _CURRENCY_VALUE.search(tail)
        if not m:
            continue
        code = m.group(1) or _KOREAN_TO_CODE.get(re.sub(r"\s+", " ", m.group(2)).strip())
        if not code:
            continue
        evidence = text[max(0, label.start() - 40): label.end() + m.end() + 20].strip()
        return code, evidence
    return None, ""


# 간이투자설명서 '상품개요' 표의 액면가액·발행가액 항목. 유의사항 본문과 달리
# 발행 조건 자체라 상품마다 값이 다르고, 통화코드가 금액에 붙어 나온다.
_PAR_LABEL = re.compile(r"(?:1\s*증권\s*당\s*)?(?:액\s*면|발\s*행)\s*가\s*액\s*")
# '10,000원' / '10,000 원'
_PAR_KRW = re.compile(r"^[\d,]{3,}\s*원")
# '미화 일천 달러(USD 1,000)' — 한글 금액 표기를 건너뛰고 괄호 안 코드를 읽는다
_PAR_FX = re.compile(
    r"^(?:미화|외화)?\s*[가-힣\s]{0,10}?\(\s*(" + "|".join(KNOWN_CURRENCIES) + r")\s*[\d,]+\s*\)")


def extract_issue_currency(text: str):
    """간이투자설명서 전문 → (통화코드, 근거 원문). 못 찾으면 (None, "").

    액면가액 항목만 본다 — 이유는 파일 첫머리 '근거로 삼지 않는 것' 참조.
    """
    if not text:
        return None, ""
    for m in _PAR_LABEL.finditer(text):
        tail = text[m.end(): m.end() + 40]
        fx = _PAR_FX.match(tail)
        if fx:
            return fx.group(1), text[max(0, m.start() - 30): m.end() + fx.end()].strip()
        if _PAR_KRW.match(tail):
            return "KRW", text[max(0, m.start() - 30): m.end() + 12].strip()
    return None, ""


def seibro_conflict(currency: str, currency_name: str) -> bool:
    """Product.currency와 SEIBro 등록 통화가 어긋나는가."""
    if currency_name == SEIBRO_KRW:
        return currency != "KRW"
    if currency_name == SEIBRO_FOREIGN:
        return currency == "KRW"
    return False        # SEIBro 값이 없거나 모르는 값이면 판단하지 않는다


class Command(BaseCommand):
    help = "발행통화가 실물 근거와 어긋난 상품을 확인·보정 (기본 dry-run)"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, default=0, help="Product.id 한 건만")
        parser.add_argument("--product-code", type=str, default="",
                            help="KOFIA 고유코드(ISIN)로 한 건만")
        # str.upper — 소문자로 넣어도 받아 준다. choices가 오타를 여기서 막는다.
        parser.add_argument("--currency", type=str.upper, default="",
                            choices=KNOWN_CURRENCIES, metavar="CODE",
                            help=f"확인된 통화코드를 직접 지정 ({'·'.join(KNOWN_CURRENCIES)})")
        parser.add_argument("--auto", action="store_true",
                            help="설명서 액면가액에서 읽힌 통화코드를 값으로 쓴다 "
                                 "(SEIBro와 어긋나면 저장하지 않음)")
        parser.add_argument("--apply", action="store_true",
                            help="실제로 저장 (없으면 dry-run — 아무것도 쓰지 않음)")
        parser.add_argument("--no-fetch", action="store_true",
                            help="간이투자설명서 PDF를 받지 않고 DB 근거만 본다")
        parser.add_argument("--limit", type=int, default=0,
                            help="전체 점검 시 출력 건수 제한 (0=전부)")

    def handle(self, *args, **opts):
        value = (opts["currency"] or "").strip()
        targets = self._targets(opts)
        if not targets:
            self.stdout.write("발행통화가 근거와 어긋난 상품이 없습니다.")
            return

        if value and len(targets) > 1:
            raise CommandError(
                "--currency는 한 건에만 쓸 수 있습니다. --id 또는 --product-code로 좁혀 주세요."
            )
        if value and opts["auto"]:
            raise CommandError("--currency와 --auto는 함께 쓸 수 없습니다.")
        if opts["auto"] and opts["no_fetch"]:
            raise CommandError("--auto는 설명서를 읽어야 합니다. --no-fetch와 함께 쓸 수 없습니다.")

        mode = "적용" if opts["apply"] else "DRY-RUN (저장하지 않음)"
        if opts["auto"]:
            mode += " / 설명서 액면가액으로 자동 판정"
        self.stdout.write(f"대상 {len(targets)}건 / 모드: {mode}\n")

        applied = 0
        for p in targets:
            applied += bool(self._one(p, value, opts))

        self.stdout.write("")
        if opts["apply"]:
            self.stdout.write(self.style.SUCCESS(f"보정 완료 {applied}건"))
        else:
            hint = ("--auto --apply" if opts["auto"] else "--currency \"USD\" --apply")
            self.stdout.write("dry-run이라 아무것도 저장하지 않았습니다. "
                              f"적용하려면 {hint} 를 붙이세요.")

    # ------------------------------------------------------------------ 대상
    def _targets(self, opts):
        if opts["id"]:
            return list(Product.objects.filter(id=opts["id"]))
        if opts["product_code"]:
            return list(Product.objects.filter(product_code=opts["product_code"]))

        # 기본은 SEIBro 등록 통화와 어긋난 상품 전체.
        rows = list(Product.objects.exclude(product_code="")
                    .order_by("-sub_end", "id")
                    .values_list("id", "product_code", "currency"))
        registry = self._registry([code for _, code, _ in rows])
        hit = [pid for pid, code, cur in rows
               if seibro_conflict(cur, registry.get(code, ""))]
        if opts["limit"]:
            hit = hit[:opts["limit"]]
        # 위에서 정한 순서를 그대로 지킨다
        by_id = Product.objects.in_bulk(hit)
        return [by_id[pid] for pid in hit if pid in by_id]

    @staticmethod
    def _registry(codes):
        """ISIN → SEIBro 등록 통화('원화'/'외화'). SQLite 변수 한도를 피해 나눠 조회."""
        out = {}
        codes = [c for c in codes if c]
        for i in range(0, len(codes), 900):
            out.update(dict(
                HistoricalIssue.objects
                .filter(isin__in=codes[i:i + 900])
                .values_list("isin", "currency_name")
            ))
        return out

    # ------------------------------------------------------------------ 한 건
    def _one(self, p, value, opts):
        self.stdout.write("─" * 66)
        self.stdout.write(
            f"[{p.id}] {p.issuer} {p.product_no} ({p.product_code or '코드없음'})\n"
            f"    상품명   : {p.name}\n"
            f"    청약     : {p.sub_start} ~ {p.sub_end}\n"
            f"    현재값   : currency={p.currency!r}"
        )

        # ── 근거 ① SEIBro 발행내역 ──
        issue = (HistoricalIssue.objects.filter(isin=p.product_code).first()
                 if p.product_code else None)
        registered = issue.currency_name if issue else ""
        if issue:
            self.stdout.write(
                f"    SEIBro   : 등록통화={registered!r} 발행금액={issue.issue_amount!r} "
                f"발행일={issue.issue_date} 종목명={issue.name!r}"
            )
            if registered == SEIBRO_FOREIGN:
                self.stdout.write("               (외화까지만 확정 — 어느 통화인지는 알려주지 않는다)")
        else:
            self.stdout.write(self.style.WARNING(
                "    SEIBro   : 발행내역에서 찾지 못했습니다 "
                "(product_code가 비었거나 수집 범위 밖)"))

        # ── 근거 ③ 상품설명 원문 (보여 주기만 한다) ──
        desc_code, desc_evidence = extract_currency(p.description or "")
        self.stdout.write(f"    설명원문 : {p.description or '(없음)'}")
        if desc_evidence:
            self.stdout.write(f"    설명근거 : …{desc_evidence}… → {desc_code!r}")

        # ── 근거 ② 간이투자설명서 액면가액 ──
        pdf_code, pdf_evidence = None, ""
        if p.prospectus_url and not opts["no_fetch"]:
            from core.management.commands.fix_missing_assets import fetch_prospectus_text
            try:
                pdf_code, pdf_evidence = extract_issue_currency(
                    fetch_prospectus_text(p.prospectus_url))
            except Exception as e:                      # noqa: BLE001 — 조사용, 계속 진행
                self.stdout.write(self.style.WARNING(f"    설명서 조회 실패: {e}"))
        if pdf_evidence:
            self.stdout.write(f"    설명서근거: …{pdf_evidence}… → {pdf_code!r}")
        elif not p.prospectus_url:
            self.stdout.write("    설명서   : URL이 없습니다 (청약 종료 후 수집분)")
        elif not opts["no_fetch"]:
            self.stdout.write("    설명서   : 액면가액 항목을 찾지 못했습니다")

        if opts["auto"] and pdf_code:
            value = pdf_code
            self.stdout.write(f"    → 설명서 액면가액에서 확정: {value!r}")

        if not value:
            self.stdout.write(
                "    → 확인할 값이 지정되지 않았습니다. 근거를 보고 "
                "--currency 로 지정하세요.\n"
                f"       설명서: {p.prospectus_url or '(없음)'}\n"
                f"       발행사: {p.broker_url or '(없음)'}"
            )
            return False

        self.stdout.write(f"    → 적용할 값 : currency={p.currency!r} → {value!r}")

        if seibro_conflict(value, registered):
            self.stdout.write(self.style.ERROR(
                f"    지정한 값이 SEIBro 등록통화({registered!r})와 어긋납니다. 저장하지 않습니다."))
            return False
        if value == p.currency:
            self.stdout.write("    이미 같은 값입니다. 저장하지 않습니다.")
            return False

        if not opts["apply"]:
            return False

        p.currency = value
        p.save(update_fields=["currency"])
        self.stdout.write(self.style.SUCCESS("    저장했습니다."))
        return True
