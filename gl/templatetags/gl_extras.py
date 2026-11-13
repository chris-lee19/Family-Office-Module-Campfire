from typing import Any

from django import template

register = template.Library()


@register.filter
def get_item(mapping: dict[Any, Any] | None, key: Any) -> Any:
    return (mapping or {}).get(key)
