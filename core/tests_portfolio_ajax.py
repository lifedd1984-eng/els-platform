"""포트폴리오 조작을 새로고침 없이 처리한다 (fetch + 화면 조각 교체).

배경 (2026-08-10 조 팀장 지시)
    상환처리·삭제·금액수정을 하면 페이지가 통째로 새로고침되면서 보고 있던
    정렬·페이지가 풀렸다. 정렬·페이지는 URL 쿼리스트링에 있는데, 처리 후
    리다이렉트가 그 쿼리스트링을 안 들고 갔기 때문이다.

여기서 고정하는 것
    ① fetch 요청(X-Requested-With)에는 페이지 전체 대신 content 블록만 JSON으로.
    ② 그 조각이 조작 결과를 이미 반영한 상태다 — 상환 처리한 건은 보유중에서
       빠지고 상환완료에 들어가 있어야 한다.
    ③ 요청 URL의 정렬·페이지 파라미터가 그대로 먹는다 — 이게 이번 작업의 목적이다.
    ④ 일반 폼 제출(JS 없음)은 예전 그대로 302 리다이렉트다. 돈이 걸린 화면이라
       조작이 조용히 실패하면 안 된다.
"""

import json
from datetime import date, timedelta
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Investment, Product, RedemptionVerdict

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

# 화면에 나오는 순서 — 조각을 잘라내는 기준이다
SECTIONS = ["pf-notice", "pf-stats", "pf-analysis", "pf-kialert", "pf-add", "pf-pending",
            "pf-holding", "pf-missed", "pf-done"]


def section(html, name):
    """렌더된 html에서 조각 하나만 잘라낸다."""
    i = html.index('id="%s"' % name)
    k = SECTIONS.index(name) + 1
    j = html.index('id="%s"' % SECTIONS[k]) if k < len(SECTIONS) else len(html)
    return html[i:j]


class _Base(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="admin", password="x")
        self.h1 = self._inv("101", 10_000_000, "보유중")
        self.h2 = self._inv("102", 30_000_000, "보유중")
        self.d1 = self._inv("201", 5_000_000, "조기상환",
                            redeemed_amount=5_300_000, redeemed_at=date(2026, 5, 1))
        self.client.force_login(self.user)

    def _inv(self, no, amount, status, **kw):
        p = Product.objects.create(
            issuer="키움증권", product_no=no, yield_rate=10.0,
            barriers_raw=[90, 85, 80], period_months=3,
            issue_date=date(2026, 1, 2), ki=45)
        return Investment.objects.create(
            user=self.user, product=p, amount=amount,
            invested_at=date(2026, 1, 2), status=status, **kw)

    def _post(self, data, qs=""):
        resp = self.client.post("/portfolio/" + qs, data, **AJAX)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        return json.loads(resp.content.decode())


class 응답형식(_Base):
    def test_fetch에는_JSON으로_조각만_준다(self):
        data = self._post({"action": "edit", "id": self.h1.id, "amount": "11,000,000"})
        self.assertIn("html", data)
        self.assertIn("message", data)
        # 네비·헤드가 없는 content 블록만
        self.assertNotIn("<!DOCTYPE", data["html"])
        self.assertNotIn("<nav", data["html"])
        for name in SECTIONS:
            self.assertIn('id="%s"' % name, data["html"], f"{name} 조각이 없다")

    def test_일반_폼_제출은_예전처럼_리다이렉트다(self):
        """JS가 없을 때의 경로 — 여기가 깨지면 조작 자체가 안 된다."""
        resp = self.client.post("/portfolio/", {
            "action": "edit", "id": self.h1.id, "amount": "11,000,000"})
        self.assertEqual(resp.status_code, 302)
        self.h1.refresh_from_db()
        self.assertEqual(self.h1.amount, 11_000_000)

    def test_폴백_리다이렉트는_정렬_주소로_돌아간다(self):
        """next에 지금 주소가 들어 있어 폼 제출로 떨어져도 화면이 유지된다."""
        resp = self.client.post("/portfolio/", {
            "action": "delete", "id": self.d1.id,
            "next": "/portfolio/?dsort=amount&ddir=desc&psize=20"})
        self.assertEqual(resp["Location"], "/portfolio/?dsort=amount&ddir=desc&psize=20")

    def test_CSRF_토큰은_폼에_들어있는_그대로_통과한다(self):
        """fetch는 FormData를 그대로 보낸다 — 토큰을 따로 챙기지 않는다."""
        c = self.client_class(enforce_csrf_checks=True)
        c.force_login(self.user)
        html = c.get("/portfolio/").content.decode()
        i = html.index('name="csrfmiddlewaretoken" value="') + len(
            'name="csrfmiddlewaretoken" value="')
        token = html[i:html.index('"', i)]
        resp = c.post("/portfolio/", {
            "action": "edit", "id": self.h1.id, "amount": "11,000,000",
            "csrfmiddlewaretoken": token}, **AJAX)
        self.assertEqual(resp.status_code, 200)
        self.h1.refresh_from_db()
        self.assertEqual(self.h1.amount, 11_000_000)
        # 토큰이 없으면 조작이 일어나지 않는다. 응답은 JSON이 아니라 홈으로 가는
        # 리다이렉트다(settings.CSRF_FAILURE_VIEW — 로그인 상태면 홈으로 보낸다).
        # JS는 JSON이 아닌 응답을 보면 그 주소로 화면을 넘긴다.
        resp = c.post("/portfolio/", {
            "action": "edit", "id": self.h1.id, "amount": "12,000,000"}, **AJAX)
        self.assertNotEqual(resp.status_code, 200)
        self.h1.refresh_from_db()
        self.assertEqual(self.h1.amount, 11_000_000)

    def test_메시지는_JSON으로_주고_세션에_남기지_않는다(self):
        """남겨 두면 다음 새로고침에 지난 메시지가 또 뜬다."""
        data = self._post({"action": "delete", "id": self.d1.id})
        self.assertIn("삭제", data["message"])
        html = self.client.get("/portfolio/").content.decode()
        self.assertNotIn("삭제했습니다", html)


