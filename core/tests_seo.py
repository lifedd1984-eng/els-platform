"""검색 유입 기본기 — 사이트맵 · robots.txt · 메타 태그 · 리포트 페이지.

배경 (2026-08-14)
  이용자 100명 모집을 시작했는데 지인 경로도, 커뮤니티 계정도 없다. 사람 손이
  안 들어가는 유입 경로는 검색뿐인데 그 입구가 막혀 있었다 —
  /sitemap.xml 404, meta description 없음, canonical 없음.

여기서 못 박는 것
  ① /sitemap.xml 이 200이고 유입을 만드는 공개 페이지를 담는다
  ② 로그인·운영자 전용 화면은 사이트맵에 절대 안 들어간다
  ③ / 와 /about/ 은 같은 HTML이므로 대표 주소를 / 하나로 모은다
  ④ 공개 페이지마다 description 이 서로 다르다 (전부 같으면 없느니만 못하다)
  ⑤ /report/els-10year/ 가 200이고 검증된 수치를 그대로 담는다
"""

from datetime import date, timedelta
from urllib.parse import urlsplit
from xml.etree import ElementTree

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Product
from core.sitemaps import PRODUCT_SITEMAP_DAYS

TODAY = date.today()
SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def make_product(**kw):
    base = dict(
        issuer="키움증권", product_no=str(Product.objects.count() + 5000),
        name="키움 ELS", product_type="ELS", yield_rate=12.0,
        ki=45, is_no_ki=False, barrier_first=85, barrier_last=65,
        assets_raw="KOSPI200 Index", asset_type="지수형",
        sub_end=TODAY, currency="KRW", loss_prob=1.2,
    )
    base.update(kw)
    return Product.objects.create(**base)


def sitemap_locs(client):
    """사이트맵 XML을 파싱해 <loc> 목록을 돌려준다."""
    resp = client.get("/sitemap.xml")
    root = ElementTree.fromstring(resp.content)
    return resp, [e.text for e in root.iter(f"{SM_NS}loc")]


class SitemapTests(TestCase):
    def test_sitemap_is_served_as_xml(self):
        resp = self.client.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("xml", resp["Content-Type"])

    def test_public_pages_are_listed(self):
        _, locs = sitemap_locs(self.client)
        paths = {loc.split("testserver")[-1] for loc in locs}
        for expected in ("/", "/weekly/", "/report/els-10year/", "/trend/",
                         "/disclaimer/", "/terms/", "/privacy/"):
            self.assertIn(expected, paths, f"{expected} 가 사이트맵에 없다")

    def test_login_only_and_admin_pages_are_never_listed(self):
        """로그인 벽 뒤 화면이 색인되면 유입이 전부 로그인 페이지에서 끊긴다."""
        make_product()
        _, locs = sitemap_locs(self.client)
        blob = "\n".join(locs)
        for forbidden in ("/portfolio/", "/watchlist/", "/calendar/", "/presets/",
                          "/ask/", "/accounts/", "/upload/", "/manage/",
                          "/stats/", "/search/", "/admin/"):
            self.assertNotIn(forbidden, blob, f"{forbidden} 가 사이트맵에 들어갔다")

    def test_about_is_not_listed_because_it_duplicates_home(self):
        """/about/ 는 / 와 같은 HTML이다. 둘 다 제출하면 중복 콘텐츠 자진 신고다."""
        _, locs = sitemap_locs(self.client)
        self.assertNotIn("/about/", "\n".join(locs))

    def test_absolute_https_urls(self):
        _, locs = sitemap_locs(self.client)
        self.assertTrue(all(loc.startswith("https://") for loc in locs), locs[:3])

    def test_recent_product_is_included(self):
        p = make_product(sub_end=TODAY)
        _, locs = sitemap_locs(self.client)
        self.assertIn(f"/product/{p.pk}/", "\n".join(locs))

    def test_stale_product_is_excluded(self):
        """청약이 끝난 지 오래된 상품은 읽을 사람이 없다 — 크롤링 예산만 먹는다."""
        old = make_product(sub_end=TODAY - timedelta(days=PRODUCT_SITEMAP_DAYS + 5))
        _, locs = sitemap_locs(self.client)
        self.assertNotIn(f"/product/{old.pk}/", "\n".join(locs))

    def test_principal_protected_product_is_excluded(self):
        """ELB·DLB는 목록 규칙(listed())상 화면에도 안 나온다 — 사이트맵도 같다."""
        elb = make_product(product_type="ELB")
        _, locs = sitemap_locs(self.client)
        self.assertNotIn(f"/product/{elb.pk}/", "\n".join(locs))

    def test_half_parsed_product_is_excluded(self):
        """수익률·낙인이 비면 화면에 보여줄 내용이 없다. 빈 페이지를 제출하지 않는다."""
        thin = make_product(yield_rate=None)
        no_ki_info = make_product(ki=None, is_no_ki=False)
        _, locs = sitemap_locs(self.client)
        blob = "\n".join(locs)
        self.assertNotIn(f"/product/{thin.pk}/", blob)
        self.assertNotIn(f"/product/{no_ki_info.pk}/", blob)

    def test_no_ki_product_is_included(self):
        """노낙인은 낙인값이 없는 게 정상이다 — 껍데기와 구분해야 한다."""
        p = make_product(ki=None, is_no_ki=True)
        _, locs = sitemap_locs(self.client)
        self.assertIn(f"/product/{p.pk}/", "\n".join(locs))

    def test_lastmod_present_for_report(self):
        resp = self.client.get("/sitemap.xml")
        root = ElementTree.fromstring(resp.content)
        for url in root.iter(f"{SM_NS}url"):
            if url.find(f"{SM_NS}loc").text.endswith("/report/els-10year/"):
                self.assertIsNotNone(url.find(f"{SM_NS}lastmod"))
                return
        self.fail("리포트 URL을 사이트맵에서 찾지 못했다")


