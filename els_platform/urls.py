from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from core import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/', auth_views.LoginView.as_view(template_name='core/login.html'), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),

    path('', views.weekly, name='weekly'),
    path('product/<int:pk>/', views.product_detail, name='product_detail'),
    path('presets/', views.presets, name='presets'),
    path('watchlist/', views.watchlist, name='watchlist'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('calendar/', views.redemption_calendar, name='calendar'),
    path('trend/', views.market_trend, name='trend'),
    path('upload/', views.upload_excel, name='upload'),
]
