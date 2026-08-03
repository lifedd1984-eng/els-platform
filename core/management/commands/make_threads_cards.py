"""스레드 게시용 데이터 카드 생성 (로컬 전용).

왜 이 형식인가
  스톡 사진을 붙이면 어느 계정에나 있는 이미지가 되고, 한국 스레드가 싫어하는
  '인스타 복붙' 인상을 준다. 우리 글의 힘은 남이 못 가진 숫자에서 나오니
  그 숫자 자체를 이미지로 만든다. 하단에 로고·주소를 넣어 이미지만 퍼져도
  출처가 남게 한다.

  근거 박스를 일부러 넣는다. 금융 이미지에서 숫자만 크게 띄우면 리딩방과
  구분이 안 되는데, 출처가 같이 있으면 반대로 신뢰가 된다.

어느 편에 붙이나
  숫자가 주인공인 9편만. 공감형(2·4·8·11·15·18·21·25·28편)에는 절대 붙이지
  않는다 — "계좌 앱 안 연 지 며칠 됐다"에 데이터 카드가 붙으면 감정이 죽고
  광고로 읽힌다. 서비스 소개(13·14·24편)는 만든 이미지보다 실제 화면 캡처가
  설득력 있어 따로 준비한다.

로컬 전용
  한글 폰트(맑은 고딕)가 필요해 EC2에서는 돌지 않는다. 여기서 만들어
  core/assets/threads/에 커밋하면 배포로 따라간다.

사용:
  python manage.py make_threads_cards            # 전부 생성
  python manage.py make_threads_cards --day 5    # 한 장만
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

SS = 2                      # 슈퍼샘플링 (PIL은 자체 안티앨리어싱이 없다)
W = 1080 * SS

INK = (20, 23, 28)
INK2 = (85, 96, 110)
INK3 = (150, 160, 172)
PAPER = (251, 252, 253)
BLUE = (49, 130, 246)
BLUE_D = (45, 91, 255)
RULE = (226, 231, 236)
AMBER = (232, 150, 60)
WARN = (200, 78, 58)
GREEN = (14, 124, 102)

BD = "C:/Windows/Fonts/malgunbd.ttf"
RG = "C:/Windows/Fonts/malgun.ttf"

OUT_DIR = Path(settings.BASE_DIR) / "core" / "assets" / "threads"


def _f(path, size):
    from PIL import ImageFont
    return ImageFont.truetype(path, size * SS)


def _radar(d, cx, cy, r, color=BLUE, w=None):
    """사이트 로고와 같은 도형 — 링 + 스윕 + 포착 신호."""
    w = w or int(r * 0.26)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color + (60,), width=w)
    d.arc([cx - r, cy - r, cx + r, cy + r], 210, 30, fill=color, width=w)
    hx, hy = cx + r * 0.866, cy + r * 0.5
    hr = r * 0.30
    d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=color)


def _footer(d):
    y = W - 118 * SS
    d.line([70 * SS, y, W - 70 * SS, y], fill=RULE, width=2 * SS)
    _radar(d, 92 * SS, y + 44 * SS, 20 * SS, BLUE, 6 * SS)
    d.text((124 * SS, y + 26 * SS), "ELS 레이더", font=_f(BD, 25), fill=INK)
    d.text((124 * SS, y + 58 * SS), "elsrader.site", font=_f(RG, 21), fill=INK3)


def _base():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, W), PAPER)
    return img, ImageDraw.Draw(img)


def _save(img, day):
    from PIL import Image
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"card_{day:02d}.png"
    img.resize((1080, 1080), Image.LANCZOS).save(path, quality=95)
    return path


# ── A형: 숫자 하나로 승부 ────────────────────────────────
def card_number(eyebrow, number, unit, line1, line2, note):
    img, d = _base()
    x = 70 * SS
    d.text((x, 96 * SS), eyebrow, font=_f(BD, 25), fill=BLUE)

    fn, fu = _f(BD, 176), _f(BD, 70)
    d.text((x, 168 * SS), number, font=fn, fill=INK)
    d.text((x + d.textlength(number, font=fn) + 10 * SS, 268 * SS),
           unit, font=fu, fill=INK)

    d.text((x, 420 * SS), line1, font=_f(BD, 42), fill=INK)
    d.text((x, 484 * SS), line2, font=_f(RG, 34), fill=INK2)

    by = 620 * SS
    d.rounded_rectangle([x, by, W - x, by + 150 * SS], radius=14 * SS,
                        fill=(240, 245, 252))
    d.text((x + 34 * SS, by + 34 * SS), "근거", font=_f(BD, 24), fill=BLUE)
    d.text((x + 34 * SS, by + 76 * SS), note, font=_f(RG, 30), fill=INK2)

    _footer(d)
    return img


# ── B형: 여러 값 비교 막대 ───────────────────────────────
def card_compare(eyebrow, title, rows, note, suffix="%"):
    img, d = _base()
    x = 70 * SS
    d.text((x, 96 * SS), eyebrow, font=_f(BD, 25), fill=BLUE)
    d.text((x, 152 * SS), title, font=_f(BD, 52), fill=INK)

    n = len(rows)
    top = 288 * SS
    gap = (148 if n <= 3 else 118) * SS
    bar_w = W - x * 2
    mx = max(r[1] for r in rows) or 1
    for i, (label, val, color) in enumerate(rows):
        y = top + gap * i
        d.text((x, y), label, font=_f(RG, 32), fill=INK2)
        vs = f"{val:g}{suffix}"
        fv = _f(BD, 46 if n <= 3 else 40)
        d.text((W - x - d.textlength(vs, font=fv), y - 8 * SS), vs, font=fv, fill=color)
        by = y + (58 if n <= 3 else 52) * SS
        h = (26 if n <= 3 else 22) * SS
        d.rounded_rectangle([x, by, x + bar_w, by + h], radius=h // 2, fill=(233, 238, 244))
        fill_w = max(h, int(bar_w * (val / mx)))
        d.rounded_rectangle([x, by, x + fill_w, by + h], radius=h // 2, fill=color)

    d.text((x, top + gap * n + 24 * SS), note, font=_f(RG, 29), fill=INK3)
    _footer(d)
    return img


# ── 편별 정의 ────────────────────────────────────────────
def build(day):
    if day == 3:
        return card_compare(
            "조기상환 20만 건 · 2016~2025", "ELS는 3년 상품이 아니다",
            [("6개월 안에 끝남", 63.9, BLUE),
             ("1년 안에 끝남 (누적)", 87.5, BLUE_D)],
            "설명서에 적힌 만기와 실제 경험이 다른 이유")

    if day == 5:
        return card_number(
            "낙인을 터치한 상품 2만 5천 건 중", "87.5", "%",
            "원금을 돌려받았다",
            "낙인은 손실의 필요조건이지 충분조건이 아니다",
            "2016~2025 발행 공모 ELS 68,496건 전수 · SEIBro")

    if day == 7:
        return card_compare(
            "10년간 발행된 ELS의 기초자산", "1위는 코스피가 아니다",
            [("유로스톡스50", 9.9, BLUE),
             ("S&P500", 8.7, BLUE_D),
             ("코스피200", 6.6, INK3)],
            "단위: 만 건 · 내 상품이 유럽 지수에 걸린 걸 모르는 사람이 많다",
            suffix="만")

    if day == 12:
        return card_compare(
            "2021년 발행 ELS · 분기별 원금손실률", "3개월 차이로 7배가 갈렸다",
            [("1분기", 28.9, AMBER), ("2분기", 49.2, WARN),
             ("3분기", 21.1, AMBER), ("4분기", 7.0, BLUE)],
            "10년치 손실 3,215건 중 75%가 2021년 발행분 하나에서 나왔다")

    if day == 16:
        return card_compare(
            "종목형 ELS · 기초자산별 원금손실률", "가장 많이 터진 건 조선주였다",
            [("국내 조선사 (549건)", 89.1, WARN),
             ("테슬라 (4,086건)", 1.9, BLUE)],
            "무서워 보이는 자산이 실제로는 훨씬 안전했다 · SEIBro 공식 집계")

    if day == 19:
        return card_compare(
            "종목형 ELS · 10년 실측", "첫 조기상환 조건이 위험을 45배 가른다",
            [("첫 조건 79% 이하", 0.64, BLUE),
             ("85~89%", 8.48, AMBER),
             ("95% 이상", 28.67, WARN)],
            "실제 원금손실로 끝난 비율 · 설명서 앞장에서 무료로 확인 가능")

    if day == 20:
        return card_compare(
            "지수형 ELS · 발행 시점별 원금손실률", "손실은 고점에서 발행된 상품에 몰렸다",
            [("고점 대비 70~85%", 0.0, BLUE),
             ("85~95%", 0.37, AMBER),
             ("95% 이상 (고점권)", 5.33, WARN)],
            "그런데 ELS가 가장 많이 팔리는 때가 바로 그때다")

    if day == 22:
        return card_compare(
            "상환된 ELS의 원금손실 비율", "가장 무서웠던 직후가 가장 조용했다",
            [("2024년 (1.8만 건)", 16.28, WARN),
             ("2025년 (1.3만 건)", 0.37, BLUE)],
            "2025년은 2015년 이후 가장 낮은 수치 · SEIBro 공식 집계")

    if day == 23:
        return card_number(
            "2026년 7월 발행 지수형 ELS 평균 쿠폰", "18.4", "%",
            "10년 중 가장 좋은 조건",
            "역대 최고였던 연 7.4%(2025년)의 2.5배",
            "다만 코스피200 변동성이 2010년 이후 사상 최고다")

    return None


DAYS = [3, 5, 7, 12, 16, 19, 20, 22, 23]


class Command(BaseCommand):
    help = "스레드 데이터 카드 생성 (로컬 전용 — 한글 폰트 필요)"

    def add_arguments(self, parser):
        parser.add_argument("--day", type=int, default=0, help="한 편만 생성")

    def handle(self, *args, **opts):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.stderr.write("Pillow가 필요합니다: pip install pillow")
            return
        if not Path(BD).exists():
            self.stderr.write(f"한글 폰트를 찾을 수 없습니다: {BD} (로컬 윈도우에서 실행)")
            return

        days = [opts["day"]] if opts["day"] else DAYS
        for d in days:
            img = build(d)
            if img is None:
                self.stderr.write(f"{d}편은 카드 정의가 없습니다")
                continue
            path = _save(img, d)
            self.stdout.write(f"  {d:>2}편 → {path.name}")
        self.stdout.write(self.style.SUCCESS(f"카드 {len(days)}장 생성"))
