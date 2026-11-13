"""Cash: where the money is and what the bank feed still needs.

Balances are the books' (Campfire's balance sheet, by entity): operating bank accounts and cash at custodians. Bank
lines come from Campfire's bank feed. Each line posts an entry of cash against "Uncategorized" until someone says
what it was; categorizing a line here changes the account on that side of the entry (`transactions:write`).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from campfire_integration.client import CampfireClient
from gl.services import statements

BANK_ACCOUNT_PATH = "/ca/api/account"
BANK_TXN_PATH = "/ca/api/transaction"
LEDGER_PATH = statements.LEDGER_PATH
ZERO = Decimal(0)
LOOKBACK_DAYS = 90


class CashError(Exception):
    pass


@dataclass
class BankLine:
    id: int
    posted: date
    bank_account: str
    entity: str
    merchant: str
    amount: Decimal  # in > 0, out < 0
    gl_line_id: int | None = None  # the other side of the entry the line posted
    category: str = ""  # "" while uncategorized
    suggestion: dict[str, Any] | None = None  # {"account": id, "label": ...}: what this payee was last categorized as
    hint: str = ""  # e.g. looks like a fund distribution

    @property
    def categorized(self) -> bool:
        return bool(self.category)


@dataclass
class EntityCash:
    entity: str
    operating: Decimal = ZERO
    custody: Decimal = ZERO
    bank_accounts: list[str] = field(default_factory=list)
    to_categorize: int = 0

    @property
    def total(self) -> Decimal:
        return self.operating + self.custody


@dataclass
class Cash:
    as_of: date
    entities: list[EntityCash]
    lines: list[BankLine]
    scope_label: str = ""

    @property
    def operating(self) -> Decimal:
        return sum((e.operating for e in self.entities), ZERO)

    @property
    def custody(self) -> Decimal:
        return sum((e.custody for e in self.entities), ZERO)

    @property
    def waiting(self) -> list[BankLine]:
        return [ln for ln in self.lines if not ln.categorized]

    @property
    def money_in(self) -> Decimal:
        return sum((ln.amount for ln in self.lines if ln.amount > 0), ZERO)

    @property
    def money_out(self) -> Decimal:
        return -sum((ln.amount for ln in self.lines if ln.amount < 0), ZERO)


def _all(client: CampfireClient, path: str, params: dict[str, Any] | None = None, cap: int = 2000) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    query: dict[str, Any] | None = dict(params or {}, limit=200)
    while path and len(items) < cap:
        page = client.get_json(path, params=query)
        if isinstance(page, list):
            return page
        items += page.get("results", [])
        nxt = page.get("next")
        path, query = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)
    return items


def _dec(v: Any) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def _balances(client: CampfireClient, as_of: date) -> tuple[dict[str, EntityCash], str]:
    """Cash on the books by entity: one balance sheet per consolidated group, split by entity."""
    out: dict[str, EntityCash] = {}
    groups = [s for s in statements.scopes(client) if s.rollup]
    for scope in groups:
        data = client.get_json(statements.BALANCE_SHEET_PATH, params={**scope.params(), "start_date": as_of.replace(day=1).isoformat(),
                                                                         "end_date": as_of.isoformat(), "group_by": "entity"})
        key = next(iter(sorted(data.get("headers", {}), reverse=True)), None)
        if key is None:
            continue
        for row in data.get("balance_sheet", []):
            if row.get("account_subtype") != "BANK" or not str((row.get("hierarchy") or [""])[-1]).isdigit():
                continue
            custody = "custod" in str(row.get("account_name", "")).lower()
            for entity in data["headers"][key]:
                amount = _dec(row.get(f"{key}_{entity}"))
                if entity == "Total" or not amount:
                    continue
                e = out.setdefault(entity, EntityCash(entity))
                if custody:
                    e.custody += amount
                else:
                    e.operating += amount
    return out, (groups[0].label if len(groups) == 1 else "")


def overview(client: CampfireClient, today: date, since: date | None = None) -> Cash:
    since = since or today - timedelta(days=LOOKBACK_DAYS)
    entities, scope_label = _balances(client, today)
    accounts = {a["id"]: a for a in _all(client, BANK_ACCOUNT_PATH) if not a.get("is_deleted")}
    for a in accounts.values():
        entities.setdefault(a.get("entity_name") or "", EntityCash(a.get("entity_name") or "")).bank_accounts.append(a.get("name") or "")

    # The entry each line posted: the side that isn't the bank account says what the line was.
    other_side: dict[int, dict[str, Any]] = {}
    learned: dict[str, dict[str, Any]] = {}  # payee -> the account it was last categorized as
    for t in _all(client, LEDGER_PATH, {"has_parent_bank_transaction": "true", "start_date": since.isoformat(), "end_date": today.isoformat()}):
        if t.get("is_deleted") or t.get("account_subtype") == "BANK":
            continue
        other_side[int(t["parent_bank_transaction"])] = t
    lines: list[BankLine] = []
    for t in _all(client, BANK_TXN_PATH):
        posted = date.fromisoformat(str(t.get("posted_at"))[:10]) if t.get("posted_at") else None
        if t.get("is_deleted") or t.get("excluded") or posted is None or posted < since:
            continue
        account = accounts.get(t.get("account"), {})
        other = other_side.get(int(t["id"]))
        categorized = other is not None and other.get("account_type") != "UNCATEGORIZED"
        merchant = t.get("merchant_name") or t.get("bank_description") or ""
        line = BankLine(int(t["id"]), posted, t.get("account_name") or account.get("name") or "", t.get("entity_name") or account.get("entity_name") or "",
                        merchant, _dec(t.get("amount")), int(other["id"]) if other else None, str(other.get("account_name") or "") if categorized else "")
        if categorized and merchant:
            learned.setdefault(merchant.lower(), {"account": other["account"], "label": other.get("account_name") or ""})  # type: ignore[index]
        lines.append(line)
    lines.sort(key=lambda ln: (ln.posted, ln.id), reverse=True)
    for line in lines:
        if not line.categorized:
            line.suggestion = learned.get(line.merchant.lower())
            if line.entity in entities:
                entities[line.entity].to_categorize += 1
    ordered = sorted(entities.values(), key=lambda e: -e.total)
    return Cash(today, ordered, lines, scope_label)


def categorize(client: CampfireClient, gl_line_id: int, account_id: int, vendor_id: int | None = None) -> str:
    """Say what a bank line was: put its entry's uncategorized side on a GL account (and payee). Returns what happened."""
    body: dict[str, Any] = {"account": account_id}
    if vendor_id:
        body["vendor"] = vendor_id
    r = client.request("PATCH", f"{LEDGER_PATH}/{gl_line_id}", json=body)
    if r.status_code == 202:
        return "sent for approval"
    if r.status_code in (401, 403):
        raise CashError("The connection doesn't allow categorizing yet. Reconnect the workspace so the app gets permission to edit ledger lines.")
    if r.status_code >= 400:
        raise CashError(f"Campfire refused it ({r.status_code}): {r.text[:300]}")
    return "categorized"


def by_month(lines: list[BankLine]) -> list[tuple[str, Decimal, Decimal]]:
    """Money in and out by month, oldest first."""
    months: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    for ln in lines:
        months[f"{ln.posted:%Y-%m}"][0 if ln.amount > 0 else 1] += abs(ln.amount)
    return [(k, v[0], v[1]) for k, v in sorted(months.items())]
