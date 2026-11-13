"""Onboarding a family from one Excel workbook, with opening balances; and exporting a family to the same workbook.

    batch = stage(workspace, content=xlsx_bytes, file_name=..., family_name="Alder Family", family_code="alder",
                  as_of=date(2025, 12, 31), set_gl_start=True)    # validates; nothing kept
    commit(batch)                                                   # imports, all or nothing

Sheets (headers in row 1; rows refer to each other by name; choices take the label or the code):
Branches, People, Entities, Ownership, Roles, Securities, Accounts (with opening cash), Prices, Positions (opening
lots: quantity, cost basis, original purchase date), Commitments (called and distributed to date), Marks (NAV or
appraisal), Transactions (optional history). Opening balances are booked on the opening-balance date: positions
as transfers in with cost basis, cash as a deposit, fund flows to date as one contribution and distributions.
The GL start date can be set to the next day, so those balances (already in Campfire's opening balances) aren't
posted again.

Rows that already exist (matched by name) are left as they are and reported; changes to existing records go
through the Records screens, where they are audited. Uploading the same workbook again adds nothing.
"""

import base64
import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Sum
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from campfire_integration.models import Workspace
from entities.models import Branch, Entity, EntityRole, Family, OwnershipInterest, Person
from entities.services.ownership import OwnershipCycleError, effective_ownership
from gl.models import GLSettings
from investments.models import Account, AssetClass, Commitment, ImportBatch, ImportIssue, Instrument, Price, Transaction, Valuation
from investments.services.ledger import LedgerError, replay
from investments.services.valuation import record_valuation
from investments.taxonomy import ensure_default_taxonomy

SOURCE = "onboarding"
T = Transaction.Type
S = ImportIssue.Severity
ZERO = Decimal(0)
MAX_ISSUES = 300


class OnboardingFailed(Exception):
    pass


# ------------------------------------------------------------------------------------------------ the sheets


@dataclass(frozen=True)
class Col:
    key: str
    header: str
    kind: str = "text"  # text | date | decimal | int | bool | choice
    required: bool = False
    choices: type[models.TextChoices] | None = None
    note: str = ""


@dataclass(frozen=True)
class Sheet:
    name: str
    cols: tuple[Col, ...]
    note: str


def C(*args: Any, **kw: Any) -> Col:
    return Col(*args, **kw)


