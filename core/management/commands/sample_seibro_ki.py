"""
SEIBro 개별상품 상세 API(bassetInfoList / midValatSkedulRedCondiList)로
과거 ELS의 낙인(KI)·스텝다운 배리어를 표본 조사한다.

전수(29만건)는 부담이라 연도별 무작위 표본을 뽑아 통계적 분포를 파악한다.
공모 ELS만 상세가 공개된다(사모는 빈 응답).

상세 API 형식(ISIN 하나만):
  <reqParam action="bassetInfoList" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask"><ISIN value="..."/></reqParam>
  → "기준가대비 하한베리어 비율" = KI (예: 60,60,60)

사용:
  python manage.py sample_seibro_ki --per-year 400
"""

import html
import random
import re
import time
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import HistoricalIssue

API_PATH = "/websquare/engine/proworks/callServletService.jsp"
LIST_URL = "https://seibro.or.kr/websquare/control.jsp?w2xPath=/IPORTAL/user/derivCombi/BIP_CNTS07015V.xml&menuNo=199"

BASSET_TMPL = '<reqParam action="bassetInfoList" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask"><ISIN value="{isin}"/></reqParam>'
SKED_TMPL = '<reqParam action="midValatSkedulRedCondiList" task="ksd.safe.bip.cnts.DerivCombi.process.DeriELSPTask"><ISIN value="{isin}"/></reqParam>'

KI_RE = re.compile(
    r'GUBUN value="기준가대비 하한베리어 비율"/><BASSET1 value="([^"]*)"/><BASSET2 value="([^"]*)"/><BASSET3 value="([^"]*)"'
)
STEP_RE = re.compile(r'RED_CONDI_CONTENT value="[^"]*?×\s*(\d+)%\)?"')


class Command(BaseCommand):
    help = "과거 ELS 낙인(KI)·스텝다운 배리어 표본조사 (공모 ELS)"

    def add_arguments(self, parser):
        parser.add_argument("--per-year", type=int, default=400)
        parser.add_argument("--delay", type=float, default=0.25)
        parser.add_argument("--start-year", type=int, default=2005)
        parser.add_argument("--end-year", type=int, default=date.today().year)

    def handle(self, *args, **opts):
        from playwright.sync_api import sync_playwright

        per_year = opts["per_year"]
        delay = opts["delay"]

        # 연도별 표본 ISIN 선정 (공모 ELS, 아직 상세 미조회, 자산개수 있음)
        targets = []
        for year in range(opts["start_year"], opts["end_year"] + 1):
            qs = list(HistoricalIssue.objects.filter(
                issue_date__year=year, product_type="ELS",
                recu_whcd="공모", detail_fetched=False,
            ).values_list("isin", flat=True))
            random.shuffle(qs)
            picked = qs[:per_year]
            targets.extend((year, isin) for isin in picked)
            self.stdout.write(f"[{year}] 공모ELS 미조회 {len(qs)}건 중 {len(picked)}건 표본")

        self.stdout.write(f"총 조회 대상: {len(targets)}건")

        # 수집 (JSON 아님 → 브라우저 컨텍스트에서 fetch, 결과는 메모리에 모아 DB는 마지막에)
        results = {}  # isin -> (ki, steps)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(LIST_URL, timeout=30000)
            page.wait_for_timeout(1500)

            done = 0
            for year, isin in targets:
                ki = self._fetch_ki(page, isin)
                steps = None
                if ki is not None:
                    steps = self._fetch_steps(page, isin)
                results[isin] = (ki, steps)
                done += 1
                if done % 100 == 0:
                    self.stdout.write(f"  진행 {done}/{len(targets)}")
                time.sleep(delay)
            browser.close()

        # DB 반영 (브라우저 종료 후)
        with transaction.atomic():
            for isin, (ki, steps) in results.items():
                HistoricalIssue.objects.filter(isin=isin).update(
                    ki=ki, stepdown_barriers=steps, detail_fetched=True)

        got = sum(1 for k, _ in results.values() if k is not None)
        self.stdout.write(f"완료: {len(results)}건 조회 / KI 확보 {got}건")

    def _fetch_ki(self, page, isin):
        try:
            res = page.evaluate(
                """async (body) => {
                    const r = await fetch('%s', {method:'POST', headers:{'Content-Type':'text/xml'}, body});
                    return await r.text();
                }""" % API_PATH,
                BASSET_TMPL.format(isin=isin),
            )
        except Exception:
            return None
        m = KI_RE.search(res)
        if not m:
            return None
        vals = [int(x) for x in m.groups() if x.strip().isdigit()]
        return min(vals) if vals else None

    def _fetch_steps(self, page, isin):
        try:
            res = page.evaluate(
                """async (body) => {
                    const r = await fetch('%s', {method:'POST', headers:{'Content-Type':'text/xml'}, body});
                    return await r.text();
                }""" % API_PATH,
                SKED_TMPL.format(isin=isin),
            )
        except Exception:
            return None
        steps = [int(x) for x in STEP_RE.findall(html.unescape(res))]
        return steps or None
