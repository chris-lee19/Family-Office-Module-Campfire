from decimal import Decimal
from typing import Any

from django import template

register = template.Library()


@register.filter
def nw_total(nw: Any, category: str) -> Decimal:
    return nw.total(category) if nw is not None else Decimal(0)
