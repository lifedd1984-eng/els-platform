"""검색엔진 사이트맵 (/sitemap.xml).

왜 필요한가
  2026-08-14 확인 시점에 /sitemap.xml 이 404였다. 검색엔진이 사이트 구조를
  스스로 알아낼 방법이 링크 추적뿐이라, 외부 링크가 거의 없는 신규 도메인은
  사실상 색인이 안 된다. 지인·커뮤니티 유입 경로가 없는 상태에서 검색 유입이
  유일한 무인 유입 경로라 우선순위를 높게 잡았다.

도메인은 하드코딩하지 않는다 — django.contrib.sites 를 쓰지 않으므로
sitemap 뷰가 RequestSite(요청 Host)로 폴백한다. 로컬·EC2 어디서 열어도
그 호스트 기준으로 절대 URL이 만들어진다.
"""

from datetime import date, timedelta

from django.contrib.sitemaps import Sitemap
from django.db.models import Max
from django.urls import reverse

from .asset_pages import TOP_ASSETS
from .models import Product

# 1편 리포트 본문이 확정된 날 (= 본문에 적힌 집계 기준일).
# 수치를 갱신하면 이 값도 함께 올린다 — 검색엔진에 "내용이 바뀌었다"고
# 알리는 유일한 신호다.
REPORT_10YEAR_UPDATED = date(2026, 8, 13)
ARTICLES_UPDATED = date(2026, 8, 29)

# 상품 상세를 사이트맵에 넣는 기간 (청약마감일 기준, 일).
# 왜 전량이 아닌가는 ProductSitemap docstring 참조. 나중에 Search Console에서
# 색인 제외가 대량으로 잡히면 이 상수만 줄이면 된다.
PRODUCT_SITEMAP_DAYS = 180


class StaticViewSitemap(Sitemap):
    """공개 정적 페이지.

    로그인이 필요한 화면(관심·포트폴리오·상환 캘린더·AI 분석 질문)과
    운영자 화면(업로드·회원·접속 통계)은 넣지 않는다. 검색 결과로 들어와도
    로그인 벽만 보게 되므로 색인돼 봐야 이탈만 만든다.

    /about/ 도 넣지 않는다 — views.home 이 about(request)를 그대로 반환해
    / 와 완전히 같은 HTML을 낸다. 둘 다 제출하면 중복 콘텐츠를 자진 신고하는
    꼴이라, 사이트맵에는 / 만 넣고 /about/ 에는 canonical 로 / 를 가리켰다.
    """

    protocol = "https"

    # (URL 이름, 우선순위, 변경주기)
    PAGES = [
        ("home", 1.0, "weekly"),          # 랜딩 — TOP5가 매주 바뀐다
        ("weekly", 0.9, "weekly"),        # 주간 청약 — 매주 월요일 갱신
        ("report_els_10year", 0.8, "monthly"),   # 10년 성적표 리포트
        ("article_list", 0.8, "monthly"),
        ("article_els_basics", 0.7, "monthly"),
        ("article_els_stepdown", 0.7, "monthly"),
        ("article_els_knock_in", 0.7, "monthly"),
        ("article_els_worst_of", 0.7, "monthly"),
        ("article_els_yield", 0.7, "monthly"),
        ("article_els_vs_elb", 0.7, "monthly"),
        *[(f"article_course_{number:02d}", 0.7, "monthly") for number in range(7, 31)],
        ("asset_list", 0.7, "weekly"),    # 기초자산 허브 목록
        ("trend", 0.6, "weekly"),         # 시장 트렌드 — 20주 추이
        ("disclaimer", 0.3, "yearly"),
        ("terms", 0.2, "yearly"),
        ("privacy", 0.2, "yearly"),
    ]

    def items(self):
        return self.PAGES

    def location(self, item):
        return reverse(item[0])

    def priority(self, item):
        return item[1]

    def changefreq(self, item):
        return item[2]

    def lastmod(self, item):
        name = item[0]
        if name == "report_els_10year":
            return REPORT_10YEAR_UPDATED
        if name.startswith("article_"):
            return ARTICLES_UPDATED
        if name in ("home", "weekly", "trend"):
            # 주간 데이터가 마지막으로 들어온 날. 없으면 lastmod 를 아예 뺀다 —
            # 지어낸 날짜를 넣는 것보다 없는 편이 낫다.
            return _last_collected()
        return None      # 약관·개인정보·유의사항은 개정일을 따로 관리하지 않는다


