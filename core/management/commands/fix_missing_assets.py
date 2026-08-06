"""기초자산(assets_raw)이 빈 채로 들어온 상품을 실물 근거로 보정한다.

왜 필요한가
  KOFIA 청약중 목록 API(DISDlsOfferSO.selectSubscribing)는 기초자산을 val8로 주는데,
  일부 상품에서 이 칸을 **빈 문자열로 내려준다**. 우리 파싱 문제가 아니라 소스 결손이라
  KOFIA 웹 화면에서도 그 칸이 비어 있다 — 그래서 웹을 봐도 답이 없다.
  (2026-08-06 실측: 청약중 425건 중 NH투자증권 25034·25065 두 건이 val8 빈 값)

  기초자산이 없으면 asset_type(지수형/종목형)이 정해지지 않고, 시세 티커를 못 잡아
  손실확률 시뮬레이션도 돌지 않는다. 화면에는 기초자산·유형 칸이 빈 채로 노출되고
  레이더 신호(유형별 TOP5)와 유형 필터에서는 통째로 빠진다.

  다만 같은 KOFIA 응답에 **간이투자설명서 PDF 링크**가 함께 온다. 그 설명서의
  '상품개요' 표에는 기초자산이 명시돼 있다. 이 명령은 그 표를 읽어 **후보값과 근거 원문**을
  보여준다 — 사람이 확인한 뒤 적용하라는 뜻이다.

⚠ 왜 배치가 자동으로 채우지 않는가
  설명서 양식이 발행사마다 달라 추출을 100% 믿을 수 없다. 그래서 수집 배치(scrape_kofia)는
  건드리지 않고, 사람이 근거를 보고 확정하는 이 명령으로 분리했다.
  틀린 기초자산이 들어가면 유형·손실확률·배지가 전부 어긋난다 — 빈 채로 두고
  경보하는 편이 낫다. 추출값은 어디까지나 **사람이 대조할 후보**다.

추출기 실측 (2026-08-06, KOFIA가 기초자산을 정상으로 내려준 33건 대조)
  일치 28 / 불일치 5 / 보류 0.
  불일치 5건은 전부 **표기 차이**이고 자산 자체를 잘못 집은 건 0건이었다:
    · 4건 '국고채권 3개월 금리'  — KOFIA만 '(민평3사)' 한정어를 덧붙인다(설명서엔 없음)
    · 1건 'Micron Technology Inc.' — KOFIA는 'Micron Technology'
  초안(상품개요 앵커 없이 첫 매칭)은 10건 중 3건에 그쳤고 투자유의사항 본문을
  잘못 물었다. 지금 구조가 그것을 막는다.

사용:
  python manage.py fix_missing_assets                      # 전체 점검 (dry-run)
  python manage.py fix_missing_assets --id 4972            # 한 건만
  python manage.py fix_missing_assets --product-code KR6NH0006VC5
  python manage.py fix_missing_assets --id 4972 --assets "KOSDAQ150 Index"           # 미리보기
  python manage.py fix_missing_assets --id 4972 --assets "KOSDAQ150 Index" --apply   # 실제 저장

보정 후
  asset_type은 이 명령이 parsers.classify_asset으로 함께 갱신한다.
  손실확률(loss_prob)은 별도 배치다 — 아래 명령을 이어서 돌려야 한다.
      python manage.py simulate_products --missing-only
"""

import io
import re

from django.core.management.base import BaseCommand, CommandError

from core import parsers
from core.models import Product

TIMEOUT = 60
HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── 간이투자설명서 '상품개요' 표에서 기초자산 칸 읽기 ───────────────────
#
# 반드시 '상품개요' 라벨 뒤에서만 찾는다. 설명서 앞머리 투자유의사항에
# "기초자산의 가격에 연계하여…" 같은 문장이 반복돼 나와, 앵커 없이 첫 매칭을
# 집으면 그 본문을 물어버린다(유안타·키움 표본에서 실제로 발생).
_OVERVIEW_ANCHOR = re.compile(r"상\s*품\s*개\s*요")

# '상품개요' 라벨 이후 이만큼만 훑는다 — 표 한 덩어리 길이(실측 표본 최대 ~1,100자).
_OVERVIEW_WINDOW = 1500

# '기초자산' / '기초자산의 종류' 항목 라벨.
_ASSET_LABEL = re.compile(r"기\s*초\s*자\s*산(?:\s*의)?(?:\s*종\s*류)?\s*[:：]?\s*")

