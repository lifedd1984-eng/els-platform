"""기본 프리셋 3종 시드 — 사용자가 수정/삭제 가능."""

from django.db import migrations


def seed_presets(apps, schema_editor):
    Preset = apps.get_model("core", "Preset")
    defaults = [
        dict(name="저낙인 종목형", is_default=True, asset_type="종목형",
             ki_max=25, include_no_ki=False, yield_min=15.0),
        dict(name="안정 지수형", is_default=True, asset_type="지수형",
             ki_max=40, include_no_ki=True, yield_min=10.0),
        dict(name="고수익 헌터", is_default=True, asset_type="전체",
             yield_min=20.0, include_no_ki=True),
    ]
    for d in defaults:
        Preset.objects.get_or_create(name=d["name"], defaults=d)


def unseed(apps, schema_editor):
    Preset = apps.get_model("core", "Preset")
    Preset.objects.filter(is_default=True).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(seed_presets, unseed)]
