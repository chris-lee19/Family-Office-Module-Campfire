"""Set up a Campfire sandbox for the fictional Alder family (developer tooling; uses CAMPFIRE_API_KEY).

    python manage.py campfire_demo_setup --dry-run     # what would be created
    python manage.py campfire_demo_setup               # create what is missing (safe to run again)
    python manage.py campfire_demo_setup --teardown    # remove what this command created (by name)

Creates, matching by name so nothing is duplicated and nothing existing is touched:
- entities: "Alder Family (Consolidated)" with the eight Alder entities and an eliminations entity under it,
  named exactly as the demo seed names them, so the app's "Link by matching name" links them in one click;
- the family-office chart of accounts next to the investment accounts the app creates (operating cash, due
  to/from related entities, real estate, art, notes, capital calls payable, mortgage, equity by kind of entity,
  K-1 income, unrealized gain/loss, rental income, trustee/legal/tax/insurance/property/custody fees, grants,
  interest and taxes), on the suggested number or the next free one;
- payees: the demo funds' general partners (capital-call bills) and the usual family-office vendors.
The investment accounts and FO tags are created by the app itself (General ledger → Preview suggested setup).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from campfire_integration.client import ApiKeyClient, CampfireError, NotConnected
from gl.services.setup import ACCOUNT_PATH, ENTITY_PATH, _all, _norm, _post, create_planned, list_accounts, plan_accounts

VENDOR_PATH = "/coa/api/vendor"
PARENT = "Alder Family (Consolidated)"
ELIMINATIONS = "Alder Family - Eliminations"
# Must match entities/demo.py (tests/test_gl.py checks it).
ALDER_ENTITIES = [
    "Robert Alder Revocable Trust", "Eleanor Alder Revocable Trust", "Alder 2012 Dynasty Trust",
    "Claire Moreno 2019 GST Trust", "Alder Management LLC", "Alder Family Investments LP",
    "Alder Real Estate Holdings LLC", "Alder Family Foundation",
]
# The family-office chart next to the investment accounts the app creates (cash, clearing, investments at cost,
# dividend/interest/fund income, realized gain/loss, investment fees). (number, name, type, subtype); a taken
# number moves to the next free one, an account with the same name is left as it is.
OPERATING_ACCOUNTS = [
    # Assets
    ("1040", "Operating cash - family entities", "ASSET", "BANK"),
    ("1180", "Due from related entities", "ASSET", "OTHER_CURRENT_ASSET"),
    ("1250", "Estimated tax payments", "ASSET", "OTHER_CURRENT_ASSET"),
    ("1260", "Prepaid insurance", "ASSET", "PREPAID"),
    ("1600", "Real estate held for investment", "ASSET", "OTHER_ASSET"),
    ("1610", "Art and collectibles", "ASSET", "OTHER_ASSET"),
    ("1620", "Notes receivable - related parties", "ASSET", "OTHER_ASSET"),
    ("1630", "Unrealized appreciation on investments", "ASSET", "OTHER_ASSET"),
    # Liabilities
    ("2150", "Capital calls payable", "LIABILITY", "OTHER_CURRENT_LIABILITY"),
    ("2160", "Accrued expenses - family entities", "LIABILITY", "OTHER_CURRENT_LIABILITY"),
    ("2180", "Due to related entities", "LIABILITY", "OTHER_CURRENT_LIABILITY"),
    ("2400", "Income taxes payable", "LIABILITY", "OTHER_CURRENT_LIABILITY"),
    ("2600", "Mortgage payable", "LIABILITY", "LONG_TERM_LIABILITY"),
    ("2610", "Notes payable - related parties", "LIABILITY", "LONG_TERM_LIABILITY"),
    # Equity, by kind of entity
    ("3900", "Trust principal (corpus)", "EQUITY", "EQUITY"),
    ("3910", "Partners' capital", "EQUITY", "EQUITY"),
    ("3920", "Members' capital", "EQUITY", "EQUITY"),
    ("3930", "Contributions from grantors and owners", "EQUITY", "EQUITY"),
    ("3940", "Distributions to beneficiaries and owners", "EQUITY", "EQUITY"),
    # Income (the app adds dividend, interest, fund income and realized gain/loss)
    ("4100", "Management fee income - related parties", "REVENUE", "INCOME"),
    ("7130", "K-1 ordinary business income", "OTHER_INCOME", "OTHER_INCOME"),
    ("7140", "K-1 portfolio income", "OTHER_INCOME", "OTHER_INCOME"),
    ("7150", "K-1 capital gains", "OTHER_INCOME", "REALIZED_GAIN_LOSS"),
    ("7300", "Unrealized gain / loss on investments", "OTHER_INCOME", "UNREALIZED_GAIN_LOSS"),
    ("7400", "Rental income", "OTHER_INCOME", "OTHER_INCOME"),
    # Expenses (the app adds investment management fees)
    ("6510", "Trustee fees", "OPERATING_EXPENSES", "EXPENSE"),
    ("6520", "Professional and advisory fees", "OPERATING_EXPENSES", "EXPENSE"),
    ("6530", "Legal fees", "OPERATING_EXPENSES", "EXPENSE"),
    ("6540", "Tax preparation and accounting fees", "OPERATING_EXPENSES", "EXPENSE"),
    ("6550", "Insurance expense", "OPERATING_EXPENSES", "EXPENSE"),
    ("6560", "Property taxes and maintenance", "OPERATING_EXPENSES", "EXPENSE"),
    ("6570", "Bank and custody fees", "OPERATING_EXPENSES", "EXPENSE"),
    ("6580", "Management fees - related party", "OPERATING_EXPENSES", "EXPENSE"),
    # Household and family spending (the Lifestyle Expense Report)
    ("6610", "Household staff", "OPERATING_EXPENSES", "EXPENSE"),
    ("6620", "Residence operating costs", "OPERATING_EXPENSES", "EXPENSE"),
    ("6630", "Travel", "OPERATING_EXPENSES", "EXPENSE"),
    ("6640", "Aviation", "OPERATING_EXPENSES", "EXPENSE"),
    ("6650", "Clubs and memberships", "OPERATING_EXPENSES", "EXPENSE"),
    ("6660", "Education", "OPERATING_EXPENSES", "EXPENSE"),
    ("6900", "Charitable grants", "OPERATING_EXPENSES", "EXPENSE"),
    ("8200", "Interest expense", "OTHER_EXPENSE", "OTHER_EXPENSE"),
    ("8300", "Income tax expense", "OTHER_EXPENSE", "OTHER_EXPENSE"),
]
# Payees (all fictional). The funds' GPs receive capital-call bills; the rest are a family office's usual vendors.
VENDORS = [
    ("Summit Growth Partners", "General partner, Summit Growth Partners IV (demo fund)"),
    ("Crescent Ventures", "General partner, Crescent Venture Fund II (demo fund)"),
    ("Northwind Trust Company", "Corporate trustee, Alder 2012 Dynasty Trust"),
    ("Hollis & Park LLP", "Trusts and estates counsel; trust protector"),
    ("Bayview Securities", "Custodian of the family's brokerage accounts"),
    ("Marlowe Finch CPAs", "Tax preparation and fiduciary accounting"),
    ("Granite Peak Advisors", "Investment adviser"),
    ("Harborline Insurance Brokers", "Property, casualty and umbrella insurance"),
    ("Seabright Property Management", "Manager of the Harbor Street property"),
]
REASON = {"reason": "Alder demo setup teardown"}


class Command(BaseCommand):
    help = "Create (or remove) the Alder demo structure in a Campfire sandbox: entities, operating accounts, fund vendors."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--teardown", action="store_true", help="Delete the entities, accounts and vendors this command creates")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        try:
            self.client = ApiKeyClient()
        except NotConnected as e:
            raise CommandError(str(e)) from e
        self.dry = opts["dry_run"]
        try:
            if opts["teardown"]:
                self.teardown()
            else:
                self.setup()
        except CampfireError as e:
            raise CommandError(str(e)) from e

    def say(self, verb: str, what: str) -> None:
        style = {"create": self.style.SUCCESS, "created": self.style.SUCCESS, "delete": self.style.WARNING, "deleted": self.style.WARNING}.get(verb, str)
        self.stdout.write(style(f"  {verb:8} {what}"))

    # ------------------------------------------------------------------------------------------- setup

    def setup(self) -> None:
        entities = {_norm(e.get("name")): e for e in _all(self.client, ENTITY_PATH) if not e.get("is_deleted")}
        self.stdout.write("Entities:")
        parent = self.entity(entities, PARENT, {"name": PARENT, "currency": "USD", "description": "Consolidation parent for the fictional Alder family (demo)"})
        parent_id = parent.get("id") if parent else None
        for name in ALDER_ENTITIES:
            self.entity(entities, name, {"name": name, "currency": "USD", "parent": parent_id, "description": "Alder family demo entity"})
        elim = self.entity(entities, ELIMINATIONS, {"name": ELIMINATIONS, "currency": "USD", "parent": parent_id, "is_elimination_entity": True})
        if parent and elim and not self.dry and parent.get("elimination_entity") != elim.get("id"):
            r = self.client.request("PATCH", f"{ENTITY_PATH}/{parent['id']}", json={"elimination_entity": elim["id"]})
            self.say("linked" if r.status_code < 400 else "warning", f"eliminations entity on {PARENT}" + ("" if r.status_code < 400 else f" ({r.status_code}: {r.text[:200]})"))

        self.stdout.write("Operating accounts:")
        for plan in plan_accounts(OPERATING_ACCOUNTS, list_accounts(self.client)):
            if plan.existing:
                self.say("exists", plan.label)
            elif self.dry:
                self.say("create", plan.label)
            else:
                self.say("created", f"{plan.label} (id {create_planned(self.client, plan)['id']})")

        self.stdout.write("Vendors:")
        vendors = {_norm(v.get("name")) for v in _all(self.client, VENDOR_PATH)}
        for name, note in VENDORS:
            if _norm(name) in vendors:
                self.say("exists", name)
            elif self.dry:
                self.say("create", name)
            else:
                v = _post(self.client, VENDOR_PATH, {"name": name, "vendor_type": "vendor", "notes": f"{note} (fictional, demo)"})
                self.say("created", f"{name} (id {v['id']})")

    def entity(self, entities: dict[str, dict[str, Any]], name: str, body: dict[str, Any]) -> dict[str, Any] | None:
        found = entities.get(_norm(name))
        if found:
            self.say("exists", f"{name} (id {found['id']})")
            return found
        if self.dry:
            self.say("create", name)
            return None
        created = _post(self.client, ENTITY_PATH, {k: v for k, v in body.items() if v is not None})
        entities[_norm(name)] = created
        self.say("created", f"{name} (id {created['id']})")
        return created

    # ---------------------------------------------------------------------------------------- teardown

    def delete(self, path: str, item_id: Any, label: str) -> None:
        if self.dry:
            self.say("delete", label)
            return
        r = self.client.request("POST", f"{path}/{item_id}/delete", json=REASON)
        if r.status_code == 404 or r.status_code == 405:
            r = self.client.request("DELETE", f"{path}/{item_id}")
        self.say("deleted" if r.status_code < 400 else "failed", label + ("" if r.status_code < 400 else f" ({r.status_code}: {r.text[:200]})"))

    def teardown(self) -> None:
        vendors = {_norm(v.get("name")): v for v in _all(self.client, VENDOR_PATH)}
        for name, _ in VENDORS:
            if _norm(name) in vendors:
                self.delete(VENDOR_PATH, vendors[_norm(name)]["id"], f"vendor {name}")
        accounts = {_norm(a.get("name")): a for a in list_accounts(self.client)}
        for _, name, _, _ in OPERATING_ACCOUNTS:
            if _norm(name) in accounts:
                self.delete(ACCOUNT_PATH, accounts[_norm(name)]["id"], f"account {name}")
        entities = {_norm(e.get("name")): e for e in _all(self.client, ENTITY_PATH) if not e.get("is_deleted")}
        for name in [*ALDER_ENTITIES, ELIMINATIONS, PARENT]:  # children before the parent
            if _norm(name) in entities:
                self.delete(ENTITY_PATH, entities[_norm(name)]["id"], f"entity {name}")
