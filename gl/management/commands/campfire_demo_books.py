"""Give the Alder demo entities books to operate on in a Campfire sandbox (fictional figures):

    python manage.py campfire_demo_books                # opening balances, this year's operating activity,
                                                        # operating bank accounts with recent lines, open bills
    python manage.py campfire_demo_books --dry-run      # show what would be created
    python manage.py campfire_demo_books --teardown     # remove what this command created

Run `campfire_demo_setup` first (entities, chart, payees) and the app's GL setup (investment accounts). Everything is
created with the admin API key and marked with the source "Alder demo books", so it can be found and removed.

The opening entry (last day of the previous year) carries, per entity, the custody cash and the investments at cost
that the app's investment book shows on that date, so with "Post activity from" set to January 1 the GL bridge's
entries continue from it and the tie-out ties. The rest is what a family office books outside the investment book:
operating cash, a mortgage, a note receivable, art, related-party balances, rent, fees, taxes, grants and distributions.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from campfire_integration.client import ApiKeyClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from entities.models import Family
from gl.services.calls import BILL_PATH, VENDOR_PATH, _terms
from gl.services.posting import JE_PATH, month_end
from gl.services.setup import ACCOUNT_PATH, ENTITY_PATH
from investments.models import Account, Instrument
from investments.services.ledger import replay

SOURCE = "Alder demo books"
BANK_ACCOUNT_PATH = "/ca/api/account"
BANK_FEED_PATH = "/ca/api/bank-feed"
BANK_TXN_PATH = "/ca/api/transaction"
REASON = {"reason": "Alder demo books teardown"}
ZERO, CENT = Decimal(0), Decimal("0.01")

# Account names in the family-office chart (campfire_demo_setup) and the app's GL setup.
CASH, CUSTODY, INVESTMENTS = "Operating cash - family entities", "Cash held at custodians", "Investments at cost"
DUE_FROM, DUE_TO, PREPAID, EST_TAX = "Due from related entities", "Due to related entities", "Prepaid insurance", "Estimated tax payments"
ART, NOTES, MORTGAGE = "Art and collectibles", "Notes receivable - related parties", "Mortgage payable"
CORPUS, PARTNERS, MEMBERS, CONTRIBUTIONS, DISTRIBUTIONS = ("Trust principal (corpus)", "Partners' capital", "Members' capital",
                                                           "Contributions from grantors and owners", "Distributions to beneficiaries and owners")
RENT, FEE_INCOME, INTEREST_INCOME = "Rental income", "Management fee income - related parties", "Interest income"
TRUSTEE, ADVISORY, LEGAL, TAX_PREP, INSURANCE, PROPERTY, BANK_FEES, RELATED_FEES, GRANTS, INTEREST = (
    "Trustee fees", "Professional and advisory fees", "Legal fees", "Tax preparation and accounting fees", "Insurance expense",
    "Property taxes and maintenance", "Bank and custody fees", "Management fees - related party", "Charitable grants", "Interest expense")

RRT, ERT, DYNASTY, GST = "Robert Alder Revocable Trust", "Eleanor Alder Revocable Trust", "Alder 2012 Dynasty Trust", "Claire Moreno 2019 GST Trust"
MGMT, LP, ARE, FOUNDATION = "Alder Management LLC", "Alder Family Investments LP", "Alder Real Estate Holdings LLC", "Alder Family Foundation"
EQUITY = {RRT: CORPUS, ERT: CORPUS, DYNASTY: CORPUS, GST: CORPUS, MGMT: MEMBERS, LP: PARTNERS, ARE: MEMBERS, FOUNDATION: CONTRIBUTIONS}

# What each entity holds outside the investment book on the opening date (debit positive).
OPENING: dict[str, list[tuple[str, int]]] = {
    RRT: [(CASH, 650_000), (NOTES, 500_000)],
    ERT: [(CASH, 520_000), (ART, 1_250_000)],
    DYNASTY: [(CASH, 260_000)],
    GST: [(CASH, 85_000)],
    MGMT: [(CASH, 240_000), (DUE_FROM, 60_000)],
    LP: [(CASH, 780_000), (DUE_TO, -30_000)],
    ARE: [(CASH, 365_000), (PREPAID, 18_000), (MORTGAGE, -4_200_000), (DUE_TO, -30_000)],
    FOUNDATION: [(CASH, 125_000)],
}
MORTGAGE_RATE, MORTGAGE_PAYMENT = Decimal("0.0495"), Decimal(23_400)
GRANTS_BY_MONTH = [18_000, 12_000, 25_000, 15_000, 30_000, 12_000, 20_000, 15_000, 22_000, 18_000, 25_000, 30_000]
Line = tuple[str, Decimal, str]  # (account name, amount: debit positive, description)


def opening_lines(entity: str, custody_cash: Decimal, cost: Decimal) -> list[Line]:
    lines: list[Line] = [(name, Decimal(amount), "Opening balance") for name, amount in OPENING[entity]]
    if custody_cash:
        lines.append((CUSTODY, custody_cash, "Opening balance: cash at custodians (investment book)"))
    if cost:
        lines.append((INVESTMENTS, cost, "Opening balance: investments at cost (investment book)"))
    lines.append((EQUITY[entity], -sum((a for _, a, _ in lines), ZERO), "Opening balance"))
    return lines


def month_lines(entity: str, month: int, mortgage_balance: Decimal = ZERO) -> list[Line]:
    """One month's operating activity for an entity. Everything settles through operating cash, the last line."""
    q_end, lines = month in (3, 6, 9, 12), []
    add = lambda name, amount, text: lines.append((name, Decimal(amount), text))  # noqa: E731
    if entity == ARE:
        interest = (mortgage_balance * MORTGAGE_RATE / 12).quantize(CENT)
        add(RENT, -62_000, "Rent, Harbor Street tenants")
        add(PROPERTY, 3_100, "Seabright Property Management: management fee (5% of rent)")
        add(INTEREST, interest, "Mortgage interest")
        add(MORTGAGE, MORTGAGE_PAYMENT - interest, "Mortgage principal")
        add(INSURANCE, 1_500, "Property insurance for the month")
        add(PREPAID, -1_500, "Property insurance for the month")
        if month in (4, 10):
            add(PROPERTY, 41_500, "County property tax, half-year instalment")
    if entity in (ARE, LP) and q_end:
        add(RELATED_FEES, 15_000, "Alder Management LLC: quarterly management fee")
    if entity == MGMT:
        add(ADVISORY, 8_500, "Family office staff and contractors")
        add(BANK_FEES, 150, "Bank fees")
        if q_end:
            add(FEE_INCOME, -30_000, "Quarterly management fees from Alder Real Estate Holdings and Alder Family Investments")
    if entity == LP:
        if month == 3:
            add(TAX_PREP, 18_500, "Marlowe Finch CPAs: partnership return and K-1s")
        if month == 6:
            add(LEGAL, 6_200, "Hollis & Park LLP: side letter review")
    if entity == RRT:
        add(DISTRIBUTIONS, 25_000, "Monthly distribution to Robert Alder")
        if month in (1, 4, 6, 9):
            add(EST_TAX, 40_000, "Estimated income tax payment")
        if q_end:
            add(ADVISORY, 9_500, "Granite Peak Advisors: quarterly advisory fee")
        if month in (6, 12):
            add(INTEREST_INCOME, -12_500, "Interest on the note receivable")
    if entity == ERT:
        add(DISTRIBUTIONS, 20_000, "Monthly distribution to Eleanor Alder")
        if month == 2:
            add(INSURANCE, 14_000, "Harborline Insurance Brokers: fine art policy")
        if q_end:
            add(ADVISORY, 7_000, "Granite Peak Advisors: quarterly advisory fee")
    if entity == DYNASTY:
        if q_end:
            add(TRUSTEE, 11_000, "Northwind Trust Company: quarterly trustee fee")
        if month == 4:
            add(TAX_PREP, 6_500, "Marlowe Finch CPAs: fiduciary return")
        if month in (6, 12):
            add(DISTRIBUTIONS, 50_000, "Distribution to beneficiaries")
    if entity == GST:
        if month == 2:
            add(LEGAL, 2_400, "Hollis & Park LLP: trust administration")
        if month == 4:
            add(TAX_PREP, 3_800, "Marlowe Finch CPAs: fiduciary return")
    if entity == FOUNDATION:
        add(GRANTS, GRANTS_BY_MONTH[month - 1], "Grants paid")
        if month == 1:
            add(CONTRIBUTIONS, -300_000, "Annual contribution from Robert and Eleanor Alder")
        if month == 5:
            add(TAX_PREP, 5_200, "Marlowe Finch CPAs: Form 990-PF")
    if lines:
        lines.append((CASH, -sum((a for _, a, _ in lines), ZERO), "Operating account"))
    return lines