SHEETS = [
    Sheet("Branches", (C("name", "Name", required=True), C("parent", "Part of branch", note="Name of a branch above this one")),
          "Households and generations, e.g. Founders (G1) → Branch A."),
    Sheet("People", (C("first_name", "First name", required=True), C("last_name", "Last name", required=True),
                     C("preferred_name", "Preferred name"), C("branch", "Branch"), C("generation", "Generation", "int", note="1 = founders"),
                     C("birth_year", "Birth year", "int"), C("principal", "Principal", "bool"), C("email", "Email")),
          "Family members. Owners and trustees are referred to by 'First Last'."),
    Sheet("Entities", (C("name", "Name", required=True), C("short_name", "Short name"),
                       C("entity_type", "Type", "choice", True, Entity.EntityType), C("tax_classification", "Tax classification", "choice", True, Entity.TaxClassification),
                       C("jurisdiction", "Jurisdiction", note="e.g. US-DE"), C("formation_date", "Formed", "date"), C("branch", "Branch"),
                       C("status", "Status", "choice", False, Entity.Status)),
          "Trusts, partnerships, LLCs, foundations, companies."),
    Sheet("Ownership", (C("owned", "Entity owned", required=True), C("owner", "Owner", required=True, note="An entity name or a person 'First Last'"),
                        C("interest_type", "Interest type", "choice", True, OwnershipInterest.InterestType), C("percentage", "Percentage", "decimal", True),
                        C("effective_from", "From", "date", True), C("effective_to", "To", "date")),
          "Who owns what, in percent."),
    Sheet("Roles", (C("entity", "Entity", required=True), C("role", "Role", "choice", True, EntityRole.Role),
                    C("holder", "Held by", required=True, note="A person 'First Last', or the name of an outside party")),
          "Trustees, managers, protectors, signers."),
    Sheet("Securities", (C("name", "Name", required=True), C("kind", "Type", "choice", True, Instrument.Kind),
                         C("asset_class", "Asset class", required=True, note="Code or name, e.g. public_equity"), C("ticker", "Ticker"),
                         C("cusip", "CUSIP"), C("isin", "ISIN"), C("sponsor", "Manager / GP"), C("vintage_year", "Vintage", "int"), C("currency", "Currency")),
          "Listed securities, funds, property, notes, art."),
    Sheet("Accounts", (C("name", "Name", required=True), C("owner", "Owner", required=True, note="An entity name or a person 'First Last'"),
                       C("account_type", "Type", "choice", True, Account.AccountType), C("tax_treatment", "Tax treatment", "choice", True, Account.TaxTreatment),
                       C("custodian", "Custodian"), C("custodian_account", "Custodian account number", note="As in the custodian's files"),
                       C("last4", "Last 4"), C("holds_cash", "Holds cash", "bool", note="Yes for custody accounts"),
                       C("opened_on", "Opened", "date"), C("opening_cash", "Opening cash", "decimal", note="Cash balance on the opening date")),
          "Custody accounts, fund interests, property, notes."),
    Sheet("Prices", (C("security", "Security", required=True, note="Ticker, CUSIP or name"), C("as_of", "Date", "date", True), C("price", "Price", "decimal", True)),
          "Prices for listed securities, e.g. on the opening date."),
    Sheet("Positions", (C("account", "Account", required=True), C("security", "Security", required=True), C("quantity", "Quantity", "decimal",
                          note="Units; blank for property and other holdings without units"), C("cost_basis", "Cost basis", "decimal", True),
                        C("acquired_on", "Acquired on", "date", note="Original purchase date of the lot (holding period)")),
          "Opening positions on the opening date: one row per tax lot. Funds with a commitment go on the Commitments sheet."),
    Sheet("Commitments", (C("account", "Account", required=True), C("fund", "Fund", required=True), C("commitment", "Commitment", "decimal", True),
                          C("commitment_date", "Commitment date", "date", True), C("called", "Called to date", "decimal"),
                          C("returned", "Distributed: return of capital", "decimal"), C("recallable", "Of which recallable", "decimal"),
                          C("income", "Distributed: income and gains", "decimal")),
          "Private fund commitments and their cash flows to date, booked on the opening date (or the fund's first mark, if earlier)."),
    Sheet("Marks", (C("account", "Account", required=True), C("holding", "Holding", required=True), C("as_of", "Date", "date", True),
                    C("value", "Value", "decimal", True), C("method", "Method", "choice", False, Valuation.Method)),
          "NAV statements and appraisals for funds, property and other holdings without a market price."),
    Sheet("Transactions", (C("account", "Account", required=True), C("date", "Date", "date", True), C("type", "Type", "choice", True, Transaction.Type),
                           C("security", "Security"), C("quantity", "Quantity", "decimal"), C("amount", "Amount", "decimal", True), C("fees", "Fees", "decimal"),
                           C("acquired_on", "Acquired on", "date"), C("description", "Description")),
          "Optional: dated history (e.g. fund contributions and distributions, so IRR uses real dates). Amounts positive."),
]
SHEET_BY_NAME = {s.name.lower(): s for s in SHEETS}


# ---------------------------------------------------------------------------------------------- reading


@dataclass
class Issue:
    severity: str
    message: str
    sheet: str = ""
    row: int | None = None


@dataclass
class Result:
    issues: list[Issue] = field(default_factory=list)
    created: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    existing: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    family: Family | None = None

    def add(self, severity: str, message: str, sheet: str = "", row: int | None = None) -> None:
        self.issues.append(Issue(severity, message, sheet, row))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == S.ERROR]


class CellError(ValueError):
    pass


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def _convert(col: Col, raw: Any) -> Any:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if col.required:
            raise CellError(f"{col.header} is required")
        return None
    if col.kind == "text":
        return str(raw).strip() if not isinstance(raw, float) or not raw.is_integer() else str(int(raw))
    if col.kind == "date":
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(str(raw).strip(), fmt).date()
            except ValueError:
                continue
        raise CellError(f"{col.header}: {raw!r} is not a date (YYYY-MM-DD)")
    if col.kind in ("decimal", "int"):
        try:
            value = Decimal(str(raw).replace(",", "").replace("$", "").strip())
        except InvalidOperation as e:
            raise CellError(f"{col.header}: {raw!r} is not a number") from e
        if col.kind == "int":
            if value != value.to_integral_value():
                raise CellError(f"{col.header}: {raw!r} is not a whole number")
            return int(value)
        return value
    if col.kind == "bool":
        text = _norm(raw)
        if text in ("y", "yes", "true", "1", "x"):
            return True
        if text in ("n", "no", "false", "0", ""):
            return False
        raise CellError(f"{col.header}: use Yes or No")
    if col.kind == "choice":
        assert col.choices is not None
        text = _norm(raw)
        for value, label in col.choices.choices:
            if text in (_norm(value), _norm(label)):
                return value
        raise CellError(f"{col.header}: {raw!r} isn't one of: {', '.join(str(lbl) for _, lbl in col.choices.choices)}")
    raise AssertionError(col.kind)


