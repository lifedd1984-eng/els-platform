"""스레드 30일 원고를 하루 1편씩 순서대로 게시한다.

왜 별도 명령인가
  기존 post_threads는 주간 TOP5(발행사명 + 쿠폰 + 낙인)를 올렸는데,
  그 형식은 금융소비자보호법 제22조 제1항의 금융상품 광고에 해당한다
  (판매업자가 아닌 자는 금지, 1억원 이하 과태료). 그래서 스레드에서는
  상품 특정을 포기하고 사전 검수된 원고만 나가도록 통째로 갈아탄다.
  (2026-08-03)

상태 관리
  게시 이력은 logs/threads_posted.json에 남긴다. 모델을 쓰지 않는 이유는
  마이그레이션 없이 즉시 굴리기 위해서다. 30편짜리 한시 운영이라 이걸로 충분하다.

안전장치
  - 게시 직전 threads_content.check()로 금칙어·글자수 검사. 걸리면 중단한다.
  - 같은 편을 두 번 올리지 않는다(이력에 있으면 건너뜀).
  - 실패는 텔레그램으로 올린다. 조용한 실패는 30일 계획을 통째로 날린다.

사용:
  python manage.py post_threads_daily --dry-run   # 다음 편 미리보기
  python manage.py post_threads_daily             # 다음 편 게시
  python manage.py post_threads_daily --day 5     # 특정 편 지정
  python manage.py post_threads_daily --check-all # 30편 전체 금칙어 검사
"""

import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.threads_content import DRAFTS, card_url, check

STATE = Path(settings.BASE_DIR) / "logs" / "threads_posted.json"


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def next_draft(state):
    """아직 안 올린 것 중 가장 빠른 편."""
    for d in DRAFTS:
        if str(d["day"]) not in state:
            return d
    return None


class Command(BaseCommand):
    help = "스레드 30일 원고를 하루 1편씩 게시"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--day", type=int, default=0, help="특정 편 강제 게시")
        parser.add_argument("--check-all", action="store_true",
                            help="30편 전체 금칙어·글자수 검사만")

    def handle(self, *args, **opts):
        if opts["check_all"]:
            return self._check_all()

        state = load_state()
        if opts["day"]:
            draft = next((d for d in DRAFTS if d["day"] == opts["day"]), None)
            if not draft:
                self.stderr.write(f"{opts['day']}편이 없습니다 (1~{len(DRAFTS)})")
                return
        else:
            draft = next_draft(state)
            if not draft:
                self.stdout.write("30편 전부 게시 완료 — 다음 계획이 필요합니다")
                return

        problems = check(draft["text"], draft["day"])
        if problems:
            msg = f"[스레드] {draft['day']}편 게시 중단 — " + " / ".join(problems)
            self.stderr.write(self.style.ERROR(msg))
            self._notify(msg)
            return

        n = len(draft["text"])
        self.stdout.write(f"── {draft['day']}편 ({draft['type']}) · {n}자 ──")
        self.stdout.write(draft["text"])

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("\n[dry-run] 게시하지 않았습니다"))
            return

        if str(draft["day"]) in state:
            self.stdout.write(self.style.WARNING(
                f"\n{draft['day']}편은 이미 {state[str(draft['day'])]['posted_at']}에 "
                f"게시됐습니다 — 중복 방지로 중단"))
            return

        # 숫자가 주인공인 편에만 데이터 카드가 붙는다(threads_content.CARD_DAYS).
        # 카드가 없으면 image_url이 None이라 텍스트 게시로 나간다.
        img = card_url(draft["day"])
        if img:
            self.stdout.write(f"   첨부 이미지: {img}")

        from core.threads_api import post_text
        try:
            post_id = post_text(draft["text"], image_url=img)
        except Exception as e:
            msg = f"[스레드] {draft['day']}편 게시 실패 — {e}"
            self.stderr.write(self.style.ERROR(msg))
            self._notify(msg)
            raise SystemExit(1)

        state[str(draft["day"])] = {
            "post_id": post_id,
            "type": draft["type"],
            "posted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_state(state)

        done = len(state)
        self.stdout.write(self.style.SUCCESS(
            f"\n게시 완료 — {draft['day']}편 (id {post_id}) · 누적 {done}/{len(DRAFTS)}"))
        self._notify(f"[스레드] {draft['day']}편 게시 완료 ({done}/{len(DRAFTS)})\n\n"
                     f"{draft['text'][:120]}...")

    # ------------------------------------------------------------------
    def _check_all(self):
        bad = 0
        for d in DRAFTS:
            problems = check(d["text"], d["day"])
            n = len(d["text"])
            if problems:
                bad += 1
                self.stdout.write(self.style.ERROR(
                    f"  {d['day']:>2}편 ({d['type']}) {n:>3}자 — " + " / ".join(problems)))
            else:
                self.stdout.write(f"  {d['day']:>2}편 ({d['type']}) {n:>3}자  통과")
        types = {}
        for d in DRAFTS:
            types[d["type"]] = types.get(d["type"], 0) + 1
        self.stdout.write("")
        self.stdout.write(f"총 {len(DRAFTS)}편 / 문제 {bad}편")
        self.stdout.write("유형 배분: " + ", ".join(
            f"{k} {v}편" for k, v in sorted(types.items())))

    def _notify(self, text):
        try:
            from core.telegram import send_message
            send_message(text)
        except Exception:
            pass
