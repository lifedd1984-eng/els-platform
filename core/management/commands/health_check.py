"""
서버 헬스 체크: 디스크·메모리가 임계치를 넘으면 텔레그램 경보.

절세해(taxdown)와 같은 EC2에 동거 중이라 디스크가 차면 두 서비스가
같이 조용히 죽는다 (PC 시절 7/19 디스크 풀 사고 전례). 일일 배치에서
실행 — 정상이면 침묵, 위험할 때만 알림.
"""

import shutil

from django.core.management.base import BaseCommand

DISK_WARN_PCT = 80      # 루트 디스크 사용률(%) 이상이면 경보
MEM_WARN_MB = 200       # 가용 메모리(MB) 미만이면 경보


class Command(BaseCommand):
    help = "디스크·메모리 임계치 초과 시 텔레그램 경보 (정상이면 침묵)"

    def handle(self, *args, **opts):
        alerts = []

        du = shutil.disk_usage("/")
        used_pct = du.used / du.total * 100
        free_gb = du.free / 1024 ** 3
        if used_pct >= DISK_WARN_PCT:
            alerts.append(f"디스크 {used_pct:.0f}% 사용 (여유 {free_gb:.1f}GB)")

        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    meminfo[k] = int(v.strip().split()[0])  # kB
            avail_mb = meminfo.get("MemAvailable", 0) / 1024
            if avail_mb < MEM_WARN_MB:
                alerts.append(f"가용 메모리 {avail_mb:.0f}MB (임계 {MEM_WARN_MB}MB)")
        except OSError:
            pass  # 리눅스 외 환경(로컬 개발)에서는 메모리 체크 생략

        if alerts:
            from core import telegram
            telegram.send_message(
                "[서버 경보] ELS 레이더/절세해 동거 서버 자원 부족\n"
                + "\n".join(f"- {a}" for a in alerts)
                + "\n로그·캐시 정리 또는 디스크 확장이 필요합니다."
            )
            self.stdout.write(f"경보 발송: {alerts}")
        else:
            self.stdout.write(
                f"정상 — 디스크 {used_pct:.0f}% (여유 {free_gb:.1f}GB)")
