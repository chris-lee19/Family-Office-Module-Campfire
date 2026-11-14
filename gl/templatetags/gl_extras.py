from typing import Any

from django import template

register = template.Library()


@register.filter
def get_item(mapping: dict[Any, Any] | None, key: Any) -> Any:
    return (mapping or {}).get(key)


@register.filter
def stmt_total(statement: Any, name: str) -> Any:
    """Total of a column on a capital statement."""
    return statement.total(name)


@register.filter
def k1_total(statement: Any, line: str) -> Any:
    return statement.k1_total(line)


@register.filter
def sub(value: Any, arg: Any) -> Any:
    try:
        return int(value) - int(arg)
    except (TypeError, ValueError):
        return ""
