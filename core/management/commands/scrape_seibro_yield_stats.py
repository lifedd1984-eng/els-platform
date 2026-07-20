"""
SEIBro '주요기초자산별상환수익률'(menu 882) — 연도×기초자산조합별 실현수익률 공식 집계를 수집한다.

이 화면은 상환결과를 기초자산 조합 단위로 집계해 실제 실현수익률(RED_MARGIN_RATE)과
손실 건수/금액(MINUS_CNT/MINUS_RED_AMT)까지 SEIBro가 직접 계산해 제공한다.
END_PAGE를 9999로 주면 그 해의 전체 조합(연 수백 개 수준)이 한 번에 반환된다.

수집(JSONL)과 DB 적재를 분리하는 이유는 scrape_seibro_history.py와 동일
(Django ORM을 sync_playwright() 컨텍스트 안에서 호출하면 비동기 안전장치에 걸림).

사용:
  python manage.py scrape_seibro_yield_stats --start-year 2003 --end-year 2026
"""

import html
import json
import re
import time
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import HistoricalYieldStat

LIST_URL = "https://seibro.or.kr/websquare/control.jsp?w2xPath=/IPORTAL/user/derivCombi/BIP_CNTS07009V.xml&menuNo=882"
API_PATH = "/websquare/engine/proworks/callServletService.jsp"

REQ_TEMPLATE = (
    '<reqParam action="imptBassetRedPrateList" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask">'
    '<MENU_NO value="882"/><CMM_BTN_ABBR_NM value="total_search,openall,print,hwp,word,pdf,seach,xls,xls,"/>'
    '<W2XPATH value="/IPORTAL/user/derivCombi/BIP_CNTS07009V.xml"/><SECN_TPCD value="99"/><SORT value="1"/>'
    '<DERISEC_EXER_TPCD value="\'3\' \'2\'"/><RED_DT1 value="{start}"/><RED_DT2 value="{end}"/>'
    '<ASSET_TPCD value=""/><START_PAGE value="1"/><END_PAGE value="9999"/></reqParam>'
)

RESULT_BLOCK_RE = re.compile(r"<result>(.*?)</result>", re.S)
ATTR_RE = re.compile(r'(\w+)\s+value="([^"]*)"')

RAW_DIR = Path(settings.BASE_DIR) / "seibro_raw_yield"


def _parse_row(block: str) -> dict:
    return {m.group(1): html.unescape(m.group(2)) for m in ATTR_RE.finditer(block)}


def _int(s):
    s = (s or "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


def _float(s):
    s = (s or "").strip()
    try:
        return float(s)
    except ValueError:
        return None


class Command(BaseCommand):
    help = "SEIBro 주요기초자산별상환수익률(연도별 공식 집계) 수집 (HistoricalYieldStat)"

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
                body = REQ_TEMPLATE.format(start=y_start, end=y_end)
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
                rows = [_parse_row(b) for b in RESULT_BLOCK_RE.findall(result)]
                out_path = RAW_DIR / f"{year}.jsonl"
                with open(out_path, "w", encoding="utf-8") as f:
                    for r in rows:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                self.stdout.write(f"[수집:{year}] {len(rows)}건 조합 -> {out_path.name}")
                time.sleep(delay)

            browser.close()

    def _load_into_db(self, start_year, end_year):
        before = HistoricalYieldStat.objects.count()
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
                    objs.append(self._row_to_obj(year, r))
            if objs:
                HistoricalYieldStat.objects.bulk_create(objs)
            self.stdout.write(f"[적재:{year}] {len(objs)}건 처리")

        after = HistoricalYieldStat.objects.count()
        self.stdout.write(f"완료: 원본 {total_read}건 / 신규 {after - before}건 / DB 총 {after}건")

    def _row_to_obj(self, year, r):
        assets = [r[f"BASSET_SECN_NM{i}"].strip() for i in (1, 2, 3, 4)
                  if r.get(f"BASSET_SECN_NM{i}", "").strip()]
        return HistoricalYieldStat(
            year=year,
            basset_sort=r.get("SECN_BASSET_SORT_CD", "").strip(),
            assets=assets,
            count=_int(r.get("CNT_HAP")),
            redemption_amount=_int(r.get("REDAMT_VAL_HAP")),
            margin_rate=_float(r.get("RED_MARGIN_RATE")),
            planned_months=_int(r.get("XPIR_MMS")),
            held_months=_int(r.get("RED_MMS")),
            minus_count=_int(r.get("MINUS_CNT")),
            minus_amount=_int(r.get("MINUS_RED_AMT")),
        )