# 기초자산 칸의 끝 — 표에서 바로 다음에 오는 항목명들.
_VALUE_STOP = re.compile(
    r"모\s*집|액\s*면|1\s*증\s*권\s*당|발\s*행\s*수\s*량|발\s*행\s*총\s*액|"
    r"발\s*행\s*가|청\s*약|최\s*소\s*청\s*약|투\s*자\s*기\s*간"
)

# 기초자산 칸의 **시작** — 바로 앞 항목(종목명) 값의 끝. 다중 기초자산이면
# 표 칸이 여러 줄로 접히면서 라벨이 칸 가운데로 밀려, 앞줄 자산이 라벨보다
# 먼저 나온다. (실측: 한국투자증권 "…원금비보장형) KOSPI200 기초자산 S&P500 …",
#  NH투자증권 "…원금비보장형 삼성전자 보통주/ SK하이닉스 보통주/ 기초자산 Micron…")
# 라벨 앞도 같이 읽지 않으면 앞쪽 자산이 통째로 누락된다.
_CELL_START = re.compile(
    r"원금비보장형|원금보장형|원금지급형|원금부분지급형|원금비보장|고난도\s*금융투자상품|"
    r"\)|\]|］|】"
)
_PREFIX_WINDOW = 90

# 표 칸이 아니라 설명 문장을 물었을 때 걸러내는 말뭉치.
# 정상 기초자산명에는 이런 표현이 들어갈 일이 없다.
_PROSE = re.compile(
    r"습니다|바랍니다|하시기|경우|위험|손실|변동|가격|따라|다양한|관련|"
    r"결정|발생|투자|수익률|성격|추이|해당|이해|숙지"
)

# 설명서 표기 → KOFIA가 쓰는 표기. KOFIA는 지수를 '<이름> Index'로 내려주고
# (실측: 'KOSPI200 Index', 'S&P500 Index', 'HSCEI Index'),
# market.resolve_ticker가 그 ' Index' 접미사를 벗겨 티커를 찾는다.
# 설명서는 같은 자산을 'KOSPI200 지수'·'SK하이닉스 보통주'로 적으므로 맞춰 준다.
_INDEX_ALIASES = {
    "KOSPI200": "KOSPI200 Index",
    "KOSPI 200": "KOSPI200 Index",
    "KOSDAQ150": "KOSDAQ150 Index",
    "KOSDAQ 150": "KOSDAQ150 Index",
    "S&P500": "S&P500 Index",
    "S&P 500": "S&P500 Index",
    "SPX500": "S&P500 Index",
    "NIKKEI225": "Nikkei225 Index",
    "NIKKEI 225": "Nikkei225 Index",
    "NKY225": "Nikkei225 Index",      # 삼성증권 설명서 표기
    "NKY": "Nikkei225 Index",
    "HSCEI": "HSCEI Index",
    "EUROSTOXX50": "Euro Stoxx 50 Index",
    "EURO STOXX 50": "Euro Stoxx 50 Index",
    "EUROSTOXX 50": "Euro Stoxx 50 Index",
    "SX5E": "Euro Stoxx 50 Index",
}


# 표 머리글·쪽번호가 앞칸으로 딸려 들어오는 경우가 있어 조각 단위로 걸러낸다.
_NOISE_TOKEN = re.compile(
    r"^(종\s*목\s*명|항\s*목|내\s*용|구\s*분|-?\s*\d+\s*-?|Page\s*\d+|전자공시시스템.*)$",
    re.IGNORECASE,
)


