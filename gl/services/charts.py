"""Chart geometry, computed once in Python so templates only draw. Each helper takes plain figures and returns what
the matching partial in templates/includes needs: positions and sizes in percent, and the figures themselves for
labels, tooltips and the table beside the chart."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

ZERO = Decimal(0)


def pct(part: Decimal, whole: Decimal, places: str = "0.1") -> float:
    return float((part / whole * 100).quantize(Decimal(places), rounding=ROUND_HALF_UP)) if whole else 0.0


def ranked(pairs: list[tuple[str, Decimal]], *, sort: bool = True, bad: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Ranked bars on one baseline: each label's amount, its bar as a share of the largest, and its share of the total."""
    rows = sorted(pairs, key=lambda p: -p[1]) if sort else list(pairs)
    most, total = max((v for _, v in rows), default=ZERO), sum((v for _, v in rows), ZERO)
    return [{"label": label, "value": value, "width": pct(max(value, ZERO), most), "share": pct(value, total), "bad": label in bad} for label, value in rows]


def month_label(d: date, first: bool = False) -> dict[str, str]:
    return {"label": f"{d:%b %Y}", "short": f"{d:%b}", "year": f"{d:%Y}" if first or d.month == 1 else ""}


def pairs(rows: list[tuple[date, Decimal, Decimal]], names: tuple[str, str]) -> dict[str, Any]:
    """Two columns per period on one zero baseline (money in beside money out)."""
    most = max((max(a, b) for _, a, b in rows), default=ZERO)
    cols = [{**month_label(d, n == 0), "a": a, "b": b, "net": a - b, "ha": pct(a, most), "hb": pct(b, most),
             "edge": "left" if n < 1 else "right" if n > len(rows) - 2 else ""} for n, (d, a, b) in enumerate(rows)]
    return {"cols": cols, "names": names, "total_a": sum((a for _, a, _ in rows), ZERO), "total_b": sum((b for _, _, b in rows), ZERO)} if most else {}


def trend(points: list[tuple[dict[str, str], list[Decimal]]], names: list[str], *, from_zero: bool = False, reference: tuple[str, Decimal] | None = None) -> dict[str, Any]:
    """One or more series over time as lines. Each point sits in the middle of its period's column, so the labels
    under the chart and the hover areas line up with it. `reference`: a level to mark (a commitment, a target)."""
    values = [v for _, vs in points for v in vs] + ([reference[1]] if reference else [])
    if not points or not any(values):
        return {}
    low, high = (ZERO if from_zero else min(values)), max(values)
    pad = (high - low) * Decimal("0.15") or abs(high) * Decimal("0.05") or Decimal(1)
    floor, ceiling = (low if from_zero else low - pad), high + pad
    y = lambda v: round(float(100 - (v - floor) / (ceiling - floor) * 100), 2)  # noqa: E731  (percent from the top)
    n = len(points)
    xs = [round(100.0 * (i + 0.5) / n, 2) for i in range(n)]
    cols = [{**label, "values": [{"name": names[k], "value": v, "y": y(v), "k": k + 1} for k, v in enumerate(vs)],
             "edge": "left" if i < 2 else "right" if i > n - 3 else "", "last": i == n - 1} for i, (label, vs) in enumerate(points)]
    lines = [{"name": name, "k": k + 1, "points": " ".join(f"{xs[i]},{y(vs[k])}" for i, (_, vs) in enumerate(points)),
              "last": points[-1][1][k], "last_y": y(points[-1][1][k])} for k, name in enumerate(names)]
    first = points[0][1][0]
    return {"n": n, "cols": cols, "lines": lines, "single": len(names) == 1,
            "area": f"{xs[0]},100 {lines[0]['points']} {xs[-1]},100" if len(names) == 1 else "",
            "first": first, "first_y": y(first), "first_below": n > 1 and points[1][1][0] > first,
            "reference": {"name": reference[0], "value": reference[1], "y": y(reference[1])} if reference else None}


def changes(opening: tuple[str, Decimal], steps: list[tuple[str, Decimal]], closing: tuple[str, Decimal]) -> dict[str, Any]:
    """What moved a balance from its opening to its closing figure: each change as a bar from a common zero (increases
    to the right, decreases to the left) on the changes' own scale. The balances themselves are stated as figures: next
    to a large balance, bars drawn from zero would hide the changes."""
    most = max((abs(v) for _, v in steps), default=ZERO)
    rows = [{"label": label, "value": value, "width": pct(abs(value), most), "negative": value < 0} for label, value in steps]
    return {"opening": {"label": opening[0], "value": opening[1]}, "closing": {"label": closing[0], "value": closing[1]}, "rows": rows,
            "change": closing[1] - opening[1], "up": closing[1] >= opening[1], "any": bool(most)}
