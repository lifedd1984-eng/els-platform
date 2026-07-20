"""
DB(SQLite)와 .env를 백업 드라이브로 복사한다. 배치(run_scrape.bat) 마지막에 실행.

- SQLite 온라인 백업 API 사용 → 서버가 켜져 있어도 안전하게 스냅샷
- 대상: F:\\ELS_backup (없으면 로컬 platform\\backups 폴백)
- 보관: 최근 30개 (초과분 오래된 것부터 삭제)
"""

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

KEEP = 30
PRIMARY_DIR = Path("F:/ELS_backup")


class Command(BaseCommand):
    help = "DB + .env 백업 (F:\\ELS_backup, 최근 30개 유지)"

    def handle(self, *args, **opts):
        src = Path(settings.BASE_DIR) / "db.sqlite3"
        if not src.exists():
            self.stderr.write("db.sqlite3 없음")
            return

        dest_dir = PRIMARY_DIR
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            dest_dir = Path(settings.BASE_DIR) / "backups"
            dest_dir.mkdir(exist_ok=True)
            self.stdout.write(f"F: 접근 불가 - 로컬 폴백: {dest_dir}")

        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        dest = dest_dir / f"db_{stamp}.sqlite3"

        # SQLite 온라인 백업 (단순 파일복사와 달리 쓰기 중에도 일관된 스냅샷)
        with sqlite3.connect(str(src)) as conn, sqlite3.connect(str(dest)) as out:
            conn.backup(out)

        env = Path(settings.BASE_DIR) / ".env"
        if env.exists():
            shutil.copy2(env, dest_dir / "env_backup.txt")

        backups = sorted(dest_dir.glob("db_*.sqlite3"))
        removed = 0
        for old in backups[:-KEEP]:
            old.unlink()
            removed += 1

        size_mb = dest.stat().st_size / 1e6
        self.stdout.write(
            f"백업 완료: {dest} ({size_mb:.1f}MB) / 보관 {min(len(backups), KEEP)}개"
            + (f" / 오래된 {removed}개 삭제" if removed else "")
        )
