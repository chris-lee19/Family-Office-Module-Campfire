"""Statement of Cash Receipts and Disbursements: where cash came from and where it went, on a cash basis.

Read from the ledger: every entry that moved a bank or custody cash account in the books, shown by the accounts on
its other side. Receipts and disbursements therefore add up to the change in the cash accounts, and the statement
opens and closes on the cash balances of the Statement of Net Worth (or the entity's balance sheet).

- Income and expense accounts are shown net in their own section (a refund reduces the expense).
- Assets, liabilities and equity are shown gross: money that came out of them is a receipt, money put in a
  disbursement (an investment sold and another bought are two lines).
- A bill that was paid is shown under what the bill was for, not under accounts payable.
- An adjustment that isn't cash but sits in a cash entry (insurance expensed out of a prepaid balance, depreciation)
  is left out: both of its lines.
- Movements with related entities and through clearing accounts are transfers, below receipts and disbursements.
- Bank lines nobody has categorized yet are their own line on each side.
"""

from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from campfire_integration.client import CampfireClient, CampfireError
from gl.services import statements as st
from gl.services.statements import ZERO, Row, Scope, Statement

TITLE = st.RECEIPTS_TITLE
BILL_PATH = "/coa/api/v1/bill"
ACCOUNT_PATH = "/coa/api/account"
LEDGER_CAP = 6000
BILL_LOOKUPS = 20  # bills read one by one when the list doesn't have them
RELATED_PARTY = ("due to", "due from", "intercompany")
NOT_CASH = ("prepaid", "depreciation", "amortization", "accrued")  # balance-sheet accounts that adjust income or expense without cash
INCOME = {"REVENUE", "OTHER_INCOME"}
EXPENSE = {"COGS", "OPERATING_EXPENSES", "OTHER_EXPENSE"}
IN, OUT, TRANSFER = "in", "out", "transfer"
UNCATEGORIZED = "uncat"
RECEIPTS = (("income", "Income"), ("assets", "From investments and other assets"), ("debt", "Borrowed"), ("owners", "Contributions"),
            ("other", "Other receipts"), (UNCATEGORIZED, "Bank lines not categorized yet"))
DISBURSEMENTS = (("expenses", "Expenses"), ("assets", "Into investments and other assets"), ("debt", "Repaid on loans and other liabilities"),
                 ("bills", "Bills paid"), ("owners", "Distributions"), ("other", "Other disbursements"), (UNCATEGORIZED, "Bank lines not categorized yet"))

Key = tuple[str, str, int | None]  # account number, name, id


def _ledger(client: CampfireClient, scope: Scope, start: date, end: date) -> tuple[list[dict[str, Any]], bool]:
    """Every ledger line of the scope in the period, and whether the ledger was too long to read in full."""
    items: list[dict[str, Any]] = []
    path, query, total = st.LEDGER_PATH, {**scope.params(), "start_date": start.isoformat(), "end_date": end.isoformat(), "limit": 200}, 0
    while path and len(items) < LEDGER_CAP:
        page = client.get_json(path, params=query)
        total = int(page.get("count") or 0)
        items += page.get("results", [])
        nxt = page.get("next")
        path, query = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)  # type: ignore[assignment]
    return [t for t in items if not t.get("is_deleted")], total > len(items)


def _cash(data: dict[str, Any], key: str) -> Decimal:
    """The cash accounts' balance in one column of a balance sheet (accounts of subtype BANK, as on the Cash page)."""
    return sum((st._dec(r.get(f"{key}_Total")) for r in data.get("balance_sheet", [])
                if r.get("account_subtype") == "BANK" and str((r.get("hierarchy") or [""])[-1]).isdigit()), ZERO)


def _name(t: dict[str, Any]) -> Key:
    name = str(t.get("account_name") or "")
    return str(t.get("account_number") or ""), name.partition(" - ")[2] or name, int(t["account"]) if str(t.get("account") or "").isdigit() else None


def _place(kind: str, subtype: str, name: str, account: int | None, amount: Decimal, clearing: frozenset[int]) -> tuple[str, str]:
    """Which side and group a line on the other side of a cash entry belongs to. `amount` > 0 brought cash in."""
    if account in clearing or any(w in name.lower() for w in RELATED_PARTY):
        return TRANSFER, ""
    side = IN if amount > 0 else OUT
    if kind.startswith("UNCATEGORIZED"):
        return side, UNCATEGORIZED
    if kind in INCOME:
        return IN, "income"
    if kind in EXPENSE:
        return OUT, "expenses"
    if kind == "ASSET":
        return side, "assets"
    if kind == "LIABILITY":
        if subtype == "ACCOUNTS_PAYABLE":
            return side, "bills" if side == OUT else "other"
        return side, "debt"
    if kind == "EQUITY":
        return side, "owners"
    return side, "other"


def _amount(t: dict[str, Any]) -> Decimal:
    return st._dec(t.get("credit_amount")) - st._dec(t.get("debit_amount"))  # a credit on the other side of a cash entry: cash came in


