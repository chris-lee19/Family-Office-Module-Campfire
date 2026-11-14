"""The executive dashboard's figures: where the family stands (net worth and how it got there, by entity), how liquid
it is (cash, the forecast, commitments), and how the books stand (bills, bank reconciliation).

Everything here comes from Campfire's books plus the investment records, through the same services the pages use, so
a number on the dashboard is the number on its page: net worth is the Statement of Net Worth's, cash is the Cash
page's, the forecast is the Liquidity and Cash Flow Forecast's. The result is plain data (strings and numbers) so it
can be kept as a snapshot: Home shows the last snapshot at once and refreshes it in the background.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from gl.models import AccountMapping, DashboardSnapshot, GLSettings, Role
from gl.services import bills as bill_svc
from gl.services import fairvalue, liquidity, reconcile
from gl.services import statements as st
from gl.services.liquidity import add_months, month_end
from investments.services.ledger import LedgerError

ZERO = Decimal(0)
TREND_MONTHS = 12
FRESH_MINUTES = 15
LE = "LIABILITIES_AND_EQUITY"


def _s(v: Decimal | None) -> str:
    return "" if v is None else str(Decimal(v).quantize(Decimal("0.01")))


def _pct(part: Decimal, whole: Decimal, places: str = "0.1") -> float:
    return float((part / whole * 100).quantize(Decimal(places), rounding=ROUND_HALF_UP)) if whole else 0.0


def compact(value: Decimal) -> str:
    v, sign = abs(value), "-" if value < 0 else ""
    for size, unit, step in ((Decimal(1_000_000_000), "B", "0.1"), (Decimal(1_000_000), "M", "0.1"), (Decimal(1_000), "K", "1")):
        if v >= size:
            return f"{sign}${(v / size).quantize(Decimal(step), rounding=ROUND_HALF_UP).normalize():f}{unit}"
    return f"{sign}${v.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}"


def _unrealized_by_entity(workspace: Workspace, as_of: date) -> dict[int, Decimal]:
    """Market value less cost of the investment holdings booked in each Campfire entity."""
    out: dict[int, Decimal] = {}
    for cf_entity, accounts in fairvalue.booked_accounts(workspace).items():
        total = ZERO
        for account in accounts:
            try:
                total += sum((value - cost for _, cost, value in fairvalue.unrealized_at(account, as_of).values()), ZERO)
            except LedgerError:
                continue
        out[cf_entity] = total
    return out


def _row(statement: st.Statement, label: str) -> list[Decimal]:
    return next(([v or ZERO for v in r.values] for r in statement.rows if r.label == label and r.kind in ("total", "grand")), [])


def net_worth(workspace: Workspace, client: CampfireClient, today: date) -> dict[str, Any] | None:
    """The family's net worth at estimated current value (the Statement of Net Worth's figure), its trend by month
    end, and how it splits by entity."""
    scope = next((s for s in st.classify(workspace, st.scopes(client)) if s.rollup), None)
    if scope is None:
        return None
    rate = GLSettings.for_workspace(workspace).estimated_tax_rate
    booked = frozenset(AccountMapping.objects.filter(workspace=workspace, role=Role.UNREALIZED).values_list("campfire_account_id", flat=True))
    tax = lambda unrealized: (max(unrealized, ZERO) * rate / 100).quantize(Decimal("0.01"))  # noqa: E731
    names = scope.names

    # The trend: the books by month, each month end brought to estimated current value.
    start = add_months(today.replace(day=1), -(TREND_MONTHS - 1))
    books = st.balance_sheet(client, scope, start, today, "monthly")
    assets, liabilities = _row(books, "Total assets"), _row(books, "Total liabilities")
    in_books = [sum((r.values[n] or ZERO for r in books.rows if r.account_id in booked), ZERO) for n in range(len(assets))]
    first = add_months(today.replace(day=1), -(len(assets) - 1))  # the last column is this month, whatever range came back
    ends = [min(month_end(add_months(first, n)), today) for n in range(len(assets))]
    points = []
    for n, when in enumerate(ends):
        if not assets[n] and not liabilities[n]:
            continue  # before the books start
        unrealized = sum(_unrealized_by_entity(workspace, when).values(), ZERO)
        points.append((when, assets[n] + unrealized - in_books[n], liabilities[n] + tax(unrealized)))
    if not points:
        return None
    when, total_assets, total_liabilities = points[-1]
    unrealized_now = _unrealized_by_entity(workspace, today)
    estimated_tax = tax(sum(unrealized_now.values(), ZERO))
    value = total_assets - total_liabilities
    series = [(d, a - b) for d, a, b in points]
    year_start = next((v for d, v in series if d == date(today.year - 1, 12, 31)), None)
    last_month = series[-2] if len(series) > 1 else None
    low, high = min(v for _, v in series), max(v for _, v in series)
    pad = (high - low) * Decimal("0.15") or abs(high) * Decimal("0.05") or Decimal(1)
    floor, ceiling = low - pad, high + pad
    xs = [100.0 * (n + 0.5) / len(series) for n in range(len(series))]  # each point sits in the middle of its month's column
    ys = [float(100 - (v - floor) / (ceiling - floor) * 100) for _, v in series]  # percent from the top
    trend = [{"label": f"{d:%b %Y}" if d != today else f"{d:%b %-d, %Y}", "short": f"{d:%b}", "year": f"{d:%Y}" if n == 0 or d.month == 1 else "",
              "value": _s(v), "compact": compact(v), "x": round(xs[n], 2), "y": round(ys[n], 2),
              "change": _s(v - series[n - 1][1]) if n else "", "up": n == 0 or v >= series[n - 1][1], "show": n in (0, len(series) - 1),
              "below": n == 0 and len(series) > 1 and series[1][1] > v,  # the first label sits clear of the line that follows it
              "edge": "left" if n < 2 else "right" if n > len(series) - 3 else ""} for n, (d, v) in enumerate(series)]

    # By entity: each entity's books (assets less liabilities), plus what its holdings are worth above cost.
    scope_all = {s.label: s.entity_id for s in st.scopes(client) if not s.rollup}
    data = client.get_json(st.BALANCE_SHEET_PATH, params={**scope.params(), "start_date": today.replace(day=1).isoformat(), "end_date": today.isoformat(), "group_by": "entity"})
    key = next(iter(sorted(data.get("headers", {}), reverse=True)), None)
    entities = []
    for name in (data["headers"][key] if key else []):
        if name == "Total":
            continue
        col = f"{key}_{name}"
        amount = lambda path: next((st._dec(r.get(col)) for r in data.get("balance_sheet", []) if r.get("hierarchy") == path), ZERO)  # noqa: E731, B023
        carried = sum((st._dec(r.get(col)) for r in data.get("balance_sheet", []) if str((r.get("hierarchy") or [""])[-1]).isdigit()
                       and int(r["hierarchy"][-1]) in booked), ZERO)
        net = amount(["ASSET"]) - amount([LE, "LIABILITY"]) + unrealized_now.get(scope_all.get(name, 0), ZERO) - carried
        if net:
            entities.append((name, net))
    entities.sort(key=lambda e: -e[1])
    top = max((v for _, v in entities), default=ZERO)
    return {
        "scope": scope.label, "name": names["bs"], "basis": names["basis"], "as_of": when.isoformat(), "value": _s(value),
        "assets": _s(total_assets), "liabilities": _s(total_liabilities - estimated_tax), "estimated_tax": _s(estimated_tax), "has_tax": bool(estimated_tax),
        "tax_rate": str(rate.normalize()) if rate else "", "trend": trend, "line": " ".join(f"{p['x']},{p['y']}" for p in trend),
        "area": f"{trend[0]['x']},100 {' '.join(str(p['x']) + ',' + str(p['y']) for p in trend)} {trend[-1]['x']},100",
        "month_change": _s(value - last_month[1]) if last_month else "", "month_up": not last_month or value >= last_month[1],
        "month_pct": _pct(value - last_month[1], abs(last_month[1])) if last_month else 0.0, "month_label": f"{last_month[0]:%b %-d}" if last_month else "",
        "year_change": _s(value - year_start) if year_start is not None else "", "year_up": year_start is None or value >= year_start,
        "year_pct": _pct(value - year_start, abs(year_start)) if year_start else 0.0,
        "entities": [{"name": name, "value": _s(v), "width": _pct(max(v, ZERO), top), "share": _pct(v, sum((x for _, x in entities), ZERO))} for name, v in entities],
    }


def cash_and_liquidity(workspace: Workspace, client: CampfireClient, today: date) -> dict[str, Any]:
    from gl.liquidity_views import chart

    fc = liquidity.build(workspace, client, today, 12)
    drawn = chart(fc)
    soon = fc.due_soon
    most = max((e.cash for e in fc.entities), default=ZERO)
    return {
        "cash": _s(fc.opening), "operating": _s(fc.operating), "custody": _s(fc.custody), "liquid": _s(fc.liquid),
        "low": _s(fc.low), "low_negative": fc.low < 0, "low_month": fc.low_month.label if fc.low_month else "", "end": _s(fc.end), "end_month": fc.months[-1].label,
        "due_soon": _s(-sum((f.amount for f in soon), ZERO)), "due_count": len(soon), "late": sum(1 for f in soon if f.late),
        "unfunded": _s(fc.total_unfunded), "coverage": str(fc.coverage) if fc.coverage is not None else "",
        "expected_items": sum(1 for f in fc.flows if f.kind == liquidity.PLANNED),
        "short": [{"entity": e.entity, "low": _s(e.low), "month": e.low_month, "covered": e.covered} for e in fc.short_entities],
        "chart": {"up": drawn["up"], "down": drawn["down"], "cols": [
            {"short": c["month"].short, "year": f"{c['month'].start:%Y}" if n == 0 or c["month"].start.month == 1 else "", "label": c["month"].label,
             "value": _s(c["value"]), "compact": compact(c["value"]), "negative": c["negative"], "height": c["height"], "show": c["label"], "edge": c["edge"],
             "opening": _s(c["opening"]), "money_in": _s(c["money_in"]), "money_out": _s(c["money_out"])} for n, c in enumerate(drawn["cols"])]} if drawn else {},
        "entities": [{"name": e.entity, "cash": _s(e.cash), "operating": _pct(max(e.operating, ZERO), most), "custody": _pct(max(e.custody, ZERO), most),
                      "short": e.short} for e in sorted(fc.entities, key=lambda e: -e.cash) if e.cash],
        "coming": [{"when": f.when.isoformat(), "mon": f"{f.when:%b}", "day": f"{f.when:%-d}", "label": f.label, "entity": f.entity, "amount": _s(-f.amount), "kind": f.source, "late": f.late}
                   for f in fc.flows if f.amount < 0 and f.kind in (liquidity.BILL, liquidity.REQUEST, liquidity.CALL) and f.when <= today + timedelta(days=45)][:8],
    }


def bills(client: CampfireClient, today: date) -> dict[str, Any]:
    """Open bills by how soon they are due: the ageing a controller looks at first."""
    open_bills = [b for b in bill_svc.bills(client, today) if b.is_open]
    buckets = [("Overdue", lambda d: d < 0, True), ("Due in 7 days", lambda d: 0 <= d <= 7, False), ("8 to 30 days", lambda d: 8 <= d <= 30, False),
               ("Later", lambda d: d > 30, False)]
    rows = []
    for label, test, bad in buckets:
        chosen = [b for b in open_bills if test(b.days if b.days is not None else 0)]
        rows.append({"label": label, "amount": sum((b.due for b in chosen), ZERO), "count": len(chosen), "bad": bad})
    most = max((r["amount"] for r in rows), default=ZERO)
    return {"total": _s(sum((b.due for b in open_bills), ZERO)), "count": len(open_bills),
            "buckets": [{**r, "amount": _s(r["amount"]), "width": _pct(r["amount"], most)} for r in rows]}


def reconciliation(workspace: Workspace, client: CampfireClient, today: date) -> dict[str, Any]:
    try:
        books = reconcile.books(client, workspace, today)
    except reconcile.ReconcileError as e:
        return {"error": str(e)}
    count = lambda state: sum(1 for b in books if b.state == state)  # noqa: E731
    target = today.replace(day=1) - timedelta(days=1)
    return {"total": len(books), "current": count("current"), "progress": count("progress"), "pending": count("pending"),
            "behind": count("behind") + count("never"), "target": target.isoformat(), "pct": _pct(Decimal(count("current")), Decimal(len(books)), "1")}


def build(workspace: Workspace, client: CampfireClient, today: date) -> dict[str, Any]:
    return {"net_worth": net_worth(workspace, client, today), "liquidity": cash_and_liquidity(workspace, client, today),
            "bills": bills(client, today), "reconciliation": reconciliation(workspace, client, today)}


def refresh(workspace: Workspace, client: CampfireClient, today: date) -> DashboardSnapshot:
    snap, _ = DashboardSnapshot.objects.update_or_create(workspace=workspace, defaults={"data": build(workspace, client, today), "as_of": today})
    return snap


def current(workspace: Workspace, today: date) -> tuple[DashboardSnapshot | None, bool]:
    """The last snapshot, and whether it is still fresh (built today, within the last few minutes)."""
    from django.utils import timezone

    snap = DashboardSnapshot.objects.filter(workspace=workspace).first()
    fresh = snap is not None and snap.as_of == today and timezone.now() - snap.built_at < timedelta(minutes=FRESH_MINUTES)
    return snap, fresh
