from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path

from core import views
from core.sitemaps import SITEMAPS

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/', views.RememberLoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('accounts/signup/', views.signup, name='signup'),
    path('accounts/password/', auth_views.PasswordChangeView.as_view(
        template_name='core/password_change.html', success_url='/'), name='password_change'),
    path('accounts/find-id/', views.find_id, name='find_id'),
    path('accounts/delete/', views.account_delete, name='account_delete'),
    path('accounts/password-reset/', auth_views.PasswordResetView.as_view(
        template_name='core/password_reset.html',
        email_template_name='core/password_reset_email.txt',
        subject_template_name='core/password_reset_subject.txt'), name='password_reset'),
    path('accounts/password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='core/password_reset_done.html'), name='password_reset_done'),
    path('accounts/reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='core/password_reset_confirm.html'), name='password_reset_confirm'),
    path('accounts/reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='core/password_reset_complete.html'), name='password_reset_complete'),

    # ── 소셜 로그인 (allauth) ─────────────────────────────
    # allauth.urls 를 통째로 붙이지 않는다. 그러면 allauth 자신의 로그인·가입·
    # 비밀번호 재설정 화면이 /accounts/ 아래에 함께 생겨, 우리 한국어 화면과
    # 영어 기본 화면이 두 벌로 굴러다닌다. 소셜에 필요한 것만 붙인다.
    #
    # allauth 내부가 reverse('account_login') 을 쓰므로(소셜 가입 세션이 끊겼을
    # 때의 되돌림 등) 우리 로그인 뷰에 그 이름을 하나 더 달아 준다. 경로가
    # 같으니 동작도 같다.
    path('accounts/login/', views.RememberLoginView.as_view(), name='account_login'),
    path('accounts/signup/', views.signup, name='account_signup'),
    # 키가 없을 때를 막는 관문. allauth 의 같은 경로보다 먼저 등록해야 한다.
    path('accounts/kakao/login/', views.kakao_login_entry, name='kakao_login_entry'),
    path('accounts/kakao/login/callback/', views.kakao_callback_entry,
         name='kakao_callback_entry'),
    path('accounts/google/login/', views.google_login_entry, name='google_login_entry'),
    path('accounts/google/login/callback/', views.google_callback_entry,
         name='google_callback_entry'),
    path('accounts/naver/login/', views.naver_login_entry, name='naver_login_entry'),
    path('accounts/naver/login/callback/', views.naver_callback_entry,
         name='naver_callback_entry'),
    # 자동 연결을 하지 않았을 때의 안내 화면
    path('accounts/social/help/', views.social_connect_help, name='social_connect_help'),
    # 연결 목록·해제 (allauth 뷰 + 우리 템플릿) 및 소셜 가입 보조 화면
    path('accounts/social/', include('allauth.socialaccount.urls')),
    # /accounts/kakao/login/ 등의 실제 구현. 위 관문이 먼저 매칭되므로 여기는
    # 이름(kakao_login·google_login·naver_login 등) 역참조용이다.
    path('accounts/', include('allauth.socialaccount.providers.kakao.urls')),
    path('accounts/', include('allauth.socialaccount.providers.google.urls')),
    path('accounts/', include('allauth.socialaccount.providers.naver.urls')),

    path('', views.home, name='home'),
    path('weekly/', views.weekly, name='weekly'),
    path('product/<int:pk>/', views.product_detail, name='product_detail'),
    # 유사상품 비교 패널 조각 (목록 모달·상세 토글이 함께 부른다)
    path('product/<int:pk>/compare/', views.product_compare, name='product_compare'),
    path('presets/', views.presets, name='presets'),
    path('watchlist/', views.watchlist, name='watchlist'),
    path('watchlist/export/', views.watchlist_export, name='watchlist_export'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('portfolio/template/', views.portfolio_template, name='portfolio_template'),
    path('portfolio/export/', views.portfolio_export, name='portfolio_export'),
    # 구글 시트가 읽어 가는 동기화 피드 — 토큰 없으면 404 (로그인 아님)
    path('portfolio/sync.json', views.portfolio_sync, name='portfolio_sync'),
    path('portfolio/upload/', views.portfolio_upload, name='portfolio_upload'),
    path('calendar/', views.redemption_calendar, name='calendar'),
    path('trend/', views.market_trend, name='trend'),
    path('assets/', views.asset_list, name='asset_list'),
    path('assets/<slug:slug>/', views.asset_detail, name='asset_detail'),
    path('upload/', views.upload_excel, name='upload'),
    path('manage/members/', views.member_admin, name='member_admin'),
    path('search/', views.product_search, name='search'),
    path('ask/', views.ask, name='ask'),
    path('about/', views.about, name='about'),
    path('articles/', views.article_list, name='article_list'),
    path('articles/els-basics/', views.article_els_basics, name='article_els_basics'),
    path('articles/stepdown-numbers/', views.article_els_stepdown, name='article_els_stepdown'),
    path('articles/knock-in/', views.article_els_knock_in, name='article_els_knock_in'),
    path('articles/worst-of/', views.article_els_worst_of, name='article_els_worst_of'),
    path('articles/yield-calculation/', views.article_els_yield, name='article_els_yield'),
    path('articles/els-vs-elb/', views.article_els_vs_elb, name='article_els_vs_elb'),
    path('articles/redemption-rate/', views.article_data_lesson, {'slug': 'redemption-rate'}, name='article_els_redemption_rate'),
    path('articles/coupon-risk/', views.article_data_lesson, {'slug': 'coupon-risk'}, name='article_els_coupon_risk'),
    path('articles/issue-timing/', views.article_data_lesson, {'slug': 'issue-timing'}, name='article_els_issue_timing'),
    path('articles/maturity-denominator/', views.article_data_lesson, {'slug': 'maturity-denominator'}, name='article_els_maturity_denominator'),
    path('articles/type-mix/', views.article_data_lesson, {'slug': 'type-mix'}, name='article_els_type_mix'),
    path('articles/type-loss/', views.article_data_lesson, {'slug': 'type-loss'}, name='article_els_type_loss'),
    path('articles/recent-three-years/', views.article_data_lesson, {'slug': 'recent-three-years'}, name='article_els_recent_three_years'),
    path('articles/redemption-and-loss/', views.article_data_lesson, {'slug': 'redemption-and-loss'}, name='article_els_redemption_and_loss'),
    path('articles/statistics-checklist/', views.article_data_lesson, {'slug': 'statistics-checklist'}, name='article_els_statistics_checklist'),
    path('articles/media/<str:name>', views.article_image, name='article_image'),
    path('report/els-10year/', views.report_els_10year, name='report_els_10year'),
    path('feedback/', views.feedback, name='feedback'),
    path('feedback/dismiss/', views.feedback_dismiss, name='feedback_dismiss'),
    path('terms/', views.legal_terms, name='terms'),
    path('privacy/', views.legal_privacy, name='privacy'),
    path('disclaimer/', views.legal_disclaimer, name='disclaimer'),
    path('stats/', views.stats, name='stats'),
    path('manifest.json', views.pwa_manifest, name='pwa_manifest'),
    path('icons/icon-<str:size>.png', views.pwa_icon, name='pwa_icon'),
    path('og.png', views.og_image, name='og_image'),
    path('threads-card/<int:day>.png', views.threads_card, name='threads_card'),
    path('sw.js', views.service_worker, name='service_worker'),
    path('sitemap.xml', sitemap, {'sitemaps': SITEMAPS}, name='django.contrib.sitemaps.views.sitemap'),
    path('robots.txt', views.robots_txt, name='robots_txt'),
    # 네이버 소유확인 파일 — 루트에 있어야 네이버가 찾는다
    path(views.NAVER_VERIFY_FILE, views.naver_verify, name='naver_verify'),
    path('push/subscribe/', views.push_subscribe, name='push_subscribe'),
    path('push/unsubscribe/', views.push_unsubscribe, name='push_unsubscribe'),
    path('push/test/', views.push_test, name='push_test'),
]