def _not_cash(entry: list[dict[str, Any]]) -> set[int]:
    """The lines of an entry that are an adjustment without cash: a prepaid, accrued or depreciation account against
    an income or expense line of exactly the opposite amount (their ledger line ids)."""
    gone: set[int] = set()
    for t in entry:
        if str(t.get("account_type")) not in ("ASSET", "LIABILITY") or not any(w in str(t.get("account_name") or "").lower() for w in NOT_CASH):
            continue
        match = next((u for u in entry if u.get("id") not in gone and u is not t and str(u.get("account_type")) in INCOME | EXPENSE
                      and _amount(u) and _amount(u) == -_amount(t)), None)
        if match is not None:
            gone |= {t.get("id"), match.get("id")}
    return gone


def _bill_id(t: dict[str, Any]) -> int | None:
    bill = t.get("bill")
    raw = bill.get("id") if isinstance(bill, dict) else bill
    return int(raw) if str(raw or "").isdigit() else None


def _bills(client: CampfireClient, wanted: set[int]) -> dict[int, list[dict[str, Any]]]:
    """The lines of the bills that were paid: from the bill list, then one by one for the few it doesn't have."""
    found: dict[int, list[dict[str, Any]]] = {}
    path, query = BILL_PATH, {"limit": 200}
    while path and len(found) < len(wanted):
        page = client.get_json(path, params=query)
        results = page if isinstance(page, list) else page.get("results", [])
        for b in results:
            if b.get("id") in wanted:
                found[int(b["id"])] = b.get("lines") or []
        nxt = None if isinstance(page, list) else page.get("next")
        path, query = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)  # type: ignore[assignment]
    for bill_id in sorted(wanted - set(found))[:BILL_LOOKUPS]:
        try:
            found[bill_id] = client.get_json(f"{BILL_PATH}/{bill_id}").get("lines") or []
        except CampfireError:
            continue
    return found


def _date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def _all(client: CampfireClient, path: str) -> list[dict[str, Any]]:
    """A whole list from Campfire (a plain list, or pages of results)."""
    items: list[dict[str, Any]] = []
    query: dict[str, Any] | None = {"limit": 200}
    while path and len(items) < 5000:
        page = client.get_json(path, params=query)
        if isinstance(page, list):
            return [a for a in page if str(a.get("id") or "").isdigit()]
        items += page.get("results", [])
        nxt = page.get("next")
        path, query = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)
    return [a for a in items if str(a.get("id") or "").isdigit()]


