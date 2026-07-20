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

주의: Django ORM은 Playwright sync_playwright() 컨텍스트 안에서 호출하면
asyncio 안전장치(SynchronousOnlyOperation)에 걸린다. 그래서 수집 단계에서는
JSONL 원본 파일에만 쓰고, 브라우저를 완전히 닫은 뒤 별도 단계에서 DB에 저장한다.

사용:
  python manage.py scrape_seibro_history --start-year 2003 --end-year 2026
  python manage.py scrape_seibro_history --start-year 2020 --end-year 2020  (특정 연도만)
"""

import html
import json
import re
import time
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

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
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(LIST_URL, timeout=30000)
            page.wait_for_timeout(1500)

            for year in range(start_year, end_year + 1):
                y_start = f"{year}0101"
                y_end = f"{year}1231" if year < date.today().year else date.today().strftime("%Y%m%d")
                out_path = RAW_DIR / f"{year}.jsonl"
                count = 0
                with open(out_path, "w", encoding="utf-8") as f:
                    for start_s, end_s in self._chunks(page, y_start, y_end, delay):
                        rows = self._fetch_rows(page, start_s, end_s)
                        time.sleep(delay)
                        for r in rows:
                            f.write(json.dumps(r, ensure_ascii=False) + "\n")
                        count += len(rows)
                self.stdout.write(f"[수집:{year}] {count}건 -> {out_path.name}")

            browser.close()

    def _get_count(self, page, start_s, end_s) -> int:
        body = COUNT_TEMPLATE.format(start=start_s, end=end_s)
        result = page.evaluate(
            """async (body) => {
                const r = await fetch('%s', {
                    method: 'POST', headers: {'Content-Type': 'text/xml'}, body,
                });
                return await r.text();
            }"""
            % API_PATH,
            body,
        )
        m = re.search(r'LIST_CNT value="(\d+)"', result)
        return int(m.group(1)) if m else 0

    def _chunks(self, page, start_s, end_s, delay):
        """구간의 건수가 MAX_PAGE_SIZE 이하가 될 때까지 절반씩 나눈다."""
        cnt = self._get_count(page, start_s, end_s)
        time.sleep(delay)
        if cnt == 0:
            return
        if cnt <= MAX_PAGE_SIZE:
            yield (start_s, end_s)
            return

        d1 = date(int(start_s[:4]), int(start_s[4:6]), int(start_s[6:8]))
        d2 = date(int(end_s[:4]), int(end_s[4:6]), int(end_s[6:8]))
        if d1 >= d2:
            yield (start_s, end_s)
            return
        mid_ord = d1.toordinal() + (d2.toordinal() - d1.toordinal()) // 2
        mid = date.fromordinal(mid_ord)
        mid_s = mid.strftime("%Y%m%d")
        yield from self._chunks(page, start_s, mid_s, delay)
        next_day = date.fromordinal(mid_ord + 1).strftime("%Y%m%d")
        yield from self._chunks(page, next_day, end_s, delay)

    def _fetch_rows(self, page, start_s, end_s):
        body = REQ_TEMPLATE.format(start=start_s, end=end_s, end_page=MAX_PAGE_SIZE)
        result = page.evaluate(
            """async (body) => {
                const r = await fetch('%s', {
                    method: 'POST', headers: {'Content-Type': 'text/xml'}, body,
                });
                return await r.text();
            }"""
            % API_PATH,
            body,
        )
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
