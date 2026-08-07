"""
SEIBro(한국예탁결제원 증권정보포털, seibro.or.kr) '발행종목조회' 화면의 조회 API를
Playwright(브라우저 컨텍스트)로 호출해 ELS/ELB 발행 이력을 대량 수집한다.

실서비스(Product) 데이터와 무관 — 별도 테이블(HistoricalIssue)에 저장해
전체적인(수십년치) 백테스팅 연구용으로만 쓴다.

동작 원리
---------
발행종목조회 화면 뒤의 실제 데이터는 /websquare/engine/proworks/callServletService.jsp
서블릿이 XML로 응답한다. 요청 파라미터 END_PAGE 값이 그대로 반환 건수가 되는 것을
확인했다(9999까지 단일 요청으로 확인됨) — 페이지를 한 건씩 넘길 필요 없이
연도(또는 그 이하 구간)당 1~2회 요청으로 전체 발행 이력을 받을 수 있다.

9999는 상한이지 넉넉한 값이 아니다 (2026-08-07 실측)
  2026-01-01~07-31 구간은 LIST_CNT=10219인데 응답 <result> 블록은 정확히 9999개였다.
  _chunks가 조회건수 기준으로 구간을 반씩 쪼개 이 상한을 피하고, _check_complete가
  구간별 조회건수와 실제 수신 행수를 대조해 잘림을 크게 실패시킨다.

검색 필터는 화면 체크박스 전량이다 (BIP_CNTS07015V.xml 실측)
  기초자산유형 10종(A B 1 AB 6 8 4 2 5 7) · 기초자산개수 3종(1 2 3, '3'이 3개이상) ·
  만기 4종(1 2 3 4) · 발행구분 2종(공모 11 / 사모 21)이 선택 가능한 값의 전부다.
  화면의 '전체' 체크박스 값 9는 UI 편의용이라 서블릿에 그대로 보내면 0건이 온다.
  값을 비워도 결과는 개별코드 나열과 동일하다. 즉 여기서 빠지는 종목은 없다.

발행종목조회 목록 자체에 안 실리는 종목이 있다 (2026-08-07 조사)
  Product 391건이 이 테이블과 매칭되지 않는데, 원인은 수집 누락이 아니라
  SEIBro 목록 화면의 결손이다. 예: 한국투자증권 18425는 앞뒤 회차(18424·18426)가
  다 있는데 그 회차만 없고, 2025-12·2026-01 발행분도 8개월 뒤 재조회에서 여전히 없다.
  그런데 앞 회차 ISIN의 연번을 하나 올린 KR6KS0007LF0을 bassetInfoList로 물으면
  기초자산(HSCEI·KOSPI200)과 기준가가 정상으로 나온다 — 종목은 예탁결제원에 있고
  발행종목 목록에만 안 잡힌다(상환스케줄도 미등록). 재수집으로는 메워지지 않는다.

수집은 순수 requests로 한다 — 목록 화면을 한 번 GET해 세션 쿠키를 받으면
서블릿을 그대로 호출할 수 있다(브라우저 불필요). 수집 단계는 JSONL 파일에만
쓰고 DB 저장은 별도 단계에서 한다(원본 보존 + 재적재 가능).

사용:
  python manage.py scrape_seibro_history --start-year 2003 --end-year 2026
  python manage.py scrape_seibro_history --start-year 2020 --end-year 2020  (특정 연도만)
"""

import html
import json
import os
import re
import time
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import HistoricalIssue

LIST_URL = "https://seibro.or.kr/websquare/control.jsp?w2xPath=/IPORTAL/user/derivCombi/BIP_CNTS07015V.xml&menuNo=199"
API_PATH = "/websquare/engine/proworks/callServletService.jsp"
MAX_PAGE_SIZE = 9999  # 단일 요청 최대 확인된 건수