def normalize_asset_token(token: str) -> str:
    """설명서 표기 한 조각을 KOFIA 표기로 맞춘다. 모르는 건 그대로 둔다.

    잡음 조각(표 머리글·쪽번호)이면 빈 문자열을 돌려준다.
    """
    t = token.strip()
    if _NOISE_TOKEN.match(t):
        return ""
    # 'Micron Technology Inc. (블룸버그 티커: MU UW)' 같은 부연 괄호는 떼어낸다
    t = re.sub(r"\s*\([^()]*(?:티커|블룸버그|Bloomberg)[^()]*\)\s*$", "", t,
               flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*(보통주|주식|지수|의\s*종류)\s*$", "", t).strip()
    key = re.sub(r"\s+", " ", t).upper()
    return _INDEX_ALIASES.get(key, t)


def _split_known_indexes(piece: str):
    """'S&P500 EUROSTOXX50'처럼 구분자 없이 공백으로만 붙은 지수를 쪼갠다.

    조각 전부가 아는 지수명일 때만 쪼갠다 — 'Micron Technology'처럼
    공백이 이름의 일부인 종목을 잘라 버리면 안 되기 때문이다.
    """
    words = piece.split()
    if len(words) < 2:
        return [piece]
    if all(w.upper() in _INDEX_ALIASES for w in words):
        return words
    return [piece]


def extract_assets_from_prospectus(text: str):
    """정규화된 설명서 텍스트 → (후보 assets_raw, 근거 원문). 못 믿으면 (None, 근거).

    근거 원문은 사람이 눈으로 대조하라고 항상 함께 돌려준다.
    """
    anchor = _OVERVIEW_ANCHOR.search(text)
    if not anchor:
        return None, ""
    # 탐색 범위는 '상품개요' 표 한 덩어리로 한정한다. 끝을 열어 두면 뒤쪽
    # '기초자산가격 변동성' 표나 발행사별 부록의 자산명을 잘못 집는다
    # (실측: 한국투자증권·NH투자증권 표본에서 발생).
    region = text[anchor.start(): anchor.start() + _OVERVIEW_WINDOW]

    # 같은 표 안에도 '기초자산 가격 변동 이외의 요인…' 같은 설명 칸이 먼저 오는
    # 발행사가 있다(교보증권). 라벨 위치를 하나씩 훑어 걸러지지 않는 첫 칸을 집는다.
    first_evidence = region[:400]
    for label in _ASSET_LABEL.finditer(region):
        tail = region[label.end(): label.end() + 120]
        stop = _VALUE_STOP.search(tail)
        if not stop:
            continue
        raw = tail[:stop.start()].strip()
        evidence = region[max(0, label.start() - 120):
                          label.end() + stop.start() + 60].strip()
        if _PROSE.search(raw) or len(raw) > 90:
            continue

        # 라벨 앞으로 접혀 올라간 자산이 있으면 함께 읽는다
        head = region[max(0, label.start() - _PREFIX_WINDOW): label.start()]
        starts = list(_CELL_START.finditer(head))
        prefix = head[starts[-1].end():].strip() if starts else ""
        if prefix and (_PROSE.search(prefix) or len(prefix) > 80):
            prefix = ""

        # 앞칸 맨 앞에 쪽번호·표 머리글이 통째로 딸려오는 설명서가 있다
        # (미래에셋 "- 3 - 항목 내용 삼성전자/SK하이닉스").
        prefix = re.sub(r"^(?:-\s*\d+\s*-|Page\s*\d+|항\s*목|내\s*용|종\s*목\s*명|구\s*분|\s)+",
                        "", prefix, flags=re.IGNORECASE).strip()

        raw = f"{prefix}/{raw}" if prefix else raw
        tokens = []
        for piece in re.split(r"[/,·]+", raw):
            if piece.strip():
                tokens.extend(_split_known_indexes(piece))
        tokens = [t for t in (normalize_asset_token(t) for t in tokens) if t]
        if not tokens or len(tokens) > 5:
            continue
        return "/".join(tokens), evidence
    return None, first_evidence


def fetch_prospectus_text(url: str, pages: int = 12) -> str:
    """설명서 PDF를 **메모리로만** 받아 앞쪽 페이지 텍스트를 공백 정규화해 반환.

    KOFIA 파일서버는 SSL 중간인증서가 없어 verify=False가 필요하다
    (parse_prospectus_dates와 같은 조건).
    """
    import pdfplumber
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    res = requests.get(url, timeout=TIMEOUT, verify=False, headers=HEADERS)
    res.raise_for_status()
    parts = []
    with pdfplumber.open(io.BytesIO(res.content)) as pdf:
        for page in pdf.pages[:pages]:
            parts.append(page.extract_text() or "")
    return re.sub(r"\s+", " ", "\n".join(parts))


class Command(BaseCommand):
    help = "기초자산이 빈 상품을 간이투자설명서 근거로 확인·보정 (기본 dry-run)"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, default=0, help="Product.id 한 건만")
        parser.add_argument("--product-code", type=str, default="",
                            help="KOFIA 고유코드(ISIN)로 한 건만")
        parser.add_argument("--assets", type=str, default="",
                            help="확인된 기초자산을 직접 지정 (예: \"KOSDAQ150 Index\"). "
                                 "여러 개면 '/'로 구분")
        parser.add_argument("--apply", action="store_true",
                            help="실제로 저장 (없으면 dry-run — 아무것도 쓰지 않음)")
        parser.add_argument("--no-fetch", action="store_true",
                            help="설명서 PDF를 받지 않고 DB 상태만 점검")

    def handle(self, *args, **opts):
        targets = self._targets(opts)
        if not targets:
            self.stdout.write("기초자산이 빈 상품이 없습니다.")
            return

        if opts["assets"] and len(targets) > 1:
            raise CommandError(
                "--assets는 한 건에만 쓸 수 있습니다. --id 또는 --product-code로 좁혀 주세요."
            )

        mode = "적용" if opts["apply"] else "DRY-RUN (저장하지 않음)"
        self.stdout.write(f"대상 {len(targets)}건 / 모드: {mode}\n")

        applied = 0
        for p in targets:
            applied += bool(self._one(p, opts))

        self.stdout.write("")
        if opts["apply"]:
            self.stdout.write(self.style.SUCCESS(f"보정 완료 {applied}건"))
            if applied:
                self.stdout.write(
                    "손실확률은 아직 비어 있습니다. 이어서 실행하세요:\n"
                    "  python manage.py simulate_products --missing-only"
                )
        else:
            self.stdout.write("dry-run이라 아무것도 저장하지 않았습니다. "
                              "적용하려면 --assets \"...\" --apply 를 붙이세요.")

    # ------------------------------------------------------------------ 대상
    def _targets(self, opts):
        from django.db.models import Q

        qs = Product.objects.all()
        if opts["id"]:
            qs = qs.filter(id=opts["id"])
        elif opts["product_code"]:
            qs = qs.filter(product_code=opts["product_code"])
        else:
            # 공백만 있는 값도 결손으로 본다 (TRIM 기준)
            qs = qs.filter(Q(assets_raw__isnull=True) | Q(assets_raw__regex=r"^\s*$"))
        return list(qs.order_by("-sub_end", "id"))

    # ------------------------------------------------------------------ 한 건
    def _one(self, p, opts):
        self.stdout.write("─" * 66)
        self.stdout.write(
            f"[{p.id}] {p.issuer} {p.product_no} ({p.product_code or '코드없음'})\n"
            f"    상품명   : {p.name}\n"
            f"    청약     : {p.sub_start} ~ {p.sub_end}\n"
            f"    설명원문 : {p.description}\n"
            f"    현재값   : assets_raw={p.assets_raw!r} asset_type={p.asset_type!r} "
            f"loss_prob={p.loss_prob!r}"
        )

        candidate, evidence = None, ""
        if p.prospectus_url and not opts["no_fetch"]:
            try:
                text = fetch_prospectus_text(p.prospectus_url)
                candidate, evidence = extract_assets_from_prospectus(text)
            except Exception as e:                      # noqa: BLE001 — 조사용, 계속 진행
                self.stdout.write(self.style.WARNING(f"    설명서 조회 실패: {e}"))
        elif not p.prospectus_url:
            self.stdout.write(self.style.WARNING("    간이투자설명서 URL이 없습니다."))

        if evidence:
            self.stdout.write(f"    설명서 근거 : …{evidence}…")
        self.stdout.write(f"    설명서 후보 : {candidate!r}")

        value = (opts["assets"] or "").strip()
        source = "--assets 지정값"
        if not value and candidate:
            value = candidate
            source = "설명서 자동추출"

        if not value:
            self.stdout.write(self.style.ERROR(
                "    → 확인 실패. 설명서 원문을 직접 보고 --assets 로 지정하세요.\n"
                f"       설명서: {p.prospectus_url or '(없음)'}\n"
                f"       발행사: {p.broker_url or '(없음)'}"
            ))
            return False

        asset_type = parsers.classify_asset(value) or ""
        self.stdout.write(
            f"    → 적용할 값 : assets_raw={value!r}  (근거: {source})\n"
            f"       asset_type = {asset_type!r}"
        )
        self._show_ticker_check(value)

        if not asset_type:
            self.stdout.write(self.style.ERROR(
                "    유형을 판정하지 못했습니다 — 값이 이상합니다. 저장하지 않습니다."))
            return False

        if not opts["apply"]:
            return False

        p.assets_raw = value
        p.asset_type = asset_type
        # 기초자산 없음으로 남은 시뮬 캐시는 무효 — 지워서 재계산 대상이 되게 한다
        p.loss_prob = None
        p.sim_result = None
        p.sim_samples = None
        p.save(update_fields=["assets_raw", "asset_type", "loss_prob",
                              "sim_result", "sim_samples"])
        self.stdout.write(self.style.SUCCESS("    저장했습니다."))
        return True

    def _show_ticker_check(self, value):
        """저장 전에 시세 티커가 잡히는지 확인 — 안 잡히면 손실확률이 안 나온다."""
        try:
            from core import market
        except Exception:                               # noqa: BLE001
            return
        for name in market.split_assets(value):
            tk = market.resolve_ticker(name)
            mark = "OK" if tk else "매핑없음"
            self.stdout.write(f"       시세 매핑 {name!r} → {tk!r} [{mark}]")