# Recent bank lines for the operating accounts, still to be categorized: (entity, days ago, amount: in > 0, merchant).
BANK_LINES = [
    (ARE, 11, 62_000, "HARBOR STREET TENANTS ACH RENT"), (ARE, 9, -3_100, "SEABRIGHT PROPERTY MGMT"), (ARE, 4, -1_850, "COASTLINE PLUMBING"),
    (RRT, 10, -25_000, "TRANSFER TO R ALDER PERSONAL"), (RRT, 3, -480, "HARBORLINE INS BROKERS"),
    (LP, 6, 120_000, "WIRE IN SUMMIT GROWTH PARTNERS IV DISTRIBUTION"),
    (FOUNDATION, 8, -15_000, "GRANT RIVERSIDE FOOD BANK"), (FOUNDATION, 2, -7_500, "GRANT CITY YOUTH ORCHESTRA"),
    (MGMT, 12, -8_500, "PAYROLL SERVICE"), (MGMT, 1, -150, "MONTHLY SERVICE FEE"),
]
# Open bills: (entity, payee, bill number, description, account, amount, billed days ago, due in days).
BILLS = [
    (DYNASTY, "Marlowe Finch CPAs", "MF-2026-1184", "Q3 fiduciary accounting", TAX_PREP, 4_800, 12, 18),
    (RRT, "Hollis & Park LLP", "HP-88213", "Trust amendment and estate planning review", LEGAL, 7_250, 9, 21),
    (ARE, "Harborline Insurance Brokers", "HIB-30077", "Property and umbrella renewal, 2027 policy year", PREPAID, 21_600, 5, 25),
    (ARE, "Seabright Property Management", "SPM-4410", "Roof repair, Harbor Street", PROPERTY, 12_400, 3, 12),
    (DYNASTY, "Northwind Trust Company", "NTC-Q4-2026", "Q4 trustee fee", TRUSTEE, 11_000, 2, 28),
    (ERT, "Granite Peak Advisors", "GPA-2026-Q4-ERT", "Q4 advisory fee", ADVISORY, 7_000, 6, 9),
    (FOUNDATION, "Bayview Securities", "BVS-C-90112", "Custody fee, third quarter", BANK_FEES, 1_150, 35, -5),
    (FOUNDATION, "Marlowe Finch CPAs", "MF-2026-1201", "Form 990-PF extension work", TAX_PREP, 2_300, 15, 15),
]


