"""
SEIBro '상환종목조회' 화면(menu 198)의 조회 API를 Playwright로 호출해
ELS/ELB 실제 상환 결과(조기상환/만기상환, 시점)를 대량 수집한다.

scrape_seibro_history.py(발행종목조회)와 동일한 구조/제약:
  - callServletService.jsp가 END_PAGE만큼 그대로 반환(최대 9999 확인됨)
  - Django ORM은 sync_playwright() 컨텍스트 밖에서만 호출(비동기 안전장치 회피)
  - 수집(JSONL) → 적재(DB) 2단계 분리

주의: 이 API에는 수익률(%)·손실금액 필드가 없다(상환유형·시점만 제공).

사용:
  python manage.py scrape_seibro_redemptions --start-year 2003 --end-year 2026
"""

import html
import json
import re
import time
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import HistoricalRedemption

LIST_URL = "https://seibro.or.kr/websquare/control.jsp?w2xPath=/IPORTAL/user/derivCombi/BIP_CNTS07013V.xml&menuNo=198"
API_PATH = "/websquare/engine/proworks/callServletService.jsp"
MAX_PAGE_SIZE = 9999

REQ_TEMPLATE = (
    '<reqParam action="redSecnPList2EL1" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask">'
    '<SECN_TPCD value="99"/><MENU_NO value="198"/>'
    '<CMM_BTN_ABBR_NM value="total_search,openall,print,hwp,word,pdf,seach,xls,"/>'
    '<W2XPATH value="/IPORTAL/user/derivCombi/BIP_CNTS07013V.xml"/>'
    '<ISSUCO_CUSTNO value=""/><DERISEC_EXER_TPCD value="\'3\' \'2\'"/>'
    '<RED_DT1 value="{start}"/><RED_DT2 value="{end}"/>'
    '<ISIN value=""/><START_PAGE value="1"/><END_PAGE value="{end_page}"/>'
    '<KOR_SECN_NM value=""/><SECN_BASSET_SORT_CD value=""/></reqParam>'
)

COUNT_TEMPLATE = (
    '<reqParam action="redSecnListCnt2EL1" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask">'
    '<SECN_TPCD value="99"/><MENU_NO value="198"/>'
    '<CMM_BTN_ABBR_NM value="total_search,openall,print,hwp,word,pdf,seach,xls,"/>'
    '<W2XPATH value="/IPORTAL/user/derivCombi/BIP_CNTS07013V.xml"/>'
    '<ISSUCO_CUSTNO value=""/><DERISEC_EXER_TPCD value="\'3\' \'2\'"/>'
    '<RED_DT1 value="{start}"/><RED_DT2 value="{end}"/>'
    '<ISIN value=""/><KOR_SECN_NM value=""/><SECN_BASSET_SORT_CD value=""/></reqParam>'
)

RESULT_BLOCK_RE = re.compile(r"<result>(.*?)</result>", re.S)
ATTR_RE = re.compile(r'(\w+)\s+value="([^"]*)"')

RAW_DIR = Path(settings.BASE_DIR) / "seibro_raw_redeem"


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
    help = "SEIBro 상환종목조회 API에서 ELS/ELB 실제 상환결과를 대량 수집 (HistoricalRedemption)"

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int, default=2003)
        parser.add_argument("--end-year", type=int, default=date.today().year)
        parser.add_argument("--delay", type=float, default=0.5)
        parser.add_argument("--skip-scrape", action="store_true")

    def handle(self, *args, **opts):
        start_year = opts["start_year"]
        end_year = opts["end_year"]
        delay = opts["delay"]

        RAW_DIR.mkdir(exist_ok=True)

        if not opts["skip_scrape"]:
            self._scrape(start_year, end_year, delay)

        self._load_into_db(start_year, end_year)

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
        yield from self._chunks(page, start_s, mid.strftime("%Y%m%d"), delay)
        next_day = date.fromordinal(mid_ord + 1)
        yield from self._chunks(page, next_day.strftime("%Y%m%d"), end_s, delay)

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

    def _load_into_db(self, start_year, end_year):
        before = HistoricalRedemption.objects.count()
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
                HistoricalRedemption.objects.bulk_create(objs, ignore_conflicts=True)
            self.stdout.write(f"[적재:{year}] {len(objs)}건 처리")

        after = HistoricalRedemption.objects.count()
        self.stdout.write(
            f"완료: 원본 {total_read}건 / 신규 저장 {after - before}건 / DB 총 {after}건"
        )

    def _row_to_obj(self, r):
        isin = r.get("ISIN", "").strip()
        red_dt = _to_date(r.get("RED_DT"))
        if not isin or not red_dt:
            return None
        assets = [r[f"BASSET_SECN_NM{i}"].strip() for i in (1, 2, 3)
                  if r.get(f"BASSET_SECN_NM{i}", "").strip()]
        return HistoricalRedemption(
            isin=isin,
            name=r.get("KOR_SECN_NM", "").strip(),
            issuer=r.get("REP_SECN_NM", "").strip(),
            product_type=r.get("SECN_TPNM", "").strip(),
            recu_whcd=r.get("RECU_WHCD", "").strip(),
            issue_date=_to_date(r.get("ISSU_DT")),
            expiry_date=_to_date(r.get("XPIR_DT")),
            redemption_date=red_dt,
            exercise_type=r.get("DERISEC_EXER_TPCD", "").strip(),
            planned_term_months=int(r["XPIR_MONTH"]) if r.get("XPIR_MONTH", "").isdigit() else None,
            held_months=int(r["RED_MONTH"]) if r.get("RED_MONTH", "").isdigit() else None,
            asset_type_name=r.get("STND_BASSET_SORT_NM", "").strip(),
            basset_count=int(r["BASSET_SECNCNT"]) if r.get("BASSET_SECNCNT", "").isdigit() else None,
            assets=assets,
        )
