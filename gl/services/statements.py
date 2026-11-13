"""Financial statements from Campfire's books, shaped for the app's pages: balance sheet, income statement, cash
flow and trial balance, for one entity or a consolidated group, in one column or by month or quarter, with the ledger
lines behind any account.

Campfire computes the statements (hierarchy, net income, consolidation, eliminations); this module asks for them
(`reports:read`, `transactions:read`) and puts the rows in a family office's reading order.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from campfire_integration.client import CampfireClient

BALANCE_SHEET_PATH = "/ca/api/get_balance_sheet"
INCOME_STATEMENT_PATH = "/ca/api/get_income_statement"
CASH_FLOW_PATH = "/ca/api/get_cash_flow"
TRIAL_BALANCE_PATH = "/ca/api/get_trial_balance"
LEDGER_PATH = "/coa/api/transaction"
ENTITY_PATH = "/coa/api/entity"
ZERO = Decimal(0)
VIEWS = {"bs": "Balance sheet", "is": "Income statement", "cf": "Cash flow", "tb": "Trial balance"}
COLUMNS = {"total": "One column", "monthly": "By month", "quarterly": "By quarter"}
PERIODS = {"ytd": "This year to date", "month": "Last month", "quarter": "Last quarter", "year": "Last year", "custom": "Custom dates"}


@dataclass
class Row:
    label: str
    values: list[Decimal | None]
    kind: str = "account"  # account | section | total | grand
    number: str = ""
    level: int = 0
    account_id: int | None = None  # set on account rows: the ledger lines can be opened


@dataclass
class Statement:
    title: str
    columns: list[str]
    rows: list[Row] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class Scope:
    """Whose books: one Campfire entity, or a parent with everything under it (consolidated)."""

    entity_id: int
    rollup: bool
    label: str

    @property
    def value(self) -> str:
        return f"{self.entity_id}{'r' if self.rollup else ''}"

    def params(self) -> dict[str, Any]:
        return {"entity": self.entity_id, **({"entity_rollup": "true"} if self.rollup else {})}


def scopes(client: CampfireClient) -> list[Scope]:
    """What the statements can be run for: consolidated groups first, then each active entity."""
    data = client.get_json(ENTITY_PATH)
    entities = [e for e in (data if isinstance(data, list) else data.get("results", [])) if not e.get("is_deleted") and e.get("active", True)]
    parents = {e.get("parent") for e in entities if e.get("parent")}
    groups = [Scope(int(e["id"]), True, f"{e['name']}: all entities") for e in entities if e["id"] in parents]
    singles = [Scope(int(e["id"]), False, e["name"]) for e in sorted(entities, key=lambda e: (bool(e.get("is_elimination_entity")), e["name"]))
               if e["id"] not in parents]
    return groups + singles


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def period_dates(period: str, today: date, start: str = "", end: str = "") -> tuple[date, date]:
    if period == "month":
        last = month_start(today) - timedelta(days=1)
        return month_start(last), last
    if period == "quarter":
        first_of_this = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        last = first_of_this - timedelta(days=1)
        return date(last.year, 3 * ((last.month - 1) // 3) + 1, 1), last
    if period == "year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if period == "custom":
        try:
            a, b = date.fromisoformat(start), date.fromisoformat(end)
            if a <= b:
                return a, b
        except ValueError:
            pass
    return date(today.year, 1, 1), today


def _dec(v: Any) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def _label(key: str, cadence: str, as_of: bool, end: date) -> str:
    start = date.fromisoformat(key)
    if cadence == "quarterly":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if as_of:  # a balance: say the date it is at
        return f"{min(month_end(start), end):%b %-d, %Y}"
    return f"{start:%b %Y}"


def _fetch(client: CampfireClient, path: str, scope: Scope, start: date, end: date, columns: str) -> tuple[dict[str, Any], list[str], str]:
    cadence = "quarterly" if columns == "quarterly" else "monthly"
    data = client.get_json(path, params={**scope.params(), "start_date": start.isoformat(), "end_date": end.isoformat(), "cadence": cadence})
    keys = sorted(k for k in data.get("headers", {}) if k != "Total")
    return data, keys, cadence


def _account_rows(rows: list[dict[str, Any]], prefix: list[str], value: Any) -> list[Row]:
    """The account rows under a hierarchy prefix, by account number, indented by their depth under it."""
    found = [r for r in rows if r.get("hierarchy", [])[:len(prefix)] == prefix and len(r.get("hierarchy", [])) > len(prefix)
             and str(r["hierarchy"][-1]).isdigit()]
    found.sort(key=lambda r: (str(r.get("account_number") or "~"), str(r.get("account_name"))))
    return [Row(r.get("display_name") or r.get("account_name") or "", value(r), "account", str(r.get("account_number") or ""),
                len(r["hierarchy"]) - len(prefix), int(r["hierarchy"][-1])) for r in found]


def _group(rows: list[dict[str, Any]], hierarchy: list[str]) -> dict[str, Any]:
    return next((r for r in rows if r.get("hierarchy") == hierarchy), {})


def _nonzero(row: Row) -> bool:
    return any(v for v in row.values)


def balance_sheet(client: CampfireClient, scope: Scope, start: date, end: date, columns: str) -> Statement:
    if columns == "total":
        start = month_start(end)  # one column: the balances on the end date
    data, keys, cadence = _fetch(client, BALANCE_SHEET_PATH, scope, start, end, columns)
    rows = data.get("balance_sheet", [])
    value = lambda r: [_dec(r.get(f"{k}_Total")) for k in keys]  # noqa: E731
    le = "LIABILITIES_AND_EQUITY"
    out = Statement("Balance sheet", [_label(k, cadence, True, end) for k in keys])
    add = out.rows.append
    add(Row("Assets", [None] * len(keys), "section"))
    out.rows += _account_rows(rows, ["ASSET"], value)
    add(Row("Total assets", value(_group(rows, ["ASSET"])), "total"))
    add(Row("Liabilities", [None] * len(keys), "section"))
    out.rows += _account_rows(rows, [le, "LIABILITY"], value)
    add(Row("Total liabilities", value(_group(rows, [le, "LIABILITY"])), "total"))
    add(Row("Equity", [None] * len(keys), "section"))
    out.rows += _account_rows(rows, [le, "EQUITY"], value)
    for key, label in (("RETAINED_EARNINGS", "Retained earnings (prior years)"), ("NET_INCOME", "Net income, year to date")):
        extra = Row(label, value(_group(rows, [le, "EQUITY", key])), "account", level=1)
        if _nonzero(extra):
            add(extra)
    add(Row("Total equity", value(_group(rows, [le, "EQUITY"])), "total"))
    add(Row("Total liabilities and equity", value(_group(rows, [le])), "grand"))
    uncategorized = Row("Uncategorized (bank lines waiting to be categorized)", value(_group(rows, ["UNCATEGORIZED_TOTAL"])), "account")
    if _nonzero(uncategorized):
        add(uncategorized)
        out.note = "Uncategorized bank lines are not in the totals above until they are categorized."
    return out


def income_statement(client: CampfireClient, scope: Scope, start: date, end: date, columns: str) -> Statement:
    data, keys, cadence = _fetch(client, INCOME_STATEMENT_PATH, scope, start, end, columns)
    rows = data.get("income_statement", [])
    show = [] if columns == "total" else keys
    value = lambda r: [_dec(r.get(f"{k}_Total")) for k in show] + [_dec(r.get("Total"))]  # noqa: E731
    out = Statement("Income statement", [_label(k, cadence, False, end) for k in show] + ["Total"])
    blank: list[Decimal | None] = [None] * (len(show) + 1)
    sections = (("REVENUE", "Revenue", "Total revenue"), ("COGS", "Cost of sales", "Total cost of sales"),
                ("OTHER_INCOME", "Investment and other income", "Total investment and other income"),
                ("OPERATING_EXPENSES", "Expenses", "Total expenses"), ("OTHER_EXPENSE", "Interest, taxes and other", "Total interest, taxes and other"))
    for key, title, total in sections:
        accounts = [r for r in _account_rows(rows, [key], value) if _nonzero(r)]
        if not accounts:
            continue
        out.rows.append(Row(title, blank, "section"))
        out.rows += accounts
        out.rows.append(Row(total, value(_group(rows, [key])), "total"))
    out.rows.append(Row("Net income", value(_group(rows, ["NET_INCOME"])), "grand"))
    return out


def cash_flow(client: CampfireClient, scope: Scope, start: date, end: date, columns: str) -> Statement:
    data, keys, cadence = _fetch(client, CASH_FLOW_PATH, scope, start, end, columns)
    show = [] if columns == "total" else keys
    out = Statement("Cash flow", [_label(k, cadence, False, end) for k in show] + ["Total"])
    labels = {"OPERATING": "Cash from operations", "INVESTING": "Cash from investing", "FINANCING": "Cash from financing",
              "TOTAL_NET_CASH_FLOW": "Net change in cash", "CASH_BALANCE_BEGINNING_PERIOD": "Cash at the start",
              "CASH_BALANCE_ENDING_PERIOD": "Cash at the end"}
    for r in data.get("cash_flow", []):
        hierarchy = r.get("hierarchy") or []
        activity = r.get("transaction_activity") or {}
        values: list[Decimal | None] = [_dec(activity.get(k)) for k in show] + [_dec(r.get("Total"))]
        if len(hierarchy) == 1:
            kind = "grand" if hierarchy[0].startswith(("TOTAL", "CASH_BALANCE_ENDING")) else "total"
            out.rows.append(Row(labels.get(hierarchy[0], r.get("account_name") or ""), values, kind))
        elif hierarchy[-1] != "OPERATING_CASH_FLOW" or any(values):  # Campfire's empty "adjustments" line
            account_id = int(hierarchy[-1]) if str(hierarchy[-1]).isdigit() else None
            out.rows.append(Row(r.get("account_name") or "", values, "account", str(r.get("account_number") or ""), len(hierarchy) - 1, account_id))
    out.note = "Indirect method, as Campfire classifies each account's cash flow."
    return out


def trial_balance(client: CampfireClient, scope: Scope, start: date, end: date) -> Statement:
    data = client.get_json(TRIAL_BALANCE_PATH, params={**scope.params(), "start_date": start.isoformat(), "end_date": end.isoformat()})
    out = Statement("Trial balance", ["Opening", "Debits", "Credits", "Closing debit", "Closing credit"])

    def values(a: dict[str, Any]) -> list[Decimal | None]:
        b, act, e = a.get("beginning_balances") or {}, a.get("activity") or {}, a.get("ending_balances") or {}
        return [_dec(b.get("debits")) - _dec(b.get("credits")), _dec(act.get("debits")), _dec(act.get("credits")), _dec(e.get("debits")), _dec(e.get("credits"))]

    accounts = [a for a in data.get("trial_balance", {}).get("accounts", []) if a.get("id") != "total"]
    for a in sorted(accounts, key=lambda a: (str(a.get("number") or "~"), str(a.get("name")))):
        row = Row(a.get("name") or "", values(a), "account", str(a.get("number") or ""), 0, int(a["id"]) if str(a.get("id")).isdigit() else None)
        if _nonzero(row):
            out.rows.append(row)
    total = next((a for a in data.get("trial_balance", {}).get("accounts", []) if a.get("id") == "total"), None)
    if total:
        out.rows.append(Row("Total", [None, *values(total)[1:]], "grand"))
    return out


def build(client: CampfireClient, view: str, scope: Scope, start: date, end: date, columns: str) -> Statement:
    if view == "is":
        return income_statement(client, scope, start, end, columns)
    if view == "cf":
        return cash_flow(client, scope, start, end, columns)
    if view == "tb":
        return trial_balance(client, scope, start, end)
    return balance_sheet(client, scope, start, end, columns)


@dataclass
class LedgerLine:
    posted: str
    entity: str
    memo: str
    description: str
    journal: str
    kind: str
    debit: Decimal
    credit: Decimal
    balance: Decimal | None


def ledger(client: CampfireClient, account_id: int, scope: Scope, start: date, end: date, limit: int = 500) -> tuple[str, list[LedgerLine], int]:
    """The ledger lines of one account in the period: (account label, lines oldest first, how many there are in all)."""
    params: dict[str, Any] | None = {**scope.params(), "account": account_id, "start_date": start.isoformat(), "end_date": end.isoformat(),
                                     "limit": 200, "ordering": "posted_at"}
    path, lines, total, label = LEDGER_PATH, [], 0, ""
    while path and len(lines) < limit:
        page = client.get_json(path, params=params)
        total = page.get("count", total)
        for t in page.get("results", []):
            if t.get("is_deleted"):
                continue
            label = label or str(t.get("account_name") or "")
            lines.append(LedgerLine(str(t.get("posted_at") or "")[:10], t.get("entity_name") or "", t.get("journal_memo") or "",
                                    t.get("bank_description") or t.get("description") or "", str(t.get("journal_order") or t.get("journal") or ""),
                                    t.get("journal_type_name") or "", _dec(t.get("debit_amount")), _dec(t.get("credit_amount")),
                                    _dec(t["balance_after_transaction"]) if t.get("balance_after_transaction") is not None and not scope.rollup else None))
        nxt = page.get("next")
        path, params = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)
    lines.sort(key=lambda ln: (ln.posted, ln.journal))
    return label, lines, total
