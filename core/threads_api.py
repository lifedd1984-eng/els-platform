"""Meta Threads API 클라이언트 — 주간 TOP5 자동 포스팅용.

.env 필요 키:
  THREADS_TOKEN     장기 액세스 토큰 (60일, refresh_token()으로 갱신)
  THREADS_USER_ID   숫자 사용자 ID (최초 1회 fetch_user_id()로 확인)

포스팅 2단계: ① 컨테이너 생성 → ② 게시 (Meta 공식 플로우).
"""

import os

import requests

BASE = "https://graph.threads.net/v1.0"


def _token():
    t = os.environ.get("THREADS_TOKEN", "")
    if not t:
        raise RuntimeError("THREADS_TOKEN 미설정 (.env)")
    return t


def fetch_user_id():
    """토큰으로 내 계정 id·username 조회 (최초 설정 시 1회)."""
    r = requests.get(f"{BASE}/me", timeout=15,
                     params={"fields": "id,username", "access_token": _token()})
    r.raise_for_status()
    return r.json()


def post_text(text):
    """텍스트 스레드 1건 게시. 성공 시 게시물 id 반환."""
    uid = os.environ.get("THREADS_USER_ID", "")
    if not uid:
        raise RuntimeError("THREADS_USER_ID 미설정 (.env)")
    tok = _token()

    r = requests.post(f"{BASE}/{uid}/threads", timeout=20,
                      data={"media_type": "TEXT", "text": text, "access_token": tok})
    r.raise_for_status()
    creation_id = r.json()["id"]

    r = requests.post(f"{BASE}/{uid}/threads_publish", timeout=20,
                      data={"creation_id": creation_id, "access_token": tok})
    r.raise_for_status()
    return r.json().get("id", "")


def refresh_token():
    """장기 토큰 갱신(만료 24시간 전~60일 사이 언제든 가능). 새 토큰 반환."""
    r = requests.get(f"{BASE.replace('/v1.0', '')}/refresh_access_token", timeout=15,
                     params={"grant_type": "th_refresh_token", "access_token": _token()})
    r.raise_for_status()
    return r.json()   # {access_token, token_type, expires_in}