class 상환처리(_Base):
    def test_처리한_건이_보유중에서_빠지고_상환완료로_간다(self):
        data = self._post({"action": "redeem", "id": self.h1.id, "status": "조기상환",
                           "redeemed_at": "2026-08-10", "redeemed_amount": "10,500,000"})
        html = data["html"]
        self.assertNotIn("101", section(html, "pf-holding"))
        self.assertIn("101", section(html, "pf-done"))
        self.assertIn("10,500,000", section(html, "pf-done"))
        self.assertIn("보유 중 1건", section(html, "pf-holding"))
        self.assertIn("상환 완료 2건", section(html, "pf-done"))

    def test_상단_통계도_같이_바뀐다(self):
        """총 투자금액은 보유분 합계라 상환 처리 즉시 줄어야 한다."""
        before = section(self.client.get("/portfolio/").content.decode(), "pf-stats")
        self.assertIn("40,000,000", before)
        data = self._post({"action": "redeem", "id": self.h1.id, "status": "조기상환",
                           "redeemed_at": "2026-08-10", "redeemed_amount": "10,500,000"})
        self.assertIn("30,000,000", section(data["html"], "pf-stats"))

    def test_확정대기_카드의_상환_확정도_같은_경로다(self):
        RedemptionVerdict.objects.create(
            investment=self.h2, round_no=1, eval_date=date.today() - timedelta(days=3),
            barrier=90.0, worst_level=95.0, met=True)
        html = self.client.get("/portfolio/").content.decode()
        self.assertIn("상환 확정 대기 1건", section(html, "pf-pending"))
        data = self._post({"action": "redeem", "id": self.h2.id, "status": "조기상환",
                           "redeemed_at": "2026-08-10", "redeemed_amount": "31,000,000"})
        self.assertNotIn("상환 확정 대기", section(data["html"], "pf-pending"))
        self.assertIn("102", section(data["html"], "pf-done"))


class 삭제(_Base):
    def test_삭제한_행이_사라진다(self):
        data = self._post({"action": "delete", "id": self.d1.id})
        self.assertNotIn("201", section(data["html"], "pf-done"))
        self.assertFalse(Investment.objects.filter(pk=self.d1.pk).exists())

    def test_선택삭제도_같은_경로다(self):
        data = self._post({"action": "bulk_delete",
                           "ids": [str(self.h1.id), str(self.h2.id)]})
        self.assertIn("보유 중 0건", section(data["html"], "pf-holding"))
        self.assertIn("2건", data["message"])


class 금액수정(_Base):
    def test_보유중_투자금액(self):
        data = self._post({"action": "edit", "id": self.h1.id, "amount": "12,345,000"})
        self.assertIn("12,345,000", section(data["html"], "pf-holding"))

    def test_상환완료_상환금액과_실현수익률(self):
        data = self._post({"action": "edit", "id": self.d1.id,
                           "redeemed_amount": "5,500,000"})
        done = section(data["html"], "pf-done")
        self.assertIn("5,500,000", done)
        self.assertIn("10.0%", done)          # (5,500,000-5,000,000)/5,000,000


class 판정무시(_Base):
    def setUp(self):
        super().setUp()
        RedemptionVerdict.objects.create(
            investment=self.h2, round_no=1, eval_date=date.today() - timedelta(days=3),
            barrier=90.0, worst_level=95.0, met=True)

    def test_무시하면_무시함_배지가_붙는다(self):
        data = self._post({"action": "dismiss_verdict", "id": self.h2.id, "round_no": 1})
        self.assertIn("무시함", section(data["html"], "pf-pending"))

    def test_무시해제도_같은_경로다(self):
        self._post({"action": "dismiss_verdict", "id": self.h2.id, "round_no": 1})
        data = self._post({"action": "undismiss_verdict", "id": self.h2.id, "round_no": 1})
        self.assertNotIn("무시함", section(data["html"], "pf-pending"))


