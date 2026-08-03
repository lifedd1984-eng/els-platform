"""500 에러를 텔레그램으로 올리는 로깅 핸들러.

왜 필요한가
  2026-08-03에 /weekly/가 비로그인 방문자 전원에게 500을 내며 10시간 죽어
  있었는데 아무도 몰랐다. Django 기본값이 django.request 에러를 mail_admins로만
  보내고 ADMINS가 비어 있어 통째로 버려졌기 때문이다. 파일 로그만으로는
  누군가 열어봐야 알 수 있으니, 터진 즉시 손에 알림이 오게 한다.

폭주 방지
  같은 경로가 계속 터지면 하루 수백 건이 온다. 경로+예외종류로 묶어
  ALERT_COOLDOWN 안에는 한 번만 보낸다. 대신 몇 번째인지 함께 알린다.
"""

import logging
import time
from collections import defaultdict

ALERT_COOLDOWN = 600      # 같은 오류를 다시 알리기까지(초)
MAX_LEN = 1200            # 텔레그램 메시지 길이 여유


class TelegramErrorHandler(logging.Handler):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._last = {}                      # 키 -> 마지막 발송 시각
        self._count = defaultdict(int)       # 키 -> 쿨다운 동안 쌓인 횟수

    def emit(self, record):
        try:
            self._emit(record)
        except Exception:
            # 로깅 핸들러가 예외를 뱉으면 원래 요청까지 망가진다. 조용히 넘긴다.
            pass

    def _emit(self, record):
        from django.conf import settings
        if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID):
            return

        request = getattr(record, "request", None)
        path = getattr(request, "path", "") or "-"
        exc = record.exc_info[0].__name__ if record.exc_info else "-"
        key = f"{path}|{exc}"

        now = time.time()
        self._count[key] += 1
        last = self._last.get(key, 0)
        if now - last < ALERT_COOLDOWN:
            return                            # 쿨다운 중 — 횟수만 세고 넘어간다

        n = self._count[key]
        self._last[key] = now
        self._count[key] = 0

        method = getattr(request, "method", "") or ""
        repeat = f"\n(최근 {ALERT_COOLDOWN // 60}분간 {n}회)" if n > 1 else ""
        body = self.format(record)
        if len(body) > MAX_LEN:
            body = body[:MAX_LEN] + "\n…(생략)"

        from core.telegram import send_message
        send_message(f"[사이트 오류] {method} {path}\n{exc}{repeat}\n\n{body}")
