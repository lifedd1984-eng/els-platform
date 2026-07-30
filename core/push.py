"""웹 푸시 발송 (pywebpush + VAPID).

구독 저장/삭제는 views의 push_subscribe/push_unsubscribe,
실제 발송은 여기 send_to_user 하나로 통일한다 (notify.py, push_test 공용).
"""

import json

from django.conf import settings


def send_to_user(user, title, body, url="/", tag=None, stdout=None):
    """사용자의 모든 구독 기기로 발송. 만료 구독(404/410)은 삭제. 성공 건수 반환.

    VAPID 키 미설정·pywebpush 미설치 환경(로컬 개발 등)에서는 조용히 0을 반환해
    텔레그램 등 나머지 알림 흐름을 막지 않는다.
    """
    if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY):
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    for sub in list(user.push_subscriptions.all()):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUB},  # pywebpush가 dict를 변형하므로 매번 새로 생성
                ttl=43200,
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                sub.delete()
            elif stdout:
                stdout.write(f"[푸시 실패] {user.username} status={status}")
        except Exception as e:  # 네트워크 오류 등 — 알림 배치 전체를 죽이지 않는다
            if stdout:
                stdout.write(f"[푸시 오류] {user.username} {e}")
    return sent
