"""
DB(SQLite)와 .env를 백업한다. 일일 배치 마지막에 실행.

- SQLite 온라인 백업 API 사용 → 서버가 켜져 있어도 안전하게 스냅샷
- gzip 압축 저장 (339MB → 약 52MB. EC2 디스크 7.6GB에서 무압축 30개는
  10GB로 디스크를 터뜨림 — PC F: 드라이브 시절 설계를 EC2에 맞게 수정)
- 대상: F:\\ELS_backup (PC 실행 시) 없으면 로컬 platform/backups (EC2)
- 보관: F: 30개 / 로컬(EC2) 7개 — 장기 보관은 PC가 매일 당겨가 30개 유지
"""

import gzip
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

KEEP_PRIMARY = 30   # F: (PC 대용량 드라이브)
KEEP_LOCAL = 7      # EC2 로컬 폴백 (디스크 여유 2GB 미만이라 최소만)
PRIMARY_DIR = Path("F:/ELS_backup")


class Command(BaseCommand):
    help = "DB + .env 백업 (gzip, F: 30개 / 로컬 7개 유지)"

    def handle(self, *args, **opts):
        src = Path(settings.BASE_DIR) / "db.sqlite3"
        if not src.exists():
            self.stderr.write("db.sqlite3 없음")
            return

        # F: 드라이브는 Windows(PC)에서만 — 리눅스에선 'F:'가 상대경로로
        # 성립해버려 ~/els/F:/ 같은 엉뚱한 폴더가 생기므로 OS로 판별
        import os
        dest_dir = None
        keep = KEEP_PRIMARY
        if os.name == "nt":
            try:
                PRIMARY_DIR.mkdir(parents=True, exist_ok=True)
                dest_dir = PRIMARY_DIR
            except OSError:
                pass
        if dest_dir is None:
            dest_dir = Path(settings.BASE_DIR) / "backups"
            dest_dir.mkdir(exist_ok=True)
            keep = KEEP_LOCAL
            self.stdout.write(f"로컬 백업: {dest_dir} (보관 {keep}개)")

        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        dest = dest_dir / f"db_{stamp}.sqlite3.gz"

        # SQLite 온라인 백업으로 일관 스냅샷 → gzip 압축 저장
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with sqlite3.connect(str(src)) as conn, sqlite3.connect(str(tmp_path)) as out:
                conn.backup(out)
            with open(tmp_path, "rb") as f_in, gzip.open(dest, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)
        finally:
            tmp_path.unlink(missing_ok=True)

        env = Path(settings.BASE_DIR) / ".env"
        if env.exists():
            shutil.copy2(env, dest_dir / "env_backup.txt")

        # 구형(무압축 .sqlite3) 포함해 보관 개수 관리
        backups = sorted(list(dest_dir.glob("db_*.sqlite3.gz"))
                         + list(dest_dir.glob("db_*.sqlite3")))
        removed = 0
        for old in backups[:-keep]:
            old.unlink()
            removed += 1

        size_mb = dest.stat().st_size / 1e6
        self.stdout.write(
            f"백업 완료: {dest} ({size_mb:.1f}MB 압축) / 보관 {min(len(backups), keep)}개"
            + (f" / 오래된 {removed}개 삭제" if removed else "")
        )
