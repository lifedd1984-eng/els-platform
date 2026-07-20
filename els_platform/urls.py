from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from core import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/', auth_views.LoginView.as_view(template_name='core/login.html'), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('accounts/signup/', views.signup, name='signup'),
    path('accounts/password/', auth_views.PasswordChangeView.as_view(
        template_name='core/password_change.html', success_url='/'), name='password_change'),

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
]
