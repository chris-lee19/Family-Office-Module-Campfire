"""Small charts drawn in HTML: an allocation as one stacked bar with a legend that doubles as its table."""

from decimal import Decimal
from typing import Any

from django import template

register = template.Library()
MAX_SLOTS = 8  # the categorical palette has eight colours; anything beyond folds into "Other"


def segments(rows: list[Any]) -> list[dict[str, Any]]:
    """Rows with .label / .value / .pct (largest first) -> bar segments. A label keeps its colour whatever its
    rank: colours go by the labels' alphabetical order, not by size."""
    positive = [r for r in rows if r.value > 0]
    keep, rest = positive[:MAX_SLOTS], positive[MAX_SLOTS:]
    if rest:  # fold the smallest into "Other" so there is room for it
        keep, rest = positive[:MAX_SLOTS - 1], positive[MAX_SLOTS - 1:]
    slot = {label: i + 1 for i, label in enumerate(sorted(r.label for r in keep))}
    out = [{"label": r.label, "value": r.value, "pct": r.pct, "slot": slot[r.label]} for r in keep]
    if rest:
        out.append({"label": f"Other ({len(rest)})", "value": sum((r.value for r in rest), Decimal(0)),
                    "pct": sum((r.pct for r in rest), Decimal(0)), "slot": 0})
    return out


@register.inclusion_tag("includes/alloc.html")
def alloc(rows: list[Any], label: str = "Allocation") -> dict[str, Any]:
    return {"segments": segments(rows), "others": [r for r in rows if r.value <= 0], "label": label}
