from decimal import ROUND_HALF_UP, Decimal

from django import template

register = template.Library()


@register.filter
def money(value: Decimal | None) -> str:
    """$1,234,567 (whole dollars) for summary tables."""
    if value is None:
        return "—"
    rounded = Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"-${-rounded:,}" if rounded < 0 else f"${rounded:,}"


@register.filter
def acct(value: Decimal | None, places: int = 0) -> str:
    """Statement style: 1,234 and (1,234), a dash for zero, nothing for a blank cell."""
    if value is None:
        return ""
    rounded = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if not rounded:
        return "–"
    return f"({-rounded:,})" if rounded < 0 else f"{rounded:,}"


@register.filter
def multiple(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}x"


@register.filter
def pct(value: Decimal | None, places: int = 1) -> str:
    return "—" if value is None else f"{Decimal(value) * 100:.{places}f}%"
