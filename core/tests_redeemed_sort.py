"""포트폴리오 '상환 완료' 목록 정렬.

'보유중' 목록에만 있던 정렬을 '상환 완료' 목록에도 붙이면서 고정하는 것은 셋이다.
  ① 정렬 파라미터가 없을 때의 순서는 예전 그대로다(등록 최신순 = -created_at).
     정렬 기능을 붙였다고 첫 화면 순서가 바뀌면 안 된다.
  ② 상환금액·실현수익률·상환일이 비어 있는 행이 섞여도 500이 나지 않는다.
     상태만 바꾸고 상환금을 넣지 않은 기록이 실제로 있다.
  ③ 실현수익률의 결측 대체값은 -1이 아니다 — 음수(-40% 등)가 정상 값이라
     -1을 쓰면 미입력 행이 손실 행과 이익 행 사이에 끼어든다.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Investment, Product


class _Base(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="admin", password="x")
        # 등록 순서 A → B → C → D. 기본 화면은 등록 최신순이라 D, C, B, A로 나온다.
        self.a = self._inv("미래에셋증권", "101", 10_000_000, 10_500_000,
                           date(2026, 3, 10), "조기상환")    # +5.0%
        self.b = self._inv("삼성증권", "202", 20_000_000, 12_000_000,
                           date(2026, 1, 5), "낙인후상환")   # -40.0%
        self.c = self._inv("키움증권", "303", 5_000_000, 5_600_000,
                           date(2026, 5, 20), "만기상환")    # +12.0%
        # 상환금·상환일이 비어 있는 건 — 상태만 바꾸고 금액을 넣지 않은 기록
        self.d = self._inv("NH투자증권", "404", 50_000_000, None, None, "조기상환")
        self.client.force_login(self.user)

    def _inv(self, issuer, no, amount, redeemed_amount, redeemed_at, status):
        p = Product.objects.create(issuer=issuer, product_no=no)
        return Investment.objects.create(
            user=self.user, product=p, amount=amount, invested_at=date(2025, 6, 1),
            status=status, redeemed_amount=redeemed_amount, redeemed_at=redeemed_at)

    def _order(self, **params):
        """정렬 결과를 상품번호 리스트로 — 화면에 보이는 순서 그대로다."""
        resp = self.client.get("/portfolio/", params)
        self.assertEqual(resp.status_code, 200)
        return [i.product.product_no for i in resp.context["d_page"]]


class 기본순서(_Base):
    def test_정렬_파라미터가_없으면_등록_최신순_그대로다(self):
        self.assertEqual(self._order(), ["404", "303", "202", "101"])

    def test_모르는_컬럼을_넣어도_기본순서로_돌아간다(self):
        self.assertEqual(self._order(dsort="없는컬럼"), ["404", "303", "202", "101"])

    def test_기본화면에서는_어느_컬럼도_활성_상태가_아니다(self):
        resp = self.client.get("/portfolio/")
        self.assertEqual([c["label"] for c in resp.context["d_cols"] if c["active"]], [])


class 컬럼별정렬(_Base):
    def test_상품_발행사_가나다순(self):
        self.assertEqual(self._order(dsort="issuer"), ["404", "101", "202", "303"])
        self.assertEqual(self._order(dsort="issuer", ddir="desc"),
                         ["303", "202", "101", "404"])

    def test_투자금액(self):
        self.assertEqual(self._order(dsort="amount"), ["303", "101", "202", "404"])
        self.assertEqual(self._order(dsort="amount", ddir="desc"),
                         ["404", "202", "101", "303"])

    def test_상환금액_미입력은_맨_아래로_모인다(self):
        # asc면 맨 앞(가장 작은 값 취급), desc면 맨 뒤 — 결측이 중간에 끼지 않는다
        self.assertEqual(self._order(dsort="redeemed"), ["404", "303", "101", "202"])
        self.assertEqual(self._order(dsort="redeemed", ddir="desc"),
                         ["202", "101", "303", "404"])

    def test_실현수익률_미입력이_손실행보다_아래다(self):
        # 202가 -40%다. 결측 대체값이 -1이었다면 404가 202와 101 사이에 끼었다.
        self.assertEqual(self._order(dsort="realized"), ["404", "202", "101", "303"])
        self.assertEqual(self._order(dsort="realized", ddir="desc"),
                         ["303", "101", "202", "404"])

    def test_상환일_미입력은_항상_마지막이다(self):
        self.assertEqual(self._order(dsort="redeemed_at"), ["202", "101", "303", "404"])
        # desc에서는 date.max가 가장 커서 맨 앞으로 온다
        self.assertEqual(self._order(dsort="redeemed_at", ddir="desc"),
                         ["404", "303", "101", "202"])

    def test_상태는_정렬_대상이_아니다(self):
        resp = self.client.get("/portfolio/")
        상태 = [c for c in resp.context["d_cols"] if c["label"] == "상태"][0]
        self.assertEqual(상태["url"], "")
        self.assertFalse(상태["active"])


class 결측값전부(_Base):
    """상환금·상환일이 하나도 없는 계정에서도 모든 컬럼이 터지지 않는다."""

    def setUp(self):
        super().setUp()
        Investment.objects.filter(user=self.user).update(
            redeemed_amount=None, redeemed_at=None)

    def test_모든_컬럼_정렬이_200이다(self):
        for key in ("issuer", "amount", "redeemed", "realized", "redeemed_at"):
            for d in ("asc", "desc"):
                resp = self.client.get("/portfolio/", {"dsort": key, "ddir": d})
                self.assertEqual(resp.status_code, 200, f"{key}/{d}에서 깨졌다")
                self.assertEqual(len(resp.context["d_page"].object_list), 4)


class 헤더렌더(_Base):
    """href의 &는 템플릿에서 &amp;로 나간다(정상 HTML) — 보유중 표와 동일하다."""

    def setUp(self):
        super().setUp()
        # 보유중 표는 건이 있을 때만 그려진다 — 두 표의 정렬 링크를 같이 보려면 필요
        self._inv("한국투자증권", "505", 7_000_000, None, None, "보유중")

    def test_정렬_링크와_캐럿이_실제로_그려진다(self):
        html = self.client.get("/portfolio/", {"dsort": "realized",
                                               "ddir": "desc"}).content.decode()
        # 활성 컬럼: 파란 글씨 + 아래 캐럿, 링크는 반대 방향(asc)으로
        self.assertIn(
            'href="?dsort=realized&amp;ddir=asc&amp;hsort=next&amp;hdir=asc&amp;psize=10"'
            ' style="color:inherit;text-decoration:none;'
            'color:var(--blue-dark);font-weight:700"', html)
        self.assertIn('<i class="fa-solid fa-caret-down"></i>', html)
        # 비활성 컬럼: 회색 정렬 아이콘
        self.assertIn(
            'href="?dsort=redeemed_at&amp;ddir=asc&amp;hsort=next&amp;hdir=asc&amp;psize=10"',
            html)
        self.assertIn('<i class="fa-solid fa-sort" style="color:var(--border)"></i>', html)
        self.assertIn("실현수익률", html)

    def test_활성컬럼_링크는_방향을_뒤집는다(self):
        html = self.client.get("/portfolio/", {"dsort": "amount"}).content.decode()
        self.assertIn(
            'href="?dsort=amount&amp;ddir=desc&amp;hsort=next&amp;hdir=asc&amp;psize=10"',
            html)
        self.assertIn('<i class="fa-solid fa-caret-up"></i>', html)

    def test_보유중_정렬이_상환완료_링크에_실려_유지된다(self):
        """아래 표를 정렬했다고 위 표 정렬이 풀리면 화면이 통째로 바뀐다."""
        html = self.client.get("/portfolio/", {"hsort": "amount",
                                               "hdir": "desc"}).content.decode()
        self.assertIn("?dsort=amount&amp;ddir=asc&amp;hsort=amount&amp;hdir=desc&amp;psize=10",
                      html)

    def test_상환완료_정렬이_보유중_링크에_실려_유지된다(self):
        html = self.client.get("/portfolio/", {"dsort": "issuer",
                                               "ddir": "desc"}).content.decode()
        self.assertIn("?hsort=amount&amp;hdir=asc&amp;dsort=issuer&amp;ddir=desc&amp;psize=10",
                      html)

    def test_상환완료가_기본순서면_보유중_링크는_예전_그대로다(self):
        html = self.client.get("/portfolio/").content.decode()
        self.assertIn('href="?hsort=amount&amp;hdir=asc&amp;psize=10"', html)
