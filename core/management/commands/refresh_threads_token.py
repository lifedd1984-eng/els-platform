"""스레드 장기 액세스 토큰 갱신 — .env의 THREADS_TOKEN을 새 값으로 교체한다.

왜 매일 돌리는가
  Threads 장기 토큰은 60일 만료다. 만료되면 갱신 API가 막혀 개발자 콘솔에서
  재발급받는 수밖에 없고, 그동안 자동 게시가 멈춘다. 매일 갱신하면 만료일이
  계속 60일 뒤로 밀려 사실상 만료되지 않는다.
  (갱신은 발급 후 24시간이 지나야 가능하다 — 그 전에는 정상적으로 실패한다.)

왜 실패 알림이 중요한가
  실패가 조용하면 60일간 아무도 모르다가 복구 불가 상태로 발견된다.
  그래서 연속 실패는 반드시 텔레그램으로 올린다. (2026-08-03)

주의
  .env에는 DB·메일·VAPID 키가 함께 있다. 파일을 통째로 다시 쓰면 안 되고
  THREADS_TOKEN 줄만 바꿔야 한다. 임시파일에 쓴 뒤 교체해 중간에 죽어도
  원본이 남게 한다.

사용:
  python manage.py refresh_threads_token             # 갱신
  python manage.py refresh_threads_token --dry-run   # 호출만, 저장 안 함
"""

import os
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

REFRESH_URL = "https://graph.threads.net/refresh_access_token"
KEY = "THREADS_TOKEN"


def _env_path():
    return Path(settings.BASE_DIR) / ".env"


def write_token(new_token):
    """`.env`의 THREADS_TOKEN 줄만 교체. 다른 키는 그대로 둔다."""
    path = _env_path()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{KEY}="):
            nl = "\n" if line.endswith("\n") else ""
            out.append(f"{KEY}={new_token}{nl}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        raise RuntimeError(f".env에 {KEY} 줄이 없다 — 최초 저장이 필요하다")

    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)          # 원자적 교체


class Command(BaseCommand):
    help = "스레드 장기 토큰 갱신 (.env의 THREADS_TOKEN 교체)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="갱신 요청만 보내고 저장은 하지 않음")

    def handle(self, *args, **opts):
        # dry-run은 점검용이라 실패해도 알림을 보내지 않는다(텔레그램 소음 방지)
        self.quiet = opts["dry_run"]
        old = os.environ.get(KEY, "")
        if not old:
            self._fail("THREADS_TOKEN 미설정 (.env)")
            return

        try:
            r = requests.get(REFRESH_URL, timeout=20, params={
                "grant_type": "th_refresh_token", "access_token": old})
            data = r.json()
        except Exception as e:
            self._fail(f"갱신 요청 실패: {e}")
            return

        new = data.get("access_token")
        if not new:
            # Meta는 실패 사유를 error.message에 담아 준다
            msg = (data.get("error") or {}).get("message") or str(data)[:200]
            self._fail(f"갱신 거부됨: {msg}")
            return

        days = int(data.get("expires_in", 0)) // 86400
        if opts["dry_run"]:
            self.stdout.write(f"[dry-run] 갱신 성공 — 만료 {days}일 뒤 (저장 안 함)")
            return

        write_token(new)
        self.stdout.write(self.style.SUCCESS(
            f"스레드 토큰 갱신 완료 — 만료 {days}일 뒤"))

        # 만료가 코앞이면 갱신이 성공해도 뭔가 잘못된 것이다(정상은 항상 59~60일)
        if days < 30:
            self._notify(f"[스레드] 토큰 갱신은 됐지만 만료가 {days}일밖에 안 남았습니다. "
                         f"매일 갱신이 도는지 확인이 필요합니다.")

    # ------------------------------------------------------------------
    def _fail(self, msg):
        self.stderr.write(self.style.ERROR(msg))
        self._notify(f"[스레드] 토큰 갱신 실패 — {msg}\n"
                     f"60일 넘게 실패하면 재발급밖에 없습니다. 확인 부탁드립니다.")
        raise SystemExit(1)

    def _notify(self, text):
        if getattr(self, "quiet", False):
            return
        try:
            from core.telegram import send_message
            send_message(text)
        except Exception:
            pass