def read_workbook(content: bytes, result: Result) -> dict[str, list[tuple[int, dict[str, Any]]]]:
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # openpyxl raises many kinds for a file that isn't a workbook
        result.add(S.ERROR, f"This isn't an Excel workbook (.xlsx) we can read: {type(e).__name__}.")
        return {}
    rows: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for ws in wb.worksheets:
        sheet = SHEET_BY_NAME.get(ws.title.strip().lower())
        if sheet is None:
            continue  # e.g. "Instructions"
        it = ws.iter_rows(values_only=True)
        header = next(it, None) or ()
        index = {_norm(h): i for i, h in enumerate(header) if h is not None}
        cols = {c.key: index.get(_norm(c.header)) for c in sheet.cols}
        missing = [c.header for c in sheet.cols if c.required and cols[c.key] is None]
        if missing:
            result.add(S.ERROR, f"Missing column(s): {', '.join(missing)}.", sheet.name)
            continue
        parsed = []
        for n, values in enumerate(it, start=2):
            if not any(v is not None and str(v).strip() for v in values):
                continue
            record: dict[str, Any] = {}
            try:
                for c in sheet.cols:
                    i = cols[c.key]
                    record[c.key] = _convert(c, values[i] if i is not None and i < len(values) else None)
            except CellError as e:
                result.add(S.ERROR, str(e) + ".", sheet.name, n)
                continue
            parsed.append((n, record))
        rows[sheet.name] = parsed
    wb.close()
    return rows


# ---------------------------------------------------------------------------------------------- importing


class _RowError(Exception):
    pass