class RobotsTxtTests(TestCase):
    def test_served_as_plain_text(self):
        resp = self.client.get("/robots.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/plain"))

    def test_points_at_the_sitemap(self):
        """이 한 줄이 없으면 검색엔진이 사이트맵을 스스로 찾지 못한다."""
        body = self.client.get("/robots.txt").content.decode()
        self.assertIn("Sitemap: http://testserver/sitemap.xml", body)

    def test_blocks_private_areas_but_allows_the_rest(self):
        body = self.client.get("/robots.txt").content.decode()
        self.assertIn("Allow: /", body)
        for path in ("/accounts/", "/portfolio/", "/watchlist/", "/calendar/",
                     "/presets/", "/ask/", "/manage/", "/upload/", "/stats/", "/search/"):
            self.assertIn(f"Disallow: {path}", body)


class MetaTagTests(TestCase):
    """공개 페이지마다 description·canonical·OG가 붙어 있는지."""

    PUBLIC_PAGES = ["/", "/about/", "/weekly/", "/trend/",
                    "/report/els-10year/", "/terms/", "/privacy/", "/disclaimer/"]

    def _head(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        html = resp.content.decode()
        return html[:html.index("</head>")]

    def test_every_public_page_has_description(self):
        for path in self.PUBLIC_PAGES:
            head = self._head(path)
            self.assertIn('<meta name="description" content="', head, path)
            self.assertIn('<meta property="og:description"', head, path)
            self.assertIn('<meta name="twitter:description"', head, path)

    def test_every_public_page_has_canonical(self):
        for path in self.PUBLIC_PAGES:
            self.assertIn('<link rel="canonical" href="http://testserver', self._head(path), path)

    def test_every_public_page_has_open_graph_and_twitter_card(self):
        for path in self.PUBLIC_PAGES:
            head = self._head(path)
            for tag in ('property="og:title"', 'property="og:type"', 'property="og:url"',
                        'property="og:image"', 'property="og:site_name"',
                        'property="og:locale"', 'name="twitter:card"',
                        'name="twitter:title"', 'name="twitter:image"'):
                self.assertIn(tag, head, f"{path} 에 {tag} 가 없다")

    def test_canonical_drops_the_query_string(self):
        """필터 조합마다 따로 색인되면 같은 화면이 수십 개로 갈라진다."""
        head = self._head("/weekly/?issuer=키움증권&sort=yield&page=2")
        self.assertIn('<link rel="canonical" href="http://testserver/weekly/">', head)

    def test_about_points_its_canonical_at_home(self):
        """/about/ 와 / 는 같은 HTML이다 — 대표 주소를 / 로 모은다."""
        self.assertIn('<link rel="canonical" href="http://testserver/">',
                      self._head("/about/"))

    def test_descriptions_differ_between_pages(self):
        """전부 같은 문장이면 검색 결과에서 페이지를 구분할 수 없다."""
        import re
        seen = {}
        for path in ("/", "/weekly/", "/trend/", "/report/els-10year/",
                     "/terms/", "/privacy/", "/disclaimer/"):
            m = re.search(r'<meta name="description" content="([^"]+)"', self._head(path))
            self.assertIsNotNone(m, path)
            seen[path] = m.group(1)
        self.assertEqual(len(set(seen.values())), len(seen), seen)

    def test_titles_differ_between_pages(self):
        import re
        titles = []
        for path in ("/", "/weekly/", "/trend/", "/report/els-10year/",
                     "/terms/", "/privacy/", "/disclaimer/"):
            m = re.search(r"<title>(.*?)</title>", self._head(path))
            titles.append(m.group(1))
        self.assertEqual(len(set(titles)), len(titles), titles)

    def test_html_lang_is_korean(self):
        self.assertIn('<html lang="ko">', self.client.get("/").content.decode())

    def test_product_page_description_is_product_specific(self):
        p = make_product(issuer="한국투자증권", product_no="24680", yield_rate=9.5,
                         ki=45, loss_prob=2.3)
        head = self._head(f"/product/{p.pk}/")
        self.assertIn("한국투자증권 24680", head)
        self.assertIn("연 수익률 9.5%", head)
        self.assertIn("낙인 45%", head)
        self.assertIn("만기손실확률 2.3%", head)


class HeadingStructureTests(TestCase):
    """h1은 페이지당 하나. 검색엔진이 이 화면의 주제를 읽는 신호다."""

    def test_public_pages_have_exactly_one_h1(self):
        make_product()
        paths = ["/", "/weekly/", "/trend/", "/report/els-10year/",
                 "/terms/", "/privacy/", "/disclaimer/"]
        paths.append(f"/product/{Product.objects.first().pk}/")
        for path in paths:
            html = self.client.get(path).content.decode()
            self.assertEqual(html.count("<h1"), 1, f"{path} 의 h1 개수가 1이 아니다")


class ReportPageTests(TestCase):
    def test_page_is_public_and_reachable(self):
        resp = self.client.get(reverse("report_els_10year"))
        self.assertEqual(resp.status_code, 200)

    def test_url_is_search_friendly(self):
        self.assertEqual(reverse("report_els_10year"), "/report/els-10year/")

    def test_key_figures_are_present_verbatim(self):
        """수치는 검증·컨펌을 마친 확정값이다. 화면에서 조용히 바뀌면 안 된다."""
        html = self.client.get("/report/els-10year/").content.decode()
        for figure in ("69,903", "3,220", "95.4%", "4.61%", "26.33%", "74.8%",
                       "65,366", "4,492", "71.7%", "86.6%", "10.51%", "3.62%",
                       "2,245", "69.7%", "81.5%", "5,164", "99.57%", "72.7%"):
            self.assertIn(figure, html, f"{figure} 이 리포트 화면에 없다")

    def test_limits_section_is_published(self):
        """한계를 지우면 이 리포트의 신뢰가 통째로 사라진다."""
        html = self.client.get("/report/els-10year/").content.decode()
        self.assertIn("7. 한계", html)
        self.assertIn("시세 기반 판정입니다", html)
        self.assertIn("투자 권유가 아닙니다", html)

    def test_structured_data_is_valid_json(self):
        import json
        import re
        html = self.client.get("/report/els-10year/").content.decode()
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        self.assertEqual(len(blocks), 2, "Article·BreadcrumbList 두 블록이 필요하다")
        types = {json.loads(b)["@type"] for b in blocks}
        self.assertEqual(types, {"Article", "BreadcrumbList"})

    def test_tables_scroll_horizontally_on_mobile(self):
        """표가 12열까지 간다 — 감싸지 않으면 좁은 화면에서 잘린다."""
        html = self.client.get("/report/els-10year/").content.decode()
        self.assertIn("overflow-x:auto", html.replace("overflow-x: auto", "overflow-x:auto"))
        self.assertIn('class="rp-table"', html)

    def test_landing_links_to_the_report(self):
        html = self.client.get("/").content.decode()
        self.assertIn('href="/report/els-10year/"', html)


class ArticleSeoParityTests(TestCase):
    """1~6편도 뒤 수업과 같은 검색 구조와 내부 이동을 갖는다."""

    FIRST_SIX = (
        "/articles/els-basics/", "/articles/stepdown-numbers/",
        "/articles/knock-in/", "/articles/worst-of/",
        "/articles/yield-calculation/", "/articles/els-vs-elb/",
    )

    def _html(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.content.decode()

    def test_first_six_have_article_breadcrumb_and_faq_schema(self):
        import json
        import re

        for path in self.FIRST_SIX:
            blocks = re.findall(
                r'<script type="application/ld\+json">(.*?)</script>',
                self._html(path), re.S)
            types = {json.loads(block)["@type"] for block in blocks}
            self.assertEqual(types, {"Article", "BreadcrumbList", "FAQPage"}, path)

    def test_first_six_have_social_tags_and_service_links(self):
        for path in self.FIRST_SIX:
            html = self._html(path)
            for tag in ('property="og:image"', 'name="twitter:card"',
                        'name="twitter:image"'):
                self.assertIn(tag, html, f"{path} 에 {tag} 가 없다")
        self.assertIn('href="/weekly/"', self._html("/articles/els-basics/"))
        self.assertIn('href="/assets/"', self._html("/articles/knock-in/"))

    def test_overlapping_topics_link_to_their_deeper_lesson(self):
        links = {
            "/articles/els-basics/": "/articles/maturity-profit-loss/",
            "/articles/knock-in/": "/articles/recover-after-knock-in/",
            "/articles/worst-of/": "/articles/more-assets-diversification/",
            "/articles/yield-calculation/": "/articles/els-tax/",
        }
        for source, target in links.items():
            self.assertIn(f'href="{target}"', self._html(source), source)

    def test_deeper_lessons_link_back_to_the_concept(self):
        links = {
            "/articles/more-assets-diversification/": "/articles/worst-of/",
            "/articles/recover-after-knock-in/": "/articles/knock-in/",
            "/articles/maturity-profit-loss/": "/articles/els-basics/",
        }
        for source, target in links.items():
            self.assertIn(f'href="{target}"', self._html(source), source)

    def test_tax_and_issuer_lesson_ctas_match_the_topic(self):
        self.assertNotIn('/report/els-10year/', self._html("/articles/els-tax/"))
        self.assertIn('href="/weekly/"', self._html("/articles/issuer-credit-risk/"))

    def test_quiz_questions_are_not_heading_level_two(self):
        for path in self.FIRST_SIX:
            html = self._html(path)
            self.assertNotIn("<h2>30초 확인 문제", html, path)

    def test_stepdown_88_example_uses_vertical_journey_without_arrows(self):
        html = self._html("/articles/stepdown-numbers/")
        self.assertIn('class="eval-journey"', html)
        self.assertIn("기준보다 2%p 낮음", html)
        self.assertNotIn('class="checkpoint-arrow"', html)


class SitemapWithUserDataTests(TestCase):
    """로그인 사용자가 있어도 개인 화면은 사이트맵에 새지 않는다."""

    def test_no_user_specific_urls(self):
        User = get_user_model()
        User.objects.create_user(username="tester", password="pw-for-test-only")
        make_product()
        _, locs = sitemap_locs(self.client)
        paths = [urlsplit(loc).path for loc in locs]
        self.assertTrue(all(not path.startswith(("/portfolio/", "/watchlist/"))
                            for path in paths))
