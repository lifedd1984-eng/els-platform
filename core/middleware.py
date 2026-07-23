"""가벼운 자체 접속 집계 — GET·HTML·200만 기록, 봇 제외.

외부 분석 스크립트 없이 서버에서 직접 집계한다.
개인 식별이 불가능하도록 (ip+ua+일자) 해시만 저장하고 원본 IP는 남기지 않는다.
"""

import hashlib
import random
from datetime import date, timedelta
from urllib.parse import urlparse

_BOT_UA = ("bot", "spider", "crawl", "curl", "wget", "python-requests",
           "monitor", "uptime", "headless", "preview", "scan")
_SKIP_PREFIX = ("/static/", "/admin/", "/health", "/favicon", "/robots",
                "/media/", "/stats")


class PageViewMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._record(request, response)
        except Exception:
            pass          # 집계 실패가 서비스 응답에 영향을 주면 안 됨
        return response

    def _record(self, request, response):
        if request.method != "GET" or response.status_code != 200:
            return
        path = request.path
        if path.startswith(_SKIP_PREFIX):
            return
        if "text/html" not in response.get("Content-Type", ""):
            return
        ua = request.META.get("HTTP_USER_AGENT", "")[:300].lower()
        if not ua or any(b in ua for b in _BOT_UA):
            return

        from .models import PageView

        today = date.today()
        ip = (request.META.get("HTTP_CF_CONNECTING_IP")
              or request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
              or request.META.get("REMOTE_ADDR", ""))
        visitor = hashlib.md5(f"{ip}|{ua}|{today}".encode()).hexdigest()[:16]

        ref = ""
        referer = request.META.get("HTTP_REFERER", "")
        if referer:
            host = urlparse(referer).netloc[:120]
            if host and "elsrader" not in host and "localhost" not in host:
                ref = host

        PageView.objects.create(
            date=today, path=path[:120], ref=ref, visitor=visitor,
            is_auth=request.user.is_authenticated)

        if random.random() < 0.001:   # 요청 1000건당 1회꼴로 오래된 기록 정리
            PageView.objects.filter(date__lt=today - timedelta(days=180)).delete()