class Importer:
    def __init__(self, workspace: Workspace, family: Family, batch: ImportBatch, as_of: date, result: Result) -> None:
        self.ws, self.family, self.batch, self.as_of, self.result = workspace, family, batch, as_of, result
        self.classes = ensure_default_taxonomy(workspace)
        self.branches = {_norm(b.name): b for b in Branch.objects.filter(family=family)}
        self.people = {}
        for p in Person.objects.filter(family=family):
            self.people[_norm(f"{p.first_name} {p.last_name}")] = p
            self.people.setdefault(_norm(p.display_name), p)
        self.entities = {_norm(e.name): e for e in Entity.objects.filter(family=family)}
        self.accounts = {_norm(a.name): a for a in Account.objects.filter(family=family)}
        self.securities: dict[str, Instrument] = {}
        for i in Instrument.objects.filter(workspace=workspace):
            for key in (i.ticker, i.cusip, i.name):
                if key:
                    self.securities.setdefault(_norm(key), i)

    # helpers
    def fail(self, message: str) -> None:
        raise _RowError(message)

    def find(self, table: dict[str, Any], name: Any, what: str) -> Any:
        found = table.get(_norm(name))
        if found is None:
            self.fail(f"{what} '{name}' not found (check the spelling, or add it on its sheet)")
        return found

    def owner(self, name: str) -> Entity | Person:
        found = self.entities.get(_norm(name)) or self.people.get(_norm(name))
        if found is None:
            self.fail(f"Owner '{name}' is neither an entity nor a person in this family")
        return found  # type: ignore[return-value]

    def save(self, obj: models.Model, sheet: str) -> models.Model:
        try:
            obj.full_clean()
        except ValidationError as e:
            self.fail(" ".join(e.messages))
        obj.save()
        self.result.created[sheet] += 1
        return obj

    def txn(self, sheet: str, n: int, label: str = "", **kw: Any) -> None:
        """Book an opening or history transaction once: the row's fingerprint is its source_ref. `label` is how it is counted."""
        ref = "onboard:" + hashlib.sha256(f"{sheet}|{n}|{sorted((k, str(v)) for k, v in kw.items())}".encode()).hexdigest()[:24]
        account = kw["account"]
        if Transaction.objects.filter(account=account, source_ref=ref).exists():
            self.result.existing[label or sheet] += 1
            return
        obj = Transaction(batch=self.batch, source_ref=ref, **kw)
        try:
            obj.full_clean(exclude=["batch"])
        except ValidationError as e:
            self.fail(" ".join(e.messages))
        obj.save()
        self.result.created[label or sheet] += 1

    def opening_date(self, account: Any, holding: Any) -> date:
        """Opening balances of a marked holding are dated no later than its first mark in the workbook: the NAV
        roll-forward adds flows after a mark, and these balances are already in that mark."""
        first = self.first_mark.get((_norm(account), _norm(holding)))
        return min(self.as_of, first) if first else self.as_of

    def run(self, rows: dict[str, list[tuple[int, dict[str, Any]]]]) -> None:
        self.first_mark: dict[tuple[str, str], date] = {}
        for _, r in rows.get("Marks", []):
            key = (_norm(r["account"]), _norm(r["holding"]))
            self.first_mark[key] = min(self.first_mark.get(key, r["as_of"]), r["as_of"])
        for sheet in SHEETS:
            handler = getattr(self, f"do_{sheet.name.lower()}")
            for n, r in rows.get(sheet.name, []):
                try:
                    with transaction.atomic():
                        handler(sheet.name, n, r)
                except _RowError as e:
                    self.result.add(S.ERROR, f"{e}.", sheet.name, n)
                except IntegrityError as e:
                    self.result.add(S.ERROR, f"Conflicts with an existing record ({e}).", sheet.name, n)
        self.final_checks()

    # sheets
    def do_branches(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        if _norm(r["name"]) in self.branches:
            self.result.existing[sheet] += 1
            return
        parent = self.find(self.branches, r["parent"], "Branch") if r["parent"] else None
        self.branches[_norm(r["name"])] = self.save(Branch(family=self.family, name=r["name"], parent=parent), sheet)  # type: ignore[assignment]

    def do_people(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        key = _norm(f"{r['first_name']} {r['last_name']}")
        if key in self.people:
            self.result.existing[sheet] += 1
            return
        p = Person(family=self.family, first_name=r["first_name"], last_name=r["last_name"], preferred_name=r["preferred_name"] or "",
                   branch=self.find(self.branches, r["branch"], "Branch") if r["branch"] else None, generation=r["generation"],
                   birth_year=r["birth_year"], is_principal=bool(r["principal"]), email=r["email"] or "")
        self.save(p, sheet)
        self.people[key] = p
        self.people.setdefault(_norm(p.display_name), p)

    def do_entities(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        if _norm(r["name"]) in self.entities:
            self.result.existing[sheet] += 1
            return
        e = Entity(family=self.family, name=r["name"], short_name=r["short_name"] or "", entity_type=r["entity_type"],
                   tax_classification=r["tax_classification"], jurisdiction=r["jurisdiction"] or "", formation_date=r["formation_date"],
                   branch=self.find(self.branches, r["branch"], "Branch") if r["branch"] else None, status=r["status"] or Entity.Status.ACTIVE)
        self.entities[_norm(r["name"])] = self.save(e, sheet)  # type: ignore[assignment]

    def do_ownership(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        owned = self.find(self.entities, r["owned"], "Entity")
        owner = self.owner(r["owner"])
        kw = {"owner_entity": owner} if isinstance(owner, Entity) else {"owner_person": owner}
        if OwnershipInterest.objects.filter(owned=owned, effective_from=r["effective_from"], **kw).exists():
            self.result.existing[sheet] += 1
            return
        self.save(OwnershipInterest(owned=owned, interest_type=r["interest_type"], percentage=r["percentage"],
                                    effective_from=r["effective_from"], effective_to=r["effective_to"], **kw), sheet)

    def do_roles(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        entity = self.find(self.entities, r["entity"], "Entity")
        person = self.people.get(_norm(r["holder"]))
        kw = {"person": person} if person else {"external_name": r["holder"]}
        if EntityRole.objects.filter(entity=entity, role=r["role"], **kw).exists():
            self.result.existing[sheet] += 1
            return
        self.save(EntityRole(entity=entity, role=r["role"], **kw), sheet)

    def do_securities(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        keys = [k for k in (r["ticker"], r["cusip"], r["name"]) if k]
        if any(_norm(k) in self.securities for k in keys):
            self.result.existing[sheet] += 1
            return
        ac = self.classes.get(_norm(r["asset_class"]).replace(" ", "_")) or next(
            (a for a in self.classes.values() if _norm(a.name) == _norm(r["asset_class"])), None)
        if ac is None:
            self.fail(f"Asset class '{r['asset_class']}' not found (use a code like public_equity, or add it in Records)")
        inst = Instrument(workspace=self.ws, name=r["name"], kind=r["kind"], asset_class=ac, ticker=(r["ticker"] or "").upper(),
                          cusip=(r["cusip"] or "").upper(), isin=(r["isin"] or "").upper(), sponsor=r["sponsor"] or "",
                          vintage_year=r["vintage_year"], currency=(r["currency"] or "USD").upper(), is_unitized=r["kind"] in Instrument.UNITIZED_KINDS)
        self.save(inst, sheet)
        for k in keys:
            self.securities[_norm(k)] = inst

    def do_accounts(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        if _norm(r["name"]) in self.accounts:
            self.result.existing[sheet] += 1
            return
        owner = self.owner(r["owner"])
        holds_cash = r["holds_cash"] if r["holds_cash"] is not None else r["account_type"] in (Account.AccountType.BROKERAGE, Account.AccountType.RETIREMENT)
        a = Account(family=self.family, name=r["name"], account_type=r["account_type"], tax_treatment=r["tax_treatment"],
                    custodian=r["custodian"] or "", external_id=r["custodian_account"] or "", account_number_last4=r["last4"] or "",
                    tracks_cash=holds_cash, opened_on=r["opened_on"], source=SOURCE,
                    entity=owner if isinstance(owner, Entity) else None, person=owner if isinstance(owner, Person) else None)
        self.accounts[_norm(r["name"])] = self.save(a, sheet)  # type: ignore[assignment]
        cash = r["opening_cash"]
        if cash:
            if cash < 0:
                self.fail("Opening cash can't be negative (enter a margin loan as a liability in Campfire)")
            if not holds_cash:
                self.fail("Opening cash on an account that doesn't hold cash")
            self.txn("Accounts", n, label="Opening cash", account=a, txn_type=T.DEPOSIT, trade_date=self.as_of, amount=cash, description="Opening cash balance")

    def do_prices(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        inst = self.find(self.securities, r["security"], "Security")
        existing = Price.objects.filter(instrument=inst, as_of=r["as_of"]).first()
        if existing is not None:
            if existing.price != r["price"]:
                self.fail(f"A different price ({existing.price}) is already recorded for that date")
            self.result.existing[sheet] += 1
            return
        if r["price"] <= 0:
            self.fail("The price must be positive")
        self.save(Price(instrument=inst, as_of=r["as_of"], price=r["price"], source=SOURCE, batch=self.batch), sheet)

    def do_positions(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        account = self.find(self.accounts, r["account"], "Account")
        inst = self.find(self.securities, r["security"], "Security")
        if Commitment.objects.filter(account=account, instrument=inst).exists():
            self.fail("This fund has a commitment: put its flows on the Commitments sheet")
        if inst.is_unitized and not r["quantity"]:
            self.fail("A listed security needs a quantity")
        if r["cost_basis"] < 0 or (r["quantity"] is not None and r["quantity"] <= 0):
            self.fail("Quantity and cost basis must be positive")
        when = self.as_of if inst.is_unitized else self.opening_date(r["account"], r["security"])
        self.txn(sheet, n, account=account, instrument=inst, txn_type=T.TRANSFER_IN, trade_date=when,
                 quantity=r["quantity"] if inst.is_unitized else None, amount=r["cost_basis"],
                 acquired_on=r["acquired_on"] or self.as_of, description="Opening position")

    def do_commitments(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        account = self.find(self.accounts, r["account"], "Account")
        fund = self.find(self.securities, r["fund"], "Fund")
        if fund.is_unitized:
            self.fail(f"'{fund}' is a listed security, not a fund interest")
        existing = Commitment.objects.filter(account=account).first()
        if existing is None:
            self.save(Commitment(account=account, instrument=fund, amount=r["commitment"], commitment_date=r["commitment_date"]), sheet)
        elif existing.instrument_id != fund.pk:
            self.fail(f"Account '{account}' already has a commitment to {existing.instrument}")
        else:
            self.result.existing[sheet] += 1
        called, returned, recallable, income = (r[k] or ZERO for k in ("called", "returned", "recallable", "income"))
        if recallable > returned:
            self.fail("Recallable can't exceed the return of capital")
        flows = [(T.CONTRIBUTION, called, False, "Called to date"), (T.DISTRIBUTION_ROC, returned - recallable, False, "Return of capital to date"),
                 (T.DISTRIBUTION_ROC, recallable, True, "Recallable return of capital to date"), (T.DISTRIBUTION_GAIN, income, False, "Income and gains distributed to date")]
        when = self.opening_date(r["account"], r["fund"])
        for t, amount, is_recallable, label in flows:
            if amount:
                self.txn(f"{sheet}:{t}:{is_recallable}", n, label="Fund flows to date", account=account, instrument=fund, txn_type=t, trade_date=when,
                         amount=amount, is_recallable=is_recallable, description=label)

    def do_marks(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        account = self.find(self.accounts, r["account"], "Account")
        inst = self.find(self.securities, r["holding"], "Holding")
        if inst.is_unitized:
            self.fail(f"'{inst}' is priced, not marked: use the Prices sheet")
        if Valuation.objects.filter(account=account, instrument=inst, as_of=r["as_of"]).exists():
            self.result.existing[sheet] += 1
            return
        v = record_valuation(account=account, instrument=inst, as_of=r["as_of"], value=r["value"],
                             method=r["method"] or Valuation.Method.NAV_STATEMENT, batch=self.batch, source_ref=f"onboarding row {n}",
                             created_by=self.batch.created_by, created_by_id=self.batch.created_by_id)
        self.result.created[sheet] += 1
        if v.status == Valuation.Status.HELD:
            self.result.add(S.WARNING, f"Held for review: {v.change_pct}% from the expected value.", sheet, n)

    def do_transactions(self, sheet: str, n: int, r: dict[str, Any]) -> None:
        account = self.find(self.accounts, r["account"], "Account")
        inst = self.find(self.securities, r["security"], "Security") if r["security"] else None
        if r["amount"] < 0 or (r["fees"] or ZERO) < 0 or (r["quantity"] is not None and r["quantity"] <= 0):
            self.fail("Amounts and quantities are positive; the type gives the direction")
        self.txn(sheet, n, account=account, instrument=inst, txn_type=r["type"], trade_date=r["date"], quantity=r["quantity"],
                 amount=r["amount"], fees=r["fees"] or ZERO, acquired_on=r["acquired_on"], description=r["description"] or "")

    def final_checks(self) -> None:
        for account in Account.objects.filter(family=self.family):
            try:
                replay(account, max(self.as_of, timezone.localdate()))
            except LedgerError as e:
                self.result.add(S.ERROR, f"Account '{account}': {e}.", "Positions / Transactions")
        try:
            effective_ownership(self.family, self.as_of)
        except OwnershipCycleError as e:
            self.result.add(S.ERROR, f"Ownership loops: {e}.", "Ownership")


# --------------------------------------------------------------------------------------- stage and commit


class _Rollback(Exception):
    pass


def _family(workspace: Workspace, options: dict[str, Any], result: Result) -> Family | None:
    if options.get("family_id"):
        fam = Family.objects.filter(workspace=workspace, pk=options["family_id"]).first()
        if fam is None:
            result.add(S.ERROR, "That family isn't in this workspace.")
        return fam
    name, code = (options.get("family_name") or "").strip(), (options.get("family_code") or "").strip().lower()
    if not name or not code:
        result.add(S.ERROR, "Give the new family a name and a short code.")
        return None
    existing = Family.objects.filter(workspace=workspace, code=code).first()
    if existing is not None:
        return existing
    fam = Family(workspace=workspace, name=name, code=code)
    try:
        fam.full_clean()
    except ValidationError as e:
        result.add(S.ERROR, "Family: " + " ".join(e.messages))
        return None
    fam.save()
    result.created["Family"] += 1
    return fam


def run(batch: ImportBatch, *, write: bool) -> Result:
    result = Result()
    options = batch.options
    as_of = date.fromisoformat(options["as_of"])
    rows = read_workbook(base64.b64decode(batch.content), result)
    if not rows:
        if not result.errors:
            result.add(S.ERROR, f"No sheets found. Use the template: {', '.join(s.name for s in SHEETS)}.")
        return result
    # Rows with unreadable cells were skipped above; the trial import still runs, so every problem is reported at once.
    try:
        with transaction.atomic():
            family = _family(batch.workspace, options, result)
            if family is not None:
                result.family = family
                Importer(batch.workspace, family, batch, as_of, result).run(rows)
                if options.get("set_gl_start") and not result.errors:
                    settings = GLSettings.for_workspace(batch.workspace)
                    if settings.post_from is None or settings.post_from <= as_of:
                        settings.post_from = as_of + timedelta(days=1)
                        settings.save()
                        result.add(S.INFO, f"GL start date set to {settings.post_from}: balances on the opening date are not posted.")
            if not write or result.errors:
                raise _Rollback
    except _Rollback:
        pass
    return result


def _record(batch: ImportBatch, result: Result, status: str) -> ImportBatch:
    batch.issues.all().delete()
    ImportIssue.objects.bulk_create([
        ImportIssue(batch=batch, row_number=i.row, severity=i.severity, code=i.sheet[:40] or "workbook",
                    message=(f"{i.sheet}: " if i.sheet else "") + i.message[:480])
        for i in result.issues[:MAX_ISSUES]
    ])
    batch.rows_new = sum(result.created.values())
    batch.rows_duplicate = sum(result.existing.values())
    batch.rows_total = batch.rows_new + batch.rows_duplicate + len({(i.sheet, i.row) for i in result.errors if i.row})
    batch.options = {**batch.options, "created": dict(result.created), "existing": dict(result.existing)}
    if result.family is not None and result.family.pk and Family.objects.filter(pk=result.family.pk).exists():
        batch.family = result.family
    batch.status = status
    if status == ImportBatch.Status.COMMITTED:
        batch.committed_at = timezone.now()
    batch.save()
    return batch


def stage(workspace: Workspace, *, content: bytes, file_name: str, as_of: date, family_id: int | None = None,
          family_name: str = "", family_code: str = "", set_gl_start: bool = False, created_by: str = "",
          created_by_id: str = "") -> ImportBatch:
    batch = ImportBatch.objects.create(
        workspace=workspace, source=SOURCE, kind=ImportBatch.Kind.OTHER, file_format="onboarding_xlsx", file_name=file_name,
        reference=file_name, file_sha256=hashlib.sha256(content).hexdigest(), content=base64.b64encode(content).decode(),
        options={"as_of": as_of.isoformat(), "family_id": family_id, "family_name": family_name, "family_code": family_code,
                 "set_gl_start": set_gl_start},
        created_by=created_by, created_by_id=created_by_id, status=ImportBatch.Status.VALIDATED,
    )
    result = run(batch, write=False)
    return _record(batch, result, ImportBatch.Status.REJECTED if result.errors else ImportBatch.Status.VALIDATED)


def commit(batch: ImportBatch) -> ImportBatch:
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.status != ImportBatch.Status.VALIDATED or batch.source != SOURCE:
            raise OnboardingFailed("Only a validated onboarding workbook can be imported.")
        result = run(batch, write=True)
        return _record(batch, result, ImportBatch.Status.REJECTED if result.errors else ImportBatch.Status.COMMITTED)


# --------------------------------------------------------------------------------------------- the workbook

HEADER_FILL = PatternFill("solid", fgColor="FFEDD5")


def _label(choices: type[models.TextChoices] | None, value: Any) -> Any:
    if choices is None or value in (None, ""):
        return value
    return dict(choices.choices).get(value, value)


def _owner_name(o: Entity | Person) -> str:
    return o.name if isinstance(o, Entity) else f"{o.first_name} {o.last_name}"


def family_rows(family: Family, as_of: date) -> dict[str, list[list[Any]]]:
    """A family's records as workbook rows, with positions as FIFO lots and fund flows to date (export / example)."""
    D = lambda d: d or None  # noqa: E731  (real Excel dates)
    out: dict[str, list[list[Any]]] = defaultdict(list)
    for b in Branch.objects.filter(family=family).select_related("parent").order_by("sort_order", "id"):
        out["Branches"].append([b.name, b.parent.name if b.parent else None])
    for p in Person.objects.filter(family=family).select_related("branch"):
        out["People"].append([p.first_name, p.last_name, p.preferred_name or None, p.branch.name if p.branch else None, p.generation,
                              p.birth_year, "Yes" if p.is_principal else None, p.email or None])
    for e in Entity.objects.filter(family=family).select_related("branch"):
        out["Entities"].append([e.name, e.short_name or None, e.get_entity_type_display(), e.get_tax_classification_display(),
                                e.jurisdiction or None, D(e.formation_date), e.branch.name if e.branch else None, e.get_status_display()])
    for o in OwnershipInterest.objects.filter(owned__family=family).select_related("owned", "owner_entity", "owner_person"):
        out["Ownership"].append([o.owned.name, _owner_name(o.owner), o.get_interest_type_display(), o.percentage.normalize(),
                                 D(o.effective_from), D(o.effective_to)])
    for r in EntityRole.objects.filter(entity__family=family).select_related("entity", "person"):
        out["Roles"].append([r.entity.name, r.get_role_display(), _owner_name(r.person) if r.person else r.external_name])
    accounts = list(Account.objects.filter(family=family).select_related("entity", "person"))
    used = Instrument.objects.filter(transactions__account__family=family).distinct().select_related("asset_class")
    for i in used:
        out["Securities"].append([i.name, i.get_kind_display(), i.asset_class.code, i.ticker or None, i.cusip or None, i.isin or None,
                                  i.sponsor or None, i.vintage_year, i.currency])
        price = i.prices.filter(as_of__lte=as_of).order_by("-as_of").first()
        if price is not None:
            out["Prices"].append([i.ticker or i.cusip or i.name, D(price.as_of), price.price.normalize()])
    for a in accounts:
        state = replay(a, as_of)
        out["Accounts"].append([a.name, _owner_name(a.owner), a.get_account_type_display(), a.get_tax_treatment_display(), a.custodian or None,
                                a.external_id or None, a.account_number_last4 or None, "Yes" if a.tracks_cash else "No", D(a.opened_on),
                                state.cash if a.tracks_cash and state.cash else None])
        commitment = getattr(a, "commitment", None)
        for instrument_id, lots in state.lots.items():
            inst = Instrument.objects.get(pk=instrument_id)
            for lot in lots:
                if lot.quantity:
                    out["Positions"].append([a.name, inst.ticker or inst.name, lot.quantity.normalize(), lot.cost, D(lot.acquired_on)])
        for instrument_id, cost in state.cost.items():
            inst = Instrument.objects.get(pk=instrument_id)
            if commitment is not None and commitment.instrument_id == instrument_id:
                flows = a.transactions.filter(instrument=inst, trade_date__lte=as_of)
                total = lambda **kw: flows.filter(**kw).aggregate(s=Sum("amount"))["s"] or ZERO  # noqa: E731
                out["Commitments"].append([a.name, inst.name, commitment.amount, D(commitment.commitment_date), total(txn_type=T.CONTRIBUTION),
                                           total(txn_type=T.DISTRIBUTION_ROC), total(txn_type=T.DISTRIBUTION_ROC, is_recallable=True),
                                           total(txn_type__in=[T.DISTRIBUTION_INCOME, T.DISTRIBUTION_GAIN])])
            elif cost:
                out["Positions"].append([a.name, inst.name, None, cost, None])
            mark = a.valuations.filter(instrument=inst, as_of__lte=as_of, status=Valuation.Status.ACCEPTED).order_by("-as_of").first()
            if mark is not None:
                out["Marks"].append([a.name, inst.name, D(mark.as_of), mark.value, mark.get_method_display()])
    return out


def build_workbook(rows: dict[str, list[list[Any]]] | None = None) -> bytes:
    wb = Workbook()
    intro = wb.active
    intro.title = "Instructions"
    lines = [
        "Family onboarding workbook",
        "",
        "Fill in the sheets you need; leave the others empty. Keep the header row (row 1) as it is.",
        "Rows refer to each other by name: an account's owner is an entity name or a person 'First Last'.",
        "Choices accept the label shown in the dropdown or its code. Dates as YYYY-MM-DD. Amounts positive.",
        "Opening balances (opening cash, positions, fund flows to date) are booked on the opening-balance date you choose when uploading.",
        "Rows that already exist (same name) are left as they are; change existing records on the Records page.",
        "",
    ] + [f"{s.name}: {s.note}" for s in SHEETS]
    for line in lines:
        intro.append([line])
    intro["A1"].font = Font(bold=True, size=14)
    intro.column_dimensions["A"].width = 120
    for sheet in SHEETS:
        ws = wb.create_sheet(sheet.name)
        ws.append([c.header for c in sheet.cols])
        for j, c in enumerate(sheet.cols, start=1):
            cell = ws.cell(row=1, column=j)
            cell.font = Font(bold=True)
            cell.fill = HEADER_FILL
            note = (" Required." if c.required else "") + (f" {c.note}" if c.note else "")
            if note.strip():
                cell.comment = Comment(note.strip(), "Family Office")
            ws.column_dimensions[cell.column_letter].width = max(14, len(c.header) + 4)
            if c.kind in ("choice", "bool"):
                options = [str(lbl) for _, lbl in c.choices.choices] if c.kind == "choice" and c.choices else ["Yes", "No"]
                formula = '"' + ",".join(options) + '"'
                if len(formula) <= 255:  # Excel's limit for an inline list
                    dv = DataValidation(type="list", formula1=formula, allow_blank=True)
                    ws.add_data_validation(dv)
                    dv.add(f"{cell.column_letter}2:{cell.column_letter}1000")
        for row in (rows or {}).get(sheet.name, []):
            ws.append([float(v) if isinstance(v, Decimal) else v for v in row])
        ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
