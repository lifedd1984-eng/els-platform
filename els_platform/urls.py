from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from core import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/', views.RememberLoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('accounts/signup/', views.signup, name='signup'),
    path('accounts/password/', auth_views.PasswordChangeView.as_view(
        template_name='core/password_change.html', success_url='/'), name='password_change'),
    path('accounts/find-id/', views.find_id, name='find_id'),
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

    path('', views.weekly, name='weekly'),
    path('product/<int:pk>/', views.product_detail, name='product_detail'),
    path('presets/', views.presets, name='presets'),
    path('watchlist/', views.watchlist, name='watchlist'),
    path('watchlist/export/', views.watchlist_export, name='watchlist_export'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('portfolio/template/', views.portfolio_template, name='portfolio_template'),
    path('portfolio/export/', views.portfolio_export, name='portfolio_export'),
    path('portfolio/upload/', views.portfolio_upload, name='portfolio_upload'),
    path('calendar/', views.redemption_calendar, name='calendar'),
    path('trend/', views.market_trend, name='trend'),
    path('upload/', views.upload_excel, name='upload'),
    path('manage/members/', views.member_admin, name='member_admin'),
    path('search/', views.product_search, name='search'),
    path('manifest.json', views.pwa_manifest, name='pwa_manifest'),
    path('icons/icon-<str:size>.png', views.pwa_icon, name='pwa_icon'),
]