class 등록(_Base):
    def test_새_투자가_보유중에_들어온다(self):
        p = Product.objects.create(issuer="삼성증권", product_no="909",
                                   issue_date=date(2026, 2, 3))
        data = self._post({"action": "add", "product_id": p.id, "amount": "7,000,000"})
        self.assertIn("909", section(data["html"], "pf-holding"))
        self.assertIn("보유 중 3건", section(data["html"], "pf-holding"))


class 정렬유지(_Base):
    """이번 작업의 목적 — 조작해도 보던 정렬·페이지가 그대로여야 한다."""

    def setUp(self):
        super().setUp()
        self.d2 = self._inv("202", 50_000_000, "만기상환",
                            redeemed_amount=52_000_000, redeemed_at=date(2026, 6, 1))

    def _order(self, html):
        done = section(html, "pf-done")
        return sorted(["201", "202"], key=lambda n: done.index(">키움증권 " + n))

    def test_조작_응답이_요청_URL의_정렬을_따른다(self):
        qs = "?dsort=amount&ddir=desc"
        data = self._post({"action": "edit", "id": self.h1.id, "amount": "9,000,000"}, qs)
        self.assertEqual(self._order(data["html"]), ["202", "201"])   # 5천만 → 5백만
        data = self._post({"action": "edit", "id": self.h1.id, "amount": "9,100,000"},
                          "?dsort=amount&ddir=asc")
        self.assertEqual(self._order(data["html"]), ["201", "202"])

    def test_정렬_헤더_링크도_그_상태로_다시_그려진다(self):
        data = self._post({"action": "edit", "id": self.h1.id, "amount": "9,000,000"},
                          "?dsort=amount&ddir=desc&psize=20")
        done = section(data["html"], "pf-done")
        self.assertIn("fa-caret-down", done)
        self.assertIn("psize=20", done)

    def test_페이지_파라미터도_그대로_먹는다(self):
        qs = "?psize=10&dsort=amount&ddir=asc&dpage=2"
        data = self._post({"action": "edit", "id": self.h1.id, "amount": "9,000,000"}, qs)
        # 2건뿐이라 2페이지는 없다 — Paginator가 마지막 페이지로 떨어뜨리고 500이 안 난다
        self.assertIn("201", section(data["html"], "pf-done"))


class _Forms(HTMLParser):
    """<form> 하나하나의 class와 그 안의 action 값을 모은다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []          # [(class, url, action값)]
        self._cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = [a.get("class", ""), a.get("action", ""), None]
            self.forms.append(self._cur)
        elif tag == "input" and a.get("name") == "action" and self._cur:
            self._cur[2] = a.get("value")

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


class _Divs(HTMLParser):
    """pf-* 래퍼가 서로 겹치지 않고 같은 깊이의 형제로 닫히는지 본다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.stack = []
        self.closed = {}         # 이름 -> 닫힌 시점의 깊이

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        self.depth += 1
        self.stack.append(dict(attrs).get("id"))

    def handle_endtag(self, tag):
        if tag != "div":
            return
        name = self.stack.pop()
        self.depth -= 1
        if name and name.startswith("pf-"):
            self.closed[name] = self.depth


class 화면구조(_Base):
    def setUp(self):
        super().setUp()
        RedemptionVerdict.objects.create(
            investment=self.h2, round_no=1, eval_date=date.today() - timedelta(days=3),
            barrier=90.0, worst_level=95.0, met=True)
        self.html = self.client.get("/portfolio/").content.decode()

    def test_이_화면의_조작_폼에는_전부_pf_form이_붙어_있다(self):
        """빠지면 그 조작만 조용히 전체 새로고침으로 돌아간다.

        기준은 action 속성이 비어 있는 폼 — 지금 주소로 그대로 보내는 폼이
        이 화면의 조작이다. 주소를 따로 적은 폼은 아래 테스트에서 가른다.
        """
        p = _Forms()
        p.feed(self.html)
        own = [f for f in p.forms if not f[1]]
        self.assertEqual(
            sorted({f[2] for f in own}),
            ["add", "bulk_delete", "delete", "dismiss_verdict", "edit", "redeem"])
        for cls, _url, act in own:
            self.assertIn("pf-form", cls, f"{act} 폼에 pf-form이 없다")

    def test_주소를_따로_가진_폼은_잡지_않는다(self):
        """엑셀 업로드는 다른 뷰로 가고, base.html의 공용 투자등록 모달은 이
        화면에서 열리는 곳이 없다(상품상세·관심목록·주간에서만 연다).
        가로채면 조각이 없는 다른 화면에서 깨진다."""
        p = _Forms()
        p.feed(self.html)
        outside = [f for f in p.forms if f[1]]
        self.assertTrue(outside)
        for cls, url, _act in outside:
            self.assertNotIn("pf-form", cls, f"{url} 폼을 잡으면 안 된다")

    def test_조각_래퍼가_형제로_닫힌다(self):
        """겹치거나 어긋나면 JS가 엉뚱한 자리를 갈아끼운다."""
        p = _Divs()
        p.feed(self.html)
        self.assertEqual(sorted(p.closed), sorted(SECTIONS))
        self.assertEqual(len(set(p.closed.values())), 1, p.closed)