def _all(client: ApiKeyClient, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    query: dict[str, Any] | None = dict(params or {}, limit=200)
    while path:
        page = client.get_json(path, params=query)
        if isinstance(page, list):
            return page
        items += page.get("results", [])
        nxt = page.get("next")
        path, query = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)
    return items


def investment_book(as_of: date) -> dict[str, tuple[Decimal, Decimal]]:
    """Entity name -> (cash at custodians, investments at cost) in the app's investment book on a date. Uses the Alder
    family in this database, or seeds one for the calculation and rolls it back (the demo is the same on any one day)."""
    out: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    with transaction.atomic():
        family = Family.objects.filter(code="alder").first()
        if family is None:
            from entities.demo import seed_alder
            from investments.demo import seed_alder_investments

            family = seed_alder(Workspace.objects.create(customer_id=-1, name="Alder demo books (temporary)"))
            seed_alder_investments(family, timezone.localdate())
            transaction.set_rollback(True)
        for account in Account.objects.filter(family=family, entity__isnull=False).select_related("entity"):
            state = replay(account, as_of)
            if account.tracks_cash:
                out[account.entity.name][0] += state.cash  # type: ignore[union-attr]
            for instrument_id in Instrument.objects.in_bulk(state.instrument_ids()):
                out[account.entity.name][1] += state.cost_basis(instrument_id)  # type: ignore[union-attr]
    return {name: (cash.quantize(CENT), cost.quantize(CENT)) for name, (cash, cost) in out.items()}


