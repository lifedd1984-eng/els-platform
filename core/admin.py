from django.contrib import admin

from .models import ImportLog, Investment, Preset, Product, WatchItem


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("issuer", "product_no", "yield_rate", "ki", "is_no_ki",
                    "asset_type", "sub_end", "currency")
    list_filter = ("issuer", "asset_type", "is_no_ki", "currency")
    search_fields = ("issuer", "product_no", "assets_raw")


@admin.register(Preset)
class PresetAdmin(admin.ModelAdmin):
    list_display = ("name", "is_default", "asset_type", "ki_max", "yield_min", "notify")


@admin.register(Investment)
class InvestmentAdmin(admin.ModelAdmin):
    list_display = ("product", "amount", "invested_at", "status", "redeemed_amount")


admin.site.register(WatchItem)
admin.site.register(ImportLog)
