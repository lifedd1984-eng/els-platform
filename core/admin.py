from django.contrib import admin

from .models import ImportLog, Investment, Preset, Product, ThreadsReply, WatchItem


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


@admin.register(ThreadsReply)
class ThreadsReplyAdmin(admin.ModelAdmin):
    """자동 응답 범위 사후 검증용 — 분류 근거와 실제 발송 문구를 나란히 본다."""
    list_display = ("timestamp", "bucket", "bucket_reason", "username", "text",
                    "status", "replied_text")
    list_filter = ("bucket", "status")
    search_fields = ("username", "text", "bucket_reason")


admin.site.register(WatchItem)
admin.site.register(ImportLog)