REQ_TEMPLATE = (
    '<reqParam action="issuSecnPListEL1" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask">'
    '<SECN_TPCD value="99"/><MENU_NO value="199"/>'
    '<CMM_BTN_ABBR_NM value="total_search,openall,print,hwp,word,pdf,searchIcon,seach,xls,"/>'
    '<W2XPATH value="/IPORTAL/user/derivCombi/BIP_CNTS07015V.xml"/>'
    '<ISSUCO_CUSTNO value=""/><KISP_BASSET_ISIN value=""/>'
    '<ISSU_DT1 value="{start}"/><ISSU_DT2 value="{end}"/>'
    '<XPIR value="1 2 3 4"/><RECU_WHCD value="11 21"/>'
    '<SECN_BASSET_SORT_CD value="A B 1 AB 6 8 4 2 5 7"/><BASSET_CNT value="1 2 3"/>'
    '<BASSET_NM value=""/><ISSU_CUR value=""/>'
    '<START_PAGE value="1"/><END_PAGE value="{end_page}"/></reqParam>'
)

COUNT_TEMPLATE = (
    '<reqParam action="issuSecnListCntEL1" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask">'
    '<SECN_TPCD value="99"/><MENU_NO value="199"/>'
    '<CMM_BTN_ABBR_NM value="total_search,openall,print,hwp,word,pdf,searchIcon,seach,xls,"/>'
    '<W2XPATH value="/IPORTAL/user/derivCombi/BIP_CNTS07015V.xml"/>'
    '<ISSUCO_CUSTNO value=""/><KISP_BASSET_ISIN value=""/>'
    '<ISSU_DT1 value="{start}"/><ISSU_DT2 value="{end}"/>'
    '<XPIR value="1 2 3 4"/><RECU_WHCD value="11 21"/>'
    '<SECN_BASSET_SORT_CD value="A B 1 AB 6 8 4 2 5 7"/><BASSET_CNT value="1 2 3"/>'
    '<BASSET_NM value=""/><ISSU_CUR value=""/></reqParam>'
)

RESULT_BLOCK_RE = re.compile(r"<result>(.*?)</result>", re.S)
ATTR_RE = re.compile(r'(\w+)\s+value="([^"]*)"')

RAW_DIR = Path(settings.BASE_DIR) / "seibro_raw"


def _parse_row(block: str) -> dict:
    return {m.group(1): html.unescape(m.group(2)) for m in ATTR_RE.finditer(block)}


def _to_date(s):
    if not s or len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


