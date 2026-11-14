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


@register.filter
def compact(value: Decimal | None) -> str:
    """$2.9M, $615K, -$40K: for labels on a chart, where the exact figure is in the table."""
    if value is None:
        return ""
    v, sign = abs(Decimal(value)), "-" if value < 0 else ""
    for size, unit, step in ((Decimal(1_000_000_000), "B", "0.1"), (Decimal(1_000_000), "M", "0.1"), (Decimal(1_000), "K", "1")):
        if v >= size:
            scaled = (v / size).quantize(Decimal(step), rounding=ROUND_HALF_UP)
            return f"{sign}${scaled.normalize():f}{unit}" if scaled == scaled.to_integral() else f"{sign}${scaled}{unit}"
    return f"{sign}${v.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}"


@register.filter
def get(mapping: object, key: str) -> object:
    """A value from a dict by key, for data that reached the template as plain JSON."""
    return mapping.get(key, "") if isinstance(mapping, dict) else ""
