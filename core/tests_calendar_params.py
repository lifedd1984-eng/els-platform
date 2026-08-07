"""상환 캘린더 GET 파라미터 방어 테스트.

배경 (2026-08-07)
  주간 청약의 ?w를 고치며 인접 뷰의 같은 자리를 훑다가 나왔다. 실측 응답 코드:

    ?y=abc / ?m=abc      500  int() ValueError
    ?m=13 / ?m=0         500  calendar.monthdayscalendar의 IllegalMonthError
    ?y=0 / ?y=999999     500  date(year, month, day)의 범위 초과

  달 이동 링크가 y·m을 URL로 나르므로 ?w와 마찬가지로 주소창을 손대지 않아도
  걸릴 수 있는 자리다.

지키려는 것
  무효한 값에 500이 나지 않는다 — 조용히 이번 달로 돌아온다.
  정상적인 달 이동은 그대로 동작한다.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.views import MAX_YEAR, MIN_YEAR

TODAY = date.today()


class CalendarParamTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("calendar", password="x")
        self.client.force_login(self.user)

    def _get(self, **params):
        return self.client.get(reverse("calendar"), params)

    def test_파라미터가_없으면_이번_달이다(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.context["year"], r.context["month"]), (TODAY.year, TODAY.month))

    def test_숫자가_아니면_이번_달로_돌아온다(self):
        for bad in ("abc", "", "1.5", "1e3", " ", "None"):
            with self.subTest(bad=bad):
                r = self._get(y=bad, m=bad)
                self.assertEqual(r.status_code, 200)
                self.assertEqual((r.context["year"], r.context["month"]),
                                 (TODAY.year, TODAY.month))

    def test_달_범위를_벗어나면_이번_달로_돌아온다(self):
        # 예전엔 calendar.monthdayscalendar가 IllegalMonthError를 냈다
        for bad in ("0", "13", "-1", "99"):
            with self.subTest(m=bad):
                r = self._get(m=bad)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["month"], TODAY.month)

    def test_연도_범위를_벗어나면_이번_해로_돌아온다(self):
        for bad in ("0", "999999", "-2026", str(MAX_YEAR + 1), str(MIN_YEAR - 1)):
            with self.subTest(y=bad):
                r = self._get(y=bad)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["year"], TODAY.year)

    def test_정상적인_달_이동은_그대로_동작한다(self):
        r = self._get(y="2026", m="3")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.context["year"], r.context["month"]), (2026, 3))
        self.assertEqual((r.context["prev_y"], r.context["prev_m"]), (2026, 2))
        self.assertEqual((r.context["next_y"], r.context["next_m"]), (2026, 4))

    def test_해를_넘는_이동도_그대로다(self):
        r = self._get(y="2026", m="1")
        self.assertEqual((r.context["prev_y"], r.context["prev_m"]), (2025, 12))
        r = self._get(y="2026", m="12")
        self.assertEqual((r.context["next_y"], r.context["next_m"]), (2027, 1))

    def test_연도_경계에서도_이전_다음_링크가_깨지지_않는다(self):
        # 허용 연도를 date의 1~9999에서 한 해씩 물러선 이유 — year±1이 유효해야 한다
        for y, m in ((MIN_YEAR, 1), (MAX_YEAR, 12)):
            with self.subTest(y=y, m=m):
                r = self._get(y=str(y), m=str(m))
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context["year"], y)
                follow = self._get(y=str(r.context["prev_y"]), m=str(r.context["prev_m"]))
                self.assertEqual(follow.status_code, 200)
                follow = self._get(y=str(r.context["next_y"]), m=str(r.context["next_m"]))
                self.assertEqual(follow.status_code, 200)