class Command(BaseCommand):
    help = "Create (or remove) demo books for the Alder entities in a Campfire sandbox: opening balances, activity, bank lines, bills."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--teardown", action="store_true", help="Delete the entries, bills and bank accounts this command created")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        try:
            self.client = ApiKeyClient()
        except NotConnected as e:
            raise CommandError(str(e)) from e
        self.dry = opts["dry_run"]
        try:
            self.teardown() if opts["teardown"] else self.setup()
        except CampfireError as e:
            raise CommandError(str(e)) from e

    def say(self, verb: str, what: str) -> None:
        style = {"created": self.style.SUCCESS, "create": self.style.SUCCESS, "deleted": self.style.WARNING, "failed": self.style.ERROR}.get(verb, str)
        self.stdout.write(style(f"  {verb:8} {what}"))

    def post(self, path: str, body: dict[str, Any], label: str) -> dict[str, Any] | None:
        if self.dry:
            self.say("create", label)
            return None
        r = self.client.request("POST", path, json=body)
        if r.status_code >= 400:
            self.say("failed", f"{label} ({r.status_code}: {r.text[:300]})")
            return None
        self.say("created", label)
        try:
            return r.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------------------------------------- setup

    def setup(self) -> None:
        today = timezone.localdate()
        opening = date(today.year - 1, 12, 31)
        entities = {e["name"]: e["id"] for e in _all(self.client, ENTITY_PATH) if not e.get("is_deleted")}
        accounts = {a["name"]: a["id"] for a in _all(self.client, ACCOUNT_PATH)}
        vendors = {v["name"]: v["id"] for v in _all(self.client, VENDOR_PATH)}
        missing = [n for n in EQUITY if n not in entities] + [n for n in (CASH, CUSTODY, INVESTMENTS, INTEREST_INCOME) if n not in accounts]
        if missing:
            raise CommandError("Not in Campfire yet (run campfire_demo_setup and the app's GL setup first): " + ", ".join(missing))
        # Listed without Campfire's `source` filter and with a changing parameter: a filtered list answered from a
        # stale cache once hid entries created minutes earlier, and a rerun then booked everything twice.
        journals = [j for j in _all(self.client, JE_PATH, {"all_time": "true", "_": timezone.now().timestamp()})
                    if j.get("source") == SOURCE and not j.get("is_deleted")]
        existing = {j.get("source_id") for j in journals}
        booked = {(str(j.get("entity")), str(j.get("date"))[:10], j.get("memo")) for j in journals}
        book = investment_book(opening)

        def entry(entity: str, when: date, memo: str, key: str, lines: list[Line]) -> None:
            source_id = f"alder-books-{key}"
            if source_id in existing or (str(entities[entity]), when.isoformat(), memo) in booked or not lines:
                return
            assert sum((a for _, a, _ in lines), ZERO) == ZERO, (entity, memo)
            body = {"date": when.isoformat(), "type": "journal_entry", "memo": memo, "entity": str(entities[entity]), "currency": "USD",
                    "source": SOURCE, "source_id": source_id, "ref_number": key.upper()[:30],
                    "transactions": [{"account": str(accounts[name]), **({"debit_amount_native": float(a)} if a > 0 else {"credit_amount_native": float(-a)}),
                                      "tags": [], "bank_description": text, "external_id": f"{source_id}-{n}"}
                                     for n, (name, a, text) in enumerate(lines, start=1) if a]}
            self.post(JE_PATH, body, f"{when} {entity}: {memo} ({sum((a for _, a, _ in lines if a > 0), ZERO):,.0f})")

        self.stdout.write(f"Opening balances ({opening}):")
        for n, entity in enumerate(EQUITY, start=1):
            cash, cost = book.get(entity, (ZERO, ZERO))
            entry(entity, opening, f"Opening balances, {opening:%B %-d, %Y}", f"open-{n}", opening_lines(entity, cash, cost))

        self.stdout.write(f"Operating activity, {today.year}:")
        mortgage = Decimal(-dict(OPENING[ARE])[MORTGAGE])
        for month in range(1, today.month):  # completed months
            when = month_end(date(today.year, month, 1))
            for n, entity in enumerate(EQUITY, start=1):
                lines = month_lines(entity, month, mortgage)
                entry(entity, when, f"Operating activity, {when:%B %Y}", f"{today.year}-{month:02d}-{n}", lines)
                if entity == ARE:
                    mortgage -= next(a for name, a, _ in lines if name == MORTGAGE)

        self.stdout.write("Operating bank accounts and recent lines:")
        banks = {b.get("name"): b for b in _all(self.client, BANK_ACCOUNT_PATH) if not b.get("is_deleted")}
        bank_lines = [t for t in _all(self.client, BANK_TXN_PATH) if not t.get("is_deleted")]
        for n, entity in enumerate(EQUITY, start=1):
            name = f"{entity} - Operating"
            bank = banks.get(name) or self.post(BANK_ACCOUNT_PATH, {"name": name, "nickname": name, "entity": entities[entity], "currency": "USD",
                                                                    "chart_of_accounts_account": accounts[CASH], "type": "CHECKING",
                                                                    "account_id": f"ALDER-OP-{n:02d}"}, f"bank account {name}")
            # Campfire tells lines apart by external_transaction_id; without one, only the first line of an account is kept.
            seen = {t.get("merchant_name") for t in bank_lines if bank and t.get("account") == bank.get("id")}
            feed = [{"posted_at": (today - timedelta(days=ago)).isoformat(), "amount": amount, "merchant_name": merchant,
                     "external_transaction_id": f"alder-books-{n}-{k}"}
                    for k, (e, ago, amount, merchant) in enumerate(BANK_LINES) if e == entity and merchant not in seen]
            if bank and bank.get("id") and feed:
                self.post(f"{BANK_FEED_PATH}/{bank['id']}", {"balance": 0, "available_balance": 0, "balance_as_of_date": today.isoformat(), "transactions": feed},
                          f"{len(feed)} bank lines for {entity}")

        self.stdout.write("Open bills:")
        billed = {b.get("source_id") for b in _all(self.client, BILL_PATH)}
        for entity, payee, number, text, account, amount, ago, due in BILLS:
            source_id = f"alder-books-bill-{number}"
            if source_id in billed:
                continue
            if payee not in vendors:
                self.say("failed", f"bill {number}: no vendor {payee}")
                continue
            bill_date, due_date = today - timedelta(days=ago), today + timedelta(days=due)
            self.post(BILL_PATH, {"bill_number": number, "bill_date": bill_date.isoformat(), "item_date": bill_date.isoformat(), "due_date": due_date.isoformat(),
                                  "terms": _terms(bill_date, due_date), "entity": str(entities[entity]), "vendor": str(vendors[payee]), "currency": "USD",
                                  "message_on_bill": text, "source": SOURCE, "source_id": source_id,
                                  "lines": [{"account": str(accounts[account]), "amount": float(amount), "description": text, "tags": []}]},
                      f"bill {number} {payee} -> {entity}: {text} ({amount:,})")

    # ---------------------------------------------------------------------------------------- teardown

    def remove(self, path: str, label: str) -> None:
        if self.dry:
            self.say("delete", label)
            return
        r = self.client.request("POST", path, json=REASON)
        self.say("deleted" if r.status_code < 400 else "failed", label + ("" if r.status_code < 400 else f" ({r.status_code}: {r.text[:200]})"))

    def teardown(self) -> None:
        for b in _all(self.client, BILL_PATH):
            if b.get("source") == SOURCE:
                self.remove(f"{BILL_PATH}/{b['id']}/delete", f"bill {b.get('bill_number')}")
        names = {f"{entity} - Operating" for entity in EQUITY}
        banks = [b for b in _all(self.client, BANK_ACCOUNT_PATH) if b.get("name") in names and not b.get("is_deleted")]
        bank_ids = {b["id"] for b in banks}
        for t in _all(self.client, BANK_TXN_PATH):
            if t.get("account") in bank_ids and not t.get("is_deleted"):
                self.remove(f"{BANK_TXN_PATH}/{t['id']}/delete", f"bank line {t.get('merchant_name')}")
        for j in _all(self.client, JE_PATH, {"all_time": "true"}):
            if j.get("source") == SOURCE:
                self.remove(f"{JE_PATH}/{j['id']}/delete", f"entry {j.get('date')} {j.get('memo')}")
        for b in banks:
            self.remove(f"{BANK_ACCOUNT_PATH}/{b['id']}/delete", f"bank account {b.get('name')}")