class ProductSitemap(Sitemap):
    """상품 상세 (/product/<id>/) — 최근 분만.

    넣는 이유
      상품 상세는 이 사이트에서 유일한 롱테일 지면이다. 발행사·상품번호·
      기초자산 조합으로 검색해 들어올 수 있고, 낙인까지 남은 거리·손실확률·
      배리어 계단처럼 다른 곳에 없는 계산값을 담고 있어 빈 페이지가 아니다.

    전량을 넣지 않는 이유
      청약이 끝난 지 오래된 상품은 읽을 사람이 없다. 권한이 아직 없는 신규
      도메인에 수천 건을 한꺼번에 제출하면 크롤링 예산이 그쪽으로 흩어져,
      정작 유입을 만드는 랜딩·주간청약·리포트가 덜 자주 수집된다.
      그래서 '아직 볼 이유가 남아 있는' 기간만 제출한다.

    조건
      · Product.objects.listed() — 원금지급형(ELB·DLB)은 목록 규칙상 제외
      · 청약마감일이 최근 PRODUCT_SITEMAP_DAYS 일 이내이거나 아직 안 지남
      · 수익률과 낙인 정보가 있는 것만 (파싱이 덜 된 껍데기 페이지 제외)
    """

    protocol = "https"

    def items(self):
        cutoff = date.today() - timedelta(days=PRODUCT_SITEMAP_DAYS)
        return (Product.objects.listed()
                .filter(sub_end__gte=cutoff, yield_rate__isnull=False)
                .exclude(ki__isnull=True, is_no_ki=False)
                .only("id", "sub_end", "sim_updated", "collected_at")
                .order_by("-sub_end", "id"))

    def location(self, obj):
        return reverse("product_detail", args=[obj.pk])

    def lastmod(self, obj):
        # 시뮬레이션이 다시 돌면 화면 숫자가 바뀐다 — 그게 실제 갱신 시점이다.
        return obj.sim_updated or obj.collected_at

    def priority(self, obj):
        # 아직 청약할 수 있는 상품이 먼저다.
        return 0.5 if obj.sub_end and obj.sub_end >= date.today() else 0.3

    def changefreq(self, obj):
        return "daily" if obj.sub_end and obj.sub_end >= date.today() else "monthly"


class AssetSitemap(Sitemap):
    """기초자산별 공개 페이지 (/assets/<slug>/) — 상위 10개 고정.

    엘마 벤치마크로 만든 검색 유입용 지면(경쟁분석_엘마_v1.md 6장 1순위,
    2026-08-24). 상품 상세와 달리 URL이 늘 10개뿐이라 만료 걱정 없이
    항상 전량을 낸다.
    """

    protocol = "https"
    changefreq = "weekly"   # 최근 발행 집계라 주간 배치 때마다 바뀐다
    priority = 0.7

    def items(self):
        return [slug for slug, _, _ in TOP_ASSETS]

    def location(self, slug):
        return reverse("asset_detail", args=[slug])

    def lastmod(self, slug):
        return _last_collected()


def _last_collected():
    """주간 데이터가 마지막으로 들어온 날짜 (없으면 None)."""
    latest = Product.objects.listed().aggregate(m=Max("collected_at"))["m"]
    return latest.date() if latest else None


SITEMAPS = {
    "static": StaticViewSitemap,
    "products": ProductSitemap,
    "assets": AssetSitemap,
}