def build(client: CampfireClient, scope: Scope, start: date, end: date, columns: str, clearing: frozenset[int] = frozenset()) -> Statement:
    """`clearing`: GL accounts that only pass money between the family's own accounts (the investment clearing
    accounts); what moves through them is a transfer, like a movement with a related entity."""
    closing_data, keys, cadence = st._fetch(client, st.BALANCE_SHEET_PATH, scope, st.month_start(end) if columns == "total" else start, end, columns)
    before = start - timedelta(days=1)
    opening_data, opening_keys, _ = st._fetch(client, st.BALANCE_SHEET_PATH, scope, st.month_start(before), before, "total")
    keys = keys or [st.month_start(end).isoformat()]
    n = len(keys)
    firsts = [date.fromisoformat(k) for k in keys]
    column = lambda when: min(max(bisect_right(firsts, when) - 1, 0), n - 1)  # noqa: E731
    closing = [_cash(closing_data, k) for k in keys]
    opening = [_cash(opening_data, opening_keys[-1]) if opening_keys else ZERO, *closing[:-1]]

    lines, partial = _ledger(client, scope, start, end)
    journals: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    kinds: dict[int, str] = {}
    for t in lines:
        journals[t.get("journal") or f"line-{t.get('id')}"].append(t)
        if str(t.get("account") or "").isdigit():
            kinds[int(t["account"])] = str(t.get("account_type") or "")

    flows: dict[tuple[str, str], dict[Key, list[Decimal]]] = defaultdict(lambda: defaultdict(lambda: [ZERO] * n))
    brought = [ZERO] * n  # cash put on the books by an opening entry
    paid: list[tuple[int, int, Decimal, Key]] = []  # bill payments, to be shown under what the bill was for
    for entry in journals.values():
        bank = [t for t in entry if t.get("account_subtype") == "BANK"]
        when = _date(entry[0].get("posted_at"))
        if not bank or when is None:
            continue
        col = column(when)
        if "opening" in str(entry[0].get("journal_memo") or "").lower():
            brought[col] += sum((st._dec(t.get("debit_amount")) - st._dec(t.get("credit_amount")) for t in bank), ZERO)
            continue
        skip = _not_cash(entry)
        for t in entry:
            amount = _amount(t)
            if t.get("account_subtype") == "BANK" or not amount or t.get("id") in skip:
                continue
            key = _name(t)
            side, group = _place(str(t.get("account_type") or ""), str(t.get("account_subtype") or ""), key[1], key[2], amount, clearing)
            bill_id = _bill_id(t)
            if group == "bills" and bill_id is not None:
                paid.append((col, bill_id, -amount, key))
                continue
            flows[(side, group)][key][col] += amount if side != OUT else -amount

    untraced = False
    if paid:
        bills = _bills(client, {bill_id for _, bill_id, _, _ in paid})
        if any(str(ln.get("account") or "").isdigit() and int(ln["account"]) not in kinds for found in bills.values() for ln in found):
            for a in _all(client, ACCOUNT_PATH):
                kinds.setdefault(int(a["id"]), str(a.get("account_type") or ""))
        for col, bill_id, amount, key in paid:
            found = [ln for ln in bills.get(bill_id, []) if st._dec(ln.get("amount"))]
            weights = [st._dec(ln.get("amount")) for ln in found]
            if not found or not sum(weights, ZERO):
                untraced = True
                flows[(OUT, "bills")][key][col] += amount
                continue
            for ln, part in zip(found, st._spread(amount, weights)):
                account = int(ln["account"]) if str(ln.get("account") or "").isdigit() else None
                name = str(ln.get("account_name") or "")
                kind = kinds.get(account or 0, "")
                group = {"ASSET": "assets", "LIABILITY": "debt", "EQUITY": "owners"}.get(kind, "expenses")
                flows[(OUT, group)][(str(ln.get("account_number") or ""), name.partition(" - ")[2] or name, account)][col] += part

    single = columns == "total" or n == 1
    wide = lambda v: [v[0]] if single else [*v, sum(v, ZERO)]  # noqa: E731
    blank: list[Decimal | None] = [None] * (1 if single else n + 1)
    out = Statement(TITLE, ["Total"] if single else [st._label(k, cadence, False, end) for k in keys] + ["Total"])
    add = out.rows.append

    def block(title: str, side: str, groups: tuple[tuple[str, str], ...], total_label: str) -> list[Decimal]:
        add(Row(title, blank, "section"))
        total = [ZERO] * n
        for group, label in groups:
            accounts = sorted(((k, v) for k, v in flows.get((side, group), {}).items() if any(v)), key=lambda kv: (kv[0][0] or "~", kv[0][1]))
            if not accounts:
                continue
            subtotal = [sum((v[i] for _, v in accounts), ZERO) for i in range(n)]
            total = [a + b for a, b in zip(total, subtotal)]
            if group == UNCATEGORIZED:  # one line: the account behind it is always "Uncategorized"
                add(Row(label, wide(subtotal), "group", account_id=accounts[0][0][2] if len(accounts) == 1 else None))
                continue
            add(Row(label, wide(subtotal), "group"))
            for (number, name, account_id), v in accounts:
                add(Row(name, wide(v), "account", number, 2, account_id))
        add(Row(total_label, wide(total), "total"))
        return total

    receipts = block("Receipts", IN, RECEIPTS, "Total receipts")
    disbursements = block("Disbursements", OUT, DISBURSEMENTS, "Total disbursements")
    excess = [a - b for a, b in zip(receipts, disbursements)]
    add(Row("Excess of receipts over disbursements", wide(excess), "total"))

    transfers = sorted(((k, v) for k, v in flows.get((TRANSFER, ""), {}).items() if any(v)), key=lambda kv: (kv[0][0] or "~", kv[0][1]))
    moved = [sum((v[i] for _, v in transfers), ZERO) for i in range(n)]
    other = [closing[i] - opening[i] - excess[i] - moved[i] - brought[i] for i in range(n)]
    if transfers or any(brought) or any(abs(x) >= Decimal("0.01") for x in other):
        add(Row("Transfers and other changes", blank, "section"))
        for (number, name, account_id), v in transfers:
            add(Row(name, wide(v), "account", number, 1, account_id))
        if any(brought):
            add(Row("Opening balances brought onto the books", wide(brought), "account", level=1))
        if any(abs(x) >= Decimal("0.01") for x in other):
            add(Row("Other changes in cash", wide(other), "account", level=1))
        add(Row("Net change in cash", wide([c - o for c, o in zip(closing, opening)]), "total"))  # otherwise it is the excess above
    ends = lambda v, first: [v[0]] if single else [*v, v[0] if first else v[-1]]  # noqa: E731
    add(Row(f"Cash, {start:%b %-d, %Y}" if single else "Cash at the start", ends(opening, True), "account"))
    add(Row(f"Cash, {end:%b %-d, %Y}" if single else "Cash at the end", ends(closing, False), "grand"))

    out.note = "Cash basis: every entry that moved a bank or custody cash account in the books, shown by what was on its other side. "
    if flows.get((IN, UNCATEGORIZED)) or flows.get((OUT, UNCATEGORIZED)):
        out.note += "Categorize the remaining bank lines on the Cash page. "
    if paid:
        out.note += "Bills that were paid are shown under what each bill was for. " if not untraced else "Bills paid that couldn't be traced to their lines are under Bills paid. "
    if partial:
        out.note += "The ledger was too long to read in full: what couldn't be read is in Other changes in cash. "
    return out
