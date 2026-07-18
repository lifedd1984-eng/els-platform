from django import template

from core import market

register = template.Library()


@register.filter
def asset_short(value):
    """기초자산 원본 문자열을 화면 표시용으로 축약 (저장값 변경 없음)."""
    return market.shorten_asset_display(value)


@register.simple_tag(takes_context=True)
def qs_replace(context, **kwargs):
    """현재 쿼리스트링을 유지하며 일부 파라미터만 교체.
    사용: ?{% qs_replace hpage=3 %}  → 기존 필터/정렬 유지 + hpage=3
    """
    request = context.get("request")
    q = request.GET.copy() if request else None
    if q is None:
        return ""
    for k, v in kwargs.items():
        if v is None or v == "":
            q.pop(k, None)
        else:
            q[k] = v
    return q.urlencode()


@register.simple_tag(takes_context=True)
def qs_set(context, key, value):
    """동적 키 하나만 교체 (키 이름이 변수일 때)."""
    request = context.get("request")
    q = request.GET.copy() if request else None
    if q is None:
        return ""
    if value in (None, ""):
        q.pop(key, None)
    else:
        q[key] = value
    return q.urlencode()
