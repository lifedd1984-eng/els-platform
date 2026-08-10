"""텔레그램 알림 발송."""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def is_alert_target(user) -> bool:
    """이 사용자의 투자 알림을 텔레그램으로 보낼지 여부.

    텔레그램은 chat_id 하나짜리 공용 채널이라 누구 투자든 같은 방으로 간다.
    settings.TELEGRAM_ALERT_USERNAMES에 든 계정 것만 보낸다(빈 값이면 전부).
    웹 푸시는 원래 계정별로 가므로 이 제한을 걸지 않는다.
    """
    allowed = getattr(settings, "TELEGRAM_ALERT_USERNAMES", None)
    if not allowed:
        return True
    return bool(user) and getattr(user, "username", None) in allowed


def send_message(text: str) -> bool:
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.warning("텔레그램 토큰/chat_id 미설정 — 발송 생략")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        result = resp.json()
        if result.get("ok"):
            return True
        logger.error("텔레그램 발송 실패: %s", result.get("description"))
        return False
    except Exception as e:
        logger.error("텔레그램 API 오류: %s", e)
        return False