class Command(BaseCommand):
    help = "SEIBro 발행종목조회 API에서 ELS/ELB 발행 이력을 대량 수집 (HistoricalIssue)"

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int, default=2003)
        parser.add_argument("--end-year", type=int, default=date.today().year)
        parser.add_argument("--delay", type=float, default=0.8, help="요청 간 대기(초)")
        parser.add_argument("--skip-scrape", action="store_true",
                            help="수집 생략, seibro_raw/*.jsonl 파일만 DB에 적재")

    def handle(self, *args, **opts):
        start_year = opts["start_year"]
        end_year = opts["end_year"]
        delay = opts["delay"]

        RAW_DIR.mkdir(exist_ok=True)

        if not opts["skip_scrape"]:
            self._scrape(start_year, end_year, delay)

        self._load_into_db(start_year, end_year)

    # ── 1단계: 수집 (JSONL 파일에만 저장, DB 접근 없음) ──
    def _scrape(self, start_year, end_year, delay):
        """서블릿을 requests로 직접 호출한다.

        예전엔 Playwright 브라우저 안에서 fetch를 실행했는데, 목록 화면을 한 번
        GET해 세션 쿠키(WMONID/JSESSIONID)만 받으면 순수 requests로도 동일하게
        동작한다(collect_seibro_detail과 같은 방식). EC2에 playwright가 없어
        2026-07-20 이후 수집이 멈춰 있던 원인이라 브라우저 의존을 걷어냈다.
        """
        page = self._session()
        try:
            for year in range(start_year, end_year + 1):
                y_start = f"{year}0101"
                y_end = f"{year}1231" if year < date.today().year else date.today().strftime("%Y%m%d")
                out_path = RAW_DIR / f"{year}.jsonl"
                # 도중에 죽어도 기존 원본을 깨뜨리지 않도록 임시파일에 쓰고 마지막에 바꾼다.
                tmp_path = out_path.with_suffix(".jsonl.tmp")
                count = expected = 0
                try:
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        for start_s, end_s, cnt in self._chunks(page, y_start, y_end, delay):
                            rows = self._fetch_rows(page, start_s, end_s)
                            time.sleep(delay)
                            self._check_complete(start_s, end_s, cnt, len(rows))
                            for r in rows:
                                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                            count += len(rows)
                            expected += cnt
                    os.replace(tmp_path, out_path)
                except BaseException:
                    # 반쪽짜리 임시파일을 남기지 않는다 — 기존 원본은 그대로 살아 있다.
                    tmp_path.unlink(missing_ok=True)
                    raise
                self.stdout.write(
                    f"[수집:{year}] {count}건 -> {out_path.name}"
                    + ("" if count == expected else f" (조회건수 {expected}건과 불일치)"))

        finally:
            page.close()

    def _check_complete(self, start_s, end_s, cnt, got):
        """구간 조회건수(cnt)만큼 실제로 받았는지 확인한다.

        왜 필요한가 (2026-08-07 실측)
          END_PAGE=9999로 요청하면 서버는 딱 9999행에서 잘라 응답한다.
          2026-01-01~07-31 구간은 LIST_CNT=10219인데 <result> 블록은 9999개였다.
          지금은 _chunks가 건수 기준으로 구간을 쪼개 이 한계를 피하지만,
          ① 하루치가 9999건을 넘으면 더 쪼갤 날짜가 없어 그대로 잘리고
          ② 서버가 상한을 낮추면 모든 구간이 조용히 잘린다.
          어느 쪽이든 '적게 받았는데 성공으로 끝나는' 조용한 유실이라
          여기서 크게 실패시켜 배치 알림에 걸리게 한다.
        """
        if got == cnt:
            return
        if got >= MAX_PAGE_SIZE and cnt > got:
            raise CommandError(
                f"SEIBro 응답이 상한({MAX_PAGE_SIZE}행)에서 잘렸다 — "
                f"{start_s}~{end_s} 조회건수 {cnt}건 / 수신 {got}건. "
                "구간을 더 쪼갤 수 없어 유실이 확정이므로 중단한다.")
        self.stderr.write(
            f"[경고] {start_s}~{end_s} 조회건수 {cnt}건인데 {got}건만 수신했다.")

    def _session(self):
        """서블릿 호출용 requests 세션 (컨텍스트 매니저)."""
        import requests

        s = requests.Session()
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Content-Type": "text/xml",
            "Referer": LIST_URL,
        })
        s.get(LIST_URL, timeout=30)      # 세션 쿠키 확보
        return s

    @staticmethod
    def _post(page, body):
        """page = requests 세션. 서블릿 POST 후 본문 반환."""
        url = API_PATH if API_PATH.startswith("http") else "https://seibro.or.kr" + API_PATH
        r = page.post(url, data=body.encode("utf-8"), timeout=60)
        r.raise_for_status()
        return r.text

    def _get_count(self, page, start_s, end_s) -> int:
        body = COUNT_TEMPLATE.format(start=start_s, end=end_s)
        result = self._post(page, body)
        m = re.search(r'LIST_CNT value="(\d+)"', result)
        return int(m.group(1)) if m else 0

    def _chunks(self, page, start_s, end_s, delay):
        """구간의 건수가 MAX_PAGE_SIZE 이하가 될 때까지 절반씩 나눈다.

        (시작일, 종료일, 그 구간의 조회건수)를 돌려준다 — 받아 간 쪽에서
        실제 수신 행수와 대조해 잘림을 잡아낼 수 있게 건수를 함께 넘긴다.
        """
        cnt = self._get_count(page, start_s, end_s)
        time.sleep(delay)
        if cnt == 0:
            return
        if cnt <= MAX_PAGE_SIZE:
            yield (start_s, end_s, cnt)
            return

        d1 = date(int(start_s[:4]), int(start_s[4:6]), int(start_s[6:8]))
        d2 = date(int(end_s[:4]), int(end_s[4:6]), int(end_s[6:8]))
        if d1 >= d2:
            # 하루치가 상한을 넘었다 — 더 쪼갤 날짜가 없다. 잘림은 _check_complete가 잡는다.
            yield (start_s, end_s, cnt)
            return
        mid_ord = d1.toordinal() + (d2.toordinal() - d1.toordinal()) // 2
        mid = date.fromordinal(mid_ord)
        mid_s = mid.strftime("%Y%m%d")
        yield from self._chunks(page, start_s, mid_s, delay)
        next_day = date.fromordinal(mid_ord + 1).strftime("%Y%m%d")
        yield from self._chunks(page, next_day, end_s, delay)

    def _fetch_rows(self, page, start_s, end_s):
        body = REQ_TEMPLATE.format(start=start_s, end=end_s, end_page=MAX_PAGE_SIZE)
        result = self._post(page, body)
        return [_parse_row(b) for b in RESULT_BLOCK_RE.findall(result)]

    # ── 2단계: JSONL -> DB (브라우저 종료 후, 별도) ──
    def _load_into_db(self, start_year, end_year):
        before = HistoricalIssue.objects.count()
        total_read = 0
        for year in range(start_year, end_year + 1):
            path = RAW_DIR / f"{year}.jsonl"
            if not path.exists():
                continue
            objs = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    total_read += 1
                    obj = self._row_to_obj(r)
                    if obj:
                        objs.append(obj)
            if objs:
                HistoricalIssue.objects.bulk_create(objs, ignore_conflicts=True)
            self.stdout.write(f"[적재:{year}] {len(objs)}건 처리")

        after = HistoricalIssue.objects.count()
        self.stdout.write(
            f"완료: 원본 {total_read}건 / 신규 저장 {after - before}건 / DB 총 {after}건"
        )

    def _row_to_obj(self, r):
        isin = r.get("ISIN", "").strip()
        if not isin:
            return None
        assets = []
        for i in (1, 2, 3):
            nm = r.get(f"KISP_BASSET_SECN_NM{i}", "").strip()
            if not nm:
                continue
            assets.append({
                "name": nm,
                "isin": r.get(f"KISP_BASSET_ISIN{i}", "").strip(),
                "std_price": r.get(f"STDPRC{i}", "").strip(),
            })
        amt = r.get("PAYIN_AMT", "").strip()
        return HistoricalIssue(
            isin=isin,
            shotn_isin=r.get("SHOTN_ISIN", "").strip(),
            name=r.get("KOR_SECN_NM", "").strip(),
            issuer=r.get("REP_SECN_NM", "").strip(),
            product_type=r.get("SECN_TPNM", "").strip() or "ELS",
            recu_whcd=r.get("RECU_WHCD", "").strip(),
            currency_name=r.get("ISSU_CUR_TPCD_NM", "").strip(),
            issue_date=_to_date(r.get("ISSU_DT")),
            expiry_date=_to_date(r.get("XPIR_DT")),
            basset_sort=r.get("SECN_BASSET_SORT_CD", "").strip(),
            basset_count=int(r["BASSET_SECNCNT"]) if r.get("BASSET_SECNCNT", "").isdigit() else None,
            assets=assets,
            issue_amount=int(amt) if amt.isdigit() else None,
        )
