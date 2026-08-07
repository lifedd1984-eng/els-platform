"""scrape_seibro_history의 조용한 유실 방어 검증.

왜 이 테스트가 있나 (2026-08-07)
  SEIBro 서블릿은 END_PAGE=9999로 요청해도 9999행에서 응답을 자른다.
  실측에서 2026-01-01~07-31 구간이 LIST_CNT=10219 / 수신 9999행이었다.
  지금은 _chunks가 건수 기준으로 구간을 쪼개 이 상한을 피하지만, 하루치가
  상한을 넘거나 서버가 상한을 낮추면 '적게 받고도 성공'으로 끝나 버린다.
  그 조용한 유실을 크게 실패시키는 장치가 살아 있는지 확인한다.
"""

import json
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.management.commands import scrape_seibro_history as mod
from core.models import HistoricalIssue


def _row_xml(isin, seq, issuer="한국투자증권", issue="20260120", expiry="20290119"):
    return (f'<result><ISIN value="{isin}"/><SHOTN_ISIN value=""/>'
            f'<KOR_SECN_NM value="{issuer}(ELS){seq}"/><REP_SECN_NM value="{issuer}"/>'
            f'<SECN_TPNM value="ELS"/><RECU_WHCD value="공모"/>'
            f'<ISSU_CUR_TPCD_NM value="원화"/><ISSU_DT value="{issue}"/>'
            f'<XPIR_DT value="{expiry}"/><SECN_BASSET_SORT_CD value="지수"/>'
            f'<BASSET_SECNCNT value="1"/><KISP_BASSET_SECN_NM1 value="KOSPI200"/>'
            f'<KISP_BASSET_ISIN1 value="KR0000000001"/><STDPRC1 value="700"/>'
            f'<PAYIN_AMT value="1000000"/></result>')


class FakeSession:
    """서블릿 응답을 흉내낸다. count는 신고 건수, rows는 실제로 돌려줄 행 수."""

    def __init__(self, count, rows):
        self.count = count
        self.rows = rows
        self.headers = {}
        self.posts = []

    def get(self, *a, **kw):
        return None

    def close(self):
        pass

    def post(self, url, data=None, timeout=None):
        body = data.decode("utf-8")
        self.posts.append(body)
        if "issuSecnListCntEL1" in body:
            text = f'<data><LIST_CNT value="{self.count}"/></data>'
        else:
            text = "<data>" + "".join(
                _row_xml(f"KR6KS000{i:04d}", 18000 + i) for i in range(self.rows)
            ) + "</data>"
        return mock.Mock(text=text, raise_for_status=lambda: None)


class SeibroTruncationGuardTests(TestCase):
    def setUp(self):
        self.raw = mod.RAW_DIR
        self.raw.mkdir(exist_ok=True)
        self.year_file = self.raw / "2026.jsonl"
        self.backup = self.year_file.read_bytes() if self.year_file.exists() else None
        self.addCleanup(self._restore)

    def _restore(self):
        tmp = self.year_file.with_suffix(".jsonl.tmp")
        if tmp.exists():
            tmp.unlink()
        if self.backup is None:
            if self.year_file.exists():
                self.year_file.unlink()
        else:
            self.year_file.write_bytes(self.backup)

    def _run(self, count, rows):
        session = FakeSession(count, rows)
        with mock.patch.object(mod.Command, "_session", return_value=session):
            call_command("scrape_seibro_history", "--start-year", "2026",
                         "--end-year", "2026", "--delay", "0")
        return session

    def test_상한에서_잘리면_중단한다(self):
        """조회건수가 상한보다 큰데 딱 상한만큼 왔다 = 확정 유실 → 크게 실패."""
        with mock.patch.object(mod, "MAX_PAGE_SIZE", 5):
            with self.assertRaises(CommandError) as cm:
                self._run(count=9, rows=5)
        self.assertIn("잘렸다", str(cm.exception))

    def test_건수가_맞으면_정상수집(self):
        with mock.patch.object(mod, "MAX_PAGE_SIZE", 50):
            self._run(count=3, rows=3)
        lines = [json.loads(x) for x in
                 self.year_file.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(HistoricalIssue.objects.count(), 3)

    def test_적게_받으면_경고만_남기고_계속한다(self):
        """상한과 무관한 결손은 배치를 세우지 않는다 — 경고로만 남긴다."""
        with mock.patch.object(mod, "MAX_PAGE_SIZE", 50):
            self._run(count=5, rows=3)
        self.assertEqual(HistoricalIssue.objects.count(), 3)

    def test_실패하면_기존_원본파일을_깨뜨리지_않는다(self):
        self.year_file.write_text('{"기존":"원본"}\n', encoding="utf-8")
        with mock.patch.object(mod, "MAX_PAGE_SIZE", 5):
            with self.assertRaises(CommandError):
                self._run(count=9, rows=5)
        self.assertEqual(self.year_file.read_text(encoding="utf-8"), '{"기존":"원본"}\n')
        self.assertFalse(self.year_file.with_suffix(".jsonl.tmp").exists())
