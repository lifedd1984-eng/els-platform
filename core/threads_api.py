"""Meta Threads API 클라이언트 — 자동 포스팅 + 댓글 수집/답글용.

.env 필요 키:
  THREADS_TOKEN     장기 액세스 토큰 (60일, refresh_token()으로 갱신)
  THREADS_USER_ID   숫자 사용자 ID (최초 1회 fetch_user_id()로 확인)

포스팅 2단계: ① 컨테이너 생성 → ② 게시 (Meta 공식 플로우).
답글도 같은 2단계이고, 컨테이너 생성 때 reply_to_id만 더 붙인다.

이 파일은 API 호출만 한다. "무엇을 답할지"는 core/threads_replies.py가 정하고,
"언제 보낼지"는 management/commands/reply_threads.py가 정한다. 판단 로직을
여기 섞지 않는 이유는 법적 경계(정형 문구만 발송)를 한 파일에서 검증하기
위해서다.
"""

import os
import time

import requests

BASE = "https://graph.threads.net/v1.0"


def _token():
    t = os.environ.get("THREADS_TOKEN", "")
    if not t:
        raise RuntimeError("THREADS_TOKEN 미설정 (.env)")
    return t


def _uid():
    uid = os.environ.get("THREADS_USER_ID", "")
    if not uid:
        raise RuntimeError("THREADS_USER_ID 미설정 (.env)")
    return uid


class ThreadsAPIError(RuntimeError):
    """Threads API 오류 — 메시지에 토큰이 들어가지 않게 만든 전용 예외.

    requests의 HTTPError는 예외 문구에 요청 URL을 통째로 넣는데, 우리는 토큰을
    쿼리스트링으로 보내므로 그게 로그 파일에 평문으로 남는다. 실제로 2026-08-04에
    댓글 수집이 실패하면서 액세스 토큰이 로그에 46번 찍혔다. 그래서 상태코드와
    Meta가 준 error.message만 남기고 URL은 버린다.
    """


def _check(r):
    """응답 검증. 실패하면 토큰이 없는 예외로 바꿔 던진다."""
    if r.ok:
        return r
    msg = ""
    try:
        msg = ((r.json().get("error") or {}).get("message")) or ""
    except Exception:
        pass
    raise ThreadsAPIError(f"HTTP {r.status_code} {msg or r.reason}".strip())


def _get_paged(url, params, max_pages=10):
    """커서 페이징을 따라가며 data를 모두 모은다.

    Threads는 limit를 줘도 한 번에 다 주지 않고 paging.next를 준다. 한 페이지만
    읽으면 인기 게시물의 뒤쪽 댓글이 조용히 누락되므로 next를 따라간다.
    max_pages는 무한 루프 방지용 안전장치.
    """
    out = []
    for _ in range(max_pages):
        r = requests.get(url, params=params, timeout=20)
        _check(r)
        body = r.json()
        out.extend(body.get("data") or [])
        nxt = (body.get("paging") or {}).get("next")
        if not nxt:
            break
        url, params = nxt, None   # next URL에 토큰·커서가 이미 들어 있다
    return out


def fetch_user_id():
    """토큰으로 내 계정 id·username 조회 (최초 설정 시 1회)."""
    r = requests.get(f"{BASE}/me", timeout=15,
                     params={"fields": "id,username", "access_token": _token()})
    _check(r)
    return r.json()


def post_text(text, reply_to_id=None, image_url=None):
    """스레드 1건 게시. 성공 시 게시물 id 반환.

    reply_to_id를 주면 그 게시물/댓글에 달리는 답글이 된다.
    image_url을 주면 이미지 게시물이 된다 — Meta 서버가 그 URL을 직접 가져가므로
    반드시 외부에서 공개 접근이 되어야 한다(우리는 elsrader.site에서 서빙).
    이미지 컨테이너는 처리에 시간이 걸려 발행 타임아웃을 넉넉히 준다.
    """
    uid, tok = _uid(), _token()

    data = {"media_type": "TEXT", "text": text, "access_token": tok}
    if image_url:
        data["media_type"] = "IMAGE"
        data["image_url"] = image_url
    if reply_to_id:
        data["reply_to_id"] = reply_to_id
    r = requests.post(f"{BASE}/{uid}/threads", timeout=30, data=data)
    _check(r)
    creation_id = r.json()["id"]

    if image_url:
        # 이미지 컨테이너는 Meta가 원본을 내려받아 처리할 시간이 필요하다.
        # 곧바로 발행하면 아직 준비 안 됐다는 오류가 난다(공식 권장 30초).
        time.sleep(30)

    r = requests.post(f"{BASE}/{uid}/threads_publish", timeout=30,
                      data={"creation_id": creation_id, "access_token": tok})
    _check(r)
    return r.json().get("id", "")


def post_reply(text, reply_to_id):
    """특정 댓글에 답글 게시. 성공 시 답글 id 반환."""
    return post_text(text, reply_to_id=reply_to_id)


def fetch_my_posts(since_date, limit=50):
    """내 게시물 목록 (since_date 이후 발행분).

    since는 날짜 문자열을 받는다. 댓글은 원 게시물을 통해서만 조회할 수 있어
    "최근 N일치 게시물"을 먼저 확보해야 한다.
    """
    return _get_paged(f"{BASE}/{_uid()}/threads", {
        "fields": "id,permalink,timestamp",
        "since": since_date.isoformat(),
        "limit": limit,
        "access_token": _token(),
    })


def fetch_replies(media_id, limit=100):
    """게시물 1건에 달린 댓글 목록.

    is_reply_owned_by_me는 우리 계정이 쓴 답글을 걸러내는 데 쓴다. 이게 없으면
    우리가 보낸 자동 답글을 다시 수집해 답글에 답글을 다는 사고가 난다.
    """
    return _get_paged(f"{BASE}/{media_id}/replies", {
        "fields": "id,text,username,timestamp,permalink,is_reply_owned_by_me",
        "limit": limit,
        "access_token": _token(),
    })


def refresh_token():
    """장기 토큰 갱신(만료 24시간 전~60일 사이 언제든 가능). 새 토큰 반환."""
    r = requests.get(f"{BASE.replace('/v1.0', '')}/refresh_access_token", timeout=15,
                     params={"grant_type": "th_refresh_token", "access_token": _token()})
    _check(r)
    return r.json()   # {access_token, token_type, expires_in}
