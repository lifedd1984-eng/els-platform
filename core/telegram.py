"""텔레그램 알림 발송."""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


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
