"""Custodian file import: validate a file into an import batch, then commit it into the book of record.

    batch = stage(workspace, fmt=resolve_format(workspace, "pershing_style_activity"), content=raw_bytes)
    # review batch.issues and the counts; then
    commit(batch)

Staging stores the file and runs every check, including a trial write and a ledger replay, inside a
transaction that is rolled back, so the preview shows exactly what an import would do. Committing runs the
same checks again (another batch may have landed in between) and keeps the writes only if no check fails.
An import is all or nothing: one error rejects the batch, warnings don't.

Validation rules (REC-08):
- Field mapping: required columns present; dates, numbers and activity codes readable; accounts and
  securities known (or, when allowed, securities created from the file's description and type).
- Sign: the signed net amount and quantity must point the way the activity code says.
- Tie-out: principal and fees must add up to the net amount; quantity x price is checked against principal.
- Fee vs transfer: cash journals are classified by sign and description; withdrawals that read like fees,
  and fees that read like transfers, are flagged.
- Duplicates: each row gets a stable `source_ref` (the custodian's transaction ID, else a fingerprint of the
  row); rows already in the book are skipped, so re-importing a file or an overlapping file is safe. A
  repeated transaction ID within one file is an error.
- Ledger: after the trial write each touched account is replayed; a sale of more than is held fails.
"""

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from campfire_integration.models import Workspace
from investments.importers.formats import CASH_JOURNAL, IGNORE, REQUIRED, FileFormat, resolve_format
from investments.importers.parsing import (
    ParseError, decode, norm_account, norm_header, norm_text, parse_date, parse_decimal, read_table,
)
from investments.models import Account, ImportBatch, ImportIssue, Instrument, Price, Transaction
from investments.services.ledger import LedgerError, replay
from investments.taxonomy import ensure_default_taxonomy

T = Transaction.Type
S = ImportIssue.Severity
ZERO = Decimal(0)
CENT = Decimal("0.01")
SOURCE = "custodian_file"
# Unitized kinds whose price is per unit, so quantity x price should equal principal. (Bonds are quoted
# per 100 of face, which custodians express differently; they are left out of the check.)
PER_UNIT_PRICED = {Instrument.Kind.EQUITY, Instrument.Kind.FUND, Instrument.Kind.CASH_EQUIVALENT, Instrument.Kind.DIGITAL_ASSET}
PRICE_CHECK_TOLERANCE = Decimal("0.005")
PRICE_MOVE_WARN_PCT = Decimal("25")
DEFAULT_ASSET_CLASS = {
    Instrument.Kind.EQUITY: "public_equity", Instrument.Kind.FUND: "public_equity",
    Instrument.Kind.FIXED_INCOME: "fixed_income", Instrument.Kind.CASH_EQUIVALENT: "cash",
    Instrument.Kind.DIGITAL_ASSET: "collectibles",
}
INCOME_TYPES = {T.DIVIDEND, T.INTEREST, T.DEPOSIT}
CASH_OUT_TYPES = {T.FEE, T.WITHDRAWAL}
UNITS_IN = {T.BUY, T.TRANSFER_IN}
UNITS_OUT = {T.SELL, T.TRANSFER_OUT}
MAX_ISSUES_PER_CODE = 200  # a systematically wrong file shouldn't store 100k identical findings


class ImportFailed(Exception):
    pass


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    row: int | None = None


@dataclass
class NewSecurity:
    key: str  # CUSIP:... or SYMBOL:...
    name: str
    kind: str
    cusip: str
    ticker: str


@dataclass
class TxnRow:
    row: int
    account: Account
    instrument: Instrument | None
    new_security: NewSecurity | None
    txn_type: str
    trade_date: date
    settle_date: date | None
    quantity: Decimal | None
    amount: Decimal
    fees: Decimal
    acquired_on: date | None
    description: str
    source_ref: str


@dataclass
class PriceRow:
    row: int
    instrument: Instrument
    as_of: date
    price: Decimal


@dataclass
class Result:
    fmt: FileFormat
    issues: list[Issue] = field(default_factory=list)
    txns: list[TxnRow] = field(default_factory=list)
    prices: list[PriceRow] = field(default_factory=list)
    rows_total: int = 0
    rows_duplicate: int = 0
    rows_ignored: int = 0
    family_ids: set[int] = field(default_factory=set)

    def add(self, severity: str, code: str, message: str, row: int | None = None) -> None:
        self.issues.append(Issue(severity, code, message, row))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == S.ERROR]

    @property
    def rows_new(self) -> int:
        return len(self.txns) + len(self.prices)


# --------------------------------------------------------------------------------------------------- reading


class _Row:
    """One data line, with field access through the format's column mapping."""

    def __init__(self, line: int, cells: list[str], columns: dict[str, int]) -> None:
        self.line, self.cells, self.columns = line, cells, columns

    def text(self, name: str) -> str:
        i = self.columns.get(name)
        return self.cells[i].strip() if i is not None and i < len(self.cells) else ""


def _columns(fmt: FileFormat, headers: dict[str, int], result: Result) -> dict[str, int] | None:
    found = {f: headers[norm_header(h)] for f, h in fmt.columns.items() if norm_header(h) in headers}
    missing = []
    for alternatives in REQUIRED[fmt.kind]:
        if not any(f in found for f in alternatives):
            missing.append(" or ".join(repr(fmt.columns[f]) for f in alternatives if f in fmt.columns))
    if missing:
        result.add(S.ERROR, "missing_column", f"The file has no column {', '.join(missing)} (layout: {fmt.name}).")
        return None
    return found


def _num(row: _Row, name: str, result: Result) -> Decimal | None:
    try:
        return parse_decimal(row.text(name))
    except ParseError as e:
        result.add(S.ERROR, "bad_number", f"{name.replace('_', ' ').capitalize()}: {e}.", row.line)
        raise


def _date(row: _Row, name: str, fmt: FileFormat, result: Result) -> date | None:
    try:
        return parse_date(row.text(name), fmt.date_formats)
    except ParseError as e:
        result.add(S.ERROR, "bad_date", f"{name.replace('_', ' ').capitalize()}: {e}.", row.line)
        raise


# ------------------------------------------------------------------------------------------------- resolving


class _Securities:
    def __init__(self, workspace: Workspace) -> None:
        self.by_cusip: dict[str, Instrument] = {}
        self.by_ticker: dict[str, Instrument] = {}
        for inst in Instrument.objects.filter(workspace=workspace):
            if inst.cusip:
                self.by_cusip.setdefault(inst.cusip.upper(), inst)
            if inst.ticker:
                self.by_ticker.setdefault(inst.ticker.upper(), inst)

    def find(self, cusip: str, symbol: str) -> Instrument | None:
        return (cusip and self.by_cusip.get(cusip.upper())) or (symbol and self.by_ticker.get(symbol.upper())) or None


def _accounts(workspace: Workspace) -> dict[str, list[Account]]:
    by_number: dict[str, list[Account]] = defaultdict(list)
    for account in Account.objects.filter(family__workspace=workspace).exclude(external_id="").select_related("family"):
        by_number[norm_account(account.external_id)].append(account)
    return by_number


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    upper = norm_text(text)
    return any(k.upper() in upper for k in keywords)


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


# ------------------------------------------------------------------------------------------- transactions


def _validate_transactions(workspace: Workspace, fmt: FileFormat, text: str, result: Result, *,
                           create_securities: bool, today: date) -> None:
    table = read_table(text, delimiter=fmt.delimiter, skip_rows=fmt.skip_rows)
    if not table.rows:
        result.add(S.ERROR, "empty_file", "The file has no data rows.")
        return
    columns = _columns(fmt, table.headers, result)
    if columns is None:
        return
    accounts = _accounts(workspace)
    securities = _Securities(workspace)
    new_securities: dict[str, NewSecurity] = {}
    fingerprints: Counter[str] = Counter()
    seen_ids: dict[str, int] = {}
    result.rows_total = len(table.rows)

    for line, cells in table.rows:
        row = _Row(line, cells, columns)
        try:
            parsed = _transaction_row(row, fmt, result, accounts, securities, new_securities, create_securities, today)
        except ParseError:
            continue
        if parsed is None:
            continue
        txn_row, fingerprint, txn_id = parsed
        if txn_id:
            if txn_id in seen_ids:
                result.add(S.ERROR, "duplicate_id_in_file", f"Transaction ID {txn_id} also appears on line {seen_ids[txn_id]}.", line)
                continue
            seen_ids[txn_id] = line
            txn_row.source_ref = f"{fmt.namespace}:{txn_id}"
        else:
            # Identical lines in one file are separate transactions (two equal buys on a day); number them
            # so the same file imported again produces the same references.
            fingerprints[fingerprint] += 1
            txn_row.source_ref = f"row:{fingerprint}#{fingerprints[fingerprint]}"
        result.txns.append(txn_row)

    result.family_ids = {t.account.family_id for t in result.txns}
    _drop_duplicates(result)


def _transaction_row(row: _Row, fmt: FileFormat, result: Result, accounts: dict[str, list[Account]],
                     securities: _Securities, new_securities: dict[str, NewSecurity], create_securities: bool,
                     today: date) -> tuple[TxnRow, str, str] | None:
    line = row.line
    raw_code = norm_text(row.text("code"))
    target = fmt.codes.get(raw_code)
    if target is None:
        result.add(S.ERROR, "unknown_code", f"Activity code {raw_code or '(blank)'!r} is not mapped in the {fmt.name} layout.", line)
        return None
    if target == IGNORE:
        result.rows_ignored += 1
        return None

    # Field mapping: required values and readable numbers/dates.
    trade_date = _date(row, "trade_date", fmt, result)
    settle_date = _date(row, "settle_date", fmt, result)
    acquired_on = _date(row, "acquired_on", fmt, result)
    quantity = _num(row, "quantity", result)
    price = _num(row, "price", result)
    principal = _num(row, "principal", result)
    commission = _num(row, "commission", result) or ZERO
    fees = abs(_num(row, "fees", result) or ZERO) + abs(commission)
    net = _num(row, "net_amount", result)
    cost_basis = _num(row, "cost_basis", result)
    description = row.text("description")

    if trade_date is None:
        result.add(S.ERROR, "missing_value", "Trade date is blank.", line)
        return None
    if trade_date > today:
        result.add(S.ERROR, "future_date", f"Trade date {trade_date} is in the future.", line)
        return None
    if settle_date and settle_date < trade_date:
        result.add(S.ERROR, "settle_before_trade", f"Settlement date {settle_date} is before trade date {trade_date}.", line)
        return None

    number = row.text("account")
    matches = accounts.get(norm_account(number), [])
    if not matches:
        result.add(S.ERROR, "unknown_account", f"Account {number or '(blank)'} is not set up (no account has this custodian account number).", line)
        return None
    if len(matches) > 1:
        result.add(S.ERROR, "ambiguous_account", f"Account {number} matches {len(matches)} accounts: {', '.join(a.name for a in matches)}.", line)
        return None
    account = matches[0]
    if (account.opened_on and trade_date < account.opened_on) or (account.closed_on and trade_date > account.closed_on):
        result.add(S.WARNING, "outside_account_dates", f"{trade_date} is outside the dates {account.name} is open.", line)

    # Amounts: unsigned layouts carry direction in the code only.
    if not fmt.signed_amounts and net is not None:
        net = abs(net)
    if not fmt.signed_quantities and quantity is not None:
        quantity = abs(quantity)
    cash = net if net is not None else None  # signed cash effect as the custodian reports it

    # Fee vs transfer (REC-08): a cash journal becomes a fee, deposit or withdrawal.
    if target == CASH_JOURNAL:
        amount_for_sign = net if net is not None else principal
        if amount_for_sign is None or amount_for_sign == 0 or not fmt.signed_amounts:
            result.add(S.ERROR, "unclassifiable_journal", f"Cash journal {raw_code} needs a signed, non-zero amount to classify.", line)
            return None
        if amount_for_sign < 0 and _contains_any(description, fmt.fee_keywords):
            target = T.FEE
            result.add(S.WARNING, "fee_classified", f"Cash journal {description!r} booked as a fee (its description reads like one).", line)
        else:
            target = T.WITHDRAWAL if amount_for_sign < 0 else T.DEPOSIT
    txn_type = target
    if txn_type == T.WITHDRAWAL and _contains_any(description, fmt.fee_keywords):
        result.add(S.WARNING, "fee_like_withdrawal", f"Withdrawal {description!r} reads like a fee; it is booked as a withdrawal (not an expense).", line)
    if txn_type == T.FEE and _contains_any(description, fmt.transfer_keywords):
        result.add(S.WARNING, "transfer_like_fee", f"Fee {description!r} reads like a transfer; it is booked as an expense.", line)

    # Security.
    cusip, symbol = norm_text(row.text("cusip")), norm_text(row.text("symbol"))
    instrument = securities.find(cusip, symbol)
    new_security = None
    needs_security = txn_type in Transaction.SECURITY_TYPES or txn_type in Transaction.DISTRIBUTION_TYPES or txn_type == T.CONTRIBUTION
    if instrument is None and (cusip or symbol):
        key = f"CUSIP:{cusip}" if cusip else f"SYMBOL:{symbol}"
        if key in new_securities:
            new_security = new_securities[key]
        elif create_securities:
            new_security = _new_security(row, fmt, result, key, cusip, symbol)
            if new_security is None:
                return None
            new_securities[key] = new_security
            result.add(S.INFO, "new_security", f"New security {new_security.name} ({cusip or symbol}) will be created as {Instrument.Kind(new_security.kind).label}.", line)
        elif needs_security:
            result.add(S.ERROR, "unknown_security", f"Security {cusip or symbol} is not set up. Add it, or allow new securities to be created from the file.", line)
            return None
        else:
            # Income on a security nobody set up (e.g. a sweep fund) is still booked, without the security.
            result.add(S.WARNING, "untracked_security", f"Security {cusip or symbol} isn't set up; this {T(txn_type).label.lower()} is booked to the account without it.", line)
    elif instrument is None and needs_security:
        result.add(S.ERROR, "missing_value", f"A {T(txn_type).label.lower()} needs a CUSIP or symbol.", line)
        return None
    kind = instrument.kind if instrument else new_security.kind if new_security else None
    unitized = kind in Instrument.UNITIZED_KINDS if kind else False

    # Quantity direction and presence.
    if txn_type in UNITS_IN | UNITS_OUT:
        if unitized and not quantity:
            result.add(S.ERROR, "missing_value", f"A {T(txn_type).label.lower()} of a listed security needs a quantity.", line)
            return None
        if quantity and fmt.signed_quantities:
            expected = 1 if txn_type in UNITS_IN else -1
            if _sign(quantity) != expected:
                result.add(S.ERROR, "sign_mismatch", f"Quantity {quantity} points the wrong way for a {T(txn_type).label.lower()}.", line)
                return None
    quantity = abs(quantity) if quantity else None

    # Amounts and the tie-out between principal, fees and net.
    if txn_type == T.BUY:
        amount = abs(principal) if principal is not None else (abs(net) - fees if net is not None else None)
        expected_cash = -(amount + fees) if amount is not None else None
    elif txn_type == T.SELL:
        amount = abs(principal) if principal is not None else (abs(net) + fees if net is not None else None)
        expected_cash = amount - fees if amount is not None else None
    elif txn_type in (T.TRANSFER_IN, T.TRANSFER_OUT):
        amount = abs(cost_basis) if cost_basis is not None else (None if txn_type == T.TRANSFER_IN else ZERO)
        expected_cash = ZERO
        if amount is None:
            result.add(S.ERROR, "transfer_without_basis", "A security received by transfer needs its cost basis (Cost Basis column).", line)
            return None
        if net:
            result.add(S.ERROR, "transfer_with_cash", f"A securities transfer shouldn't move cash, but its net amount is {net}.", line)
            return None
        fees = ZERO
    else:  # cash in or out: income, deposits, fees, withdrawals, distributions, contributions
        reported = net if net is not None else principal
        amount = abs(reported) if reported is not None else None
        outflow = txn_type in CASH_OUT_TYPES or txn_type == T.CONTRIBUTION
        expected_cash = (-amount if outflow else amount) if amount is not None else None
        if txn_type in INCOME_TYPES and principal is not None and net is not None and abs(principal) != abs(net):
            result.add(S.WARNING, "gross_net_differs", f"Gross {abs(principal)} differs from net {abs(net)}; the net amount is booked (withholding isn't tracked yet).", line)
        cash = expected_cash if net is None else cash
        fees = ZERO
    if amount is None:
        result.add(S.ERROR, "missing_value", "No amount: principal and net amount are both blank.", line)
        return None
    if amount < 0:
        result.add(S.ERROR, "net_mismatch", f"Fees {fees} exceed the net amount {net}.", line)
        return None
    if fmt.signed_amounts and cash is not None and expected_cash is not None:
        if cash and expected_cash and _sign(cash) != _sign(expected_cash):
            result.add(S.ERROR, "sign_mismatch", f"Net amount {cash} points the wrong way for a {T(txn_type).label.lower()} (expected {'cash out' if expected_cash < 0 else 'cash in'}).", line)
            return None
        if abs(cash - expected_cash) > CENT:
            result.add(S.ERROR, "net_mismatch", f"Net amount {cash} doesn't tie to principal {amount} and fees {fees} (expected {expected_cash}).", line)
            return None
    if price and quantity and principal and kind in PER_UNIT_PRICED:
        implied = (quantity * price).quantize(CENT)
        if abs(implied - abs(principal)) > max(Decimal(1), abs(principal) * PRICE_CHECK_TOLERANCE):
            result.add(S.WARNING, "price_check", f"Quantity {quantity} x price {price} = {implied}, but principal is {abs(principal)}.", line)

    txn_id = row.text("txn_id")
    fingerprint = hashlib.sha256("|".join([
        norm_account(row.text("account")), trade_date.isoformat(), raw_code, cusip, symbol,
        str(quantity or ""), str(amount), str(net if net is not None else ""), norm_text(description),
    ]).encode()).hexdigest()[:24]
    txn = TxnRow(
        row=line, account=account, instrument=instrument, new_security=new_security, txn_type=txn_type,
        trade_date=trade_date, settle_date=settle_date, quantity=quantity, amount=amount.quantize(CENT),
        fees=fees.quantize(CENT), acquired_on=acquired_on, description=description[:255], source_ref="",
    )
    return txn, fingerprint, txn_id


def _new_security(row: _Row, fmt: FileFormat, result: Result, key: str, cusip: str, symbol: str) -> NewSecurity | None:
    raw_type = norm_text(row.text("security_type"))
    kind = fmt.security_types.get(raw_type)
    if kind is None:
        result.add(S.ERROR, "unknown_security_type", f"Security {cusip or symbol}: type {raw_type or '(blank)'!r} isn't mapped, so it can't be created.", row.line)
        return None
    if kind not in DEFAULT_ASSET_CLASS:
        result.add(S.ERROR, "unknown_security", f"Security {cusip or symbol} ({Instrument.Kind(kind).label}) must be set up by hand.", row.line)
        return None
    if len(cusip) > 9 or len(symbol) > 20:
        result.add(S.ERROR, "bad_identifier", f"Security identifier {cusip or symbol} is too long.", row.line)
        return None
    name = row.text("security_name") or symbol or cusip
    return NewSecurity(key=key, name=name[:255], kind=kind, cusip=cusip, ticker=symbol)


def _drop_duplicates(result: Result) -> None:
    refs_by_account: dict[int, set[str]] = defaultdict(set)
    for t in result.txns:
        refs_by_account[t.account.pk].add(t.source_ref)
    existing: set[tuple[int, str]] = set()
    for account_id, refs in refs_by_account.items():
        existing |= {
            (account_id, ref)
            for ref in Transaction.objects.filter(account_id=account_id, source_ref__in=refs).values_list("source_ref", flat=True)
        }
    kept = [t for t in result.txns if (t.account.pk, t.source_ref) not in existing]
    result.rows_duplicate += len(result.txns) - len(kept)
    result.txns = kept


def _write_transactions(batch: ImportBatch, result: Result) -> None:
    created: dict[str, Instrument] = {}
    classes = ensure_default_taxonomy(batch.workspace) if any(t.new_security for t in result.txns) else {}
    rows = []
    for t in result.txns:
        instrument = t.instrument
        if instrument is None and t.new_security is not None:
            ns = t.new_security
            if ns.key not in created:
                created[ns.key] = Instrument.objects.create(
                    workspace=batch.workspace, name=ns.name, kind=ns.kind, asset_class=classes[DEFAULT_ASSET_CLASS[ns.kind]],
                    ticker=ns.ticker, cusip=ns.cusip, is_unitized=ns.kind in Instrument.UNITIZED_KINDS,
                )
            instrument = created[ns.key]
        rows.append(Transaction(
            account=t.account, instrument=instrument, txn_type=t.txn_type, trade_date=t.trade_date,
            settle_date=t.settle_date, quantity=t.quantity, amount=t.amount, fees=t.fees, acquired_on=t.acquired_on,
            description=t.description, batch=batch, source_ref=t.source_ref,
        ))
    Transaction.objects.bulk_create(rows)
    _check_ledger(result)


def _check_ledger(result: Result) -> None:
    """Replay every touched account through its last transaction; an oversold position fails the import."""
    for account in {t.account.pk: t.account for t in result.txns}.values():
        last = account.transactions.aggregate(d=Max("trade_date"))["d"]
        try:
            replay(account, last)
        except LedgerError as e:
            result.add(S.ERROR, "ledger", f"After this import the ledger doesn't balance: {e}.")


# ------------------------------------------------------------------------------------------------- prices


def _validate_prices(workspace: Workspace, fmt: FileFormat, text: str, result: Result, *, today: date) -> None:
    table = read_table(text, delimiter=fmt.delimiter, skip_rows=fmt.skip_rows)
    if not table.rows:
        result.add(S.ERROR, "empty_file", "The file has no data rows.")
        return
    columns = _columns(fmt, table.headers, result)
    if columns is None:
        return
    securities = _Securities(workspace)
    result.rows_total = len(table.rows)
    in_file: dict[tuple[int, date], tuple[Decimal, int]] = {}
    unknown: list[str] = []
    for line, cells in table.rows:
        row = _Row(line, cells, columns)
        try:
            as_of = _date(row, "as_of", fmt, result)
            price = _num(row, "price", result)
        except ParseError:
            continue
        cusip, symbol = norm_text(row.text("cusip")), norm_text(row.text("symbol"))
        if as_of is None or price is None:
            result.add(S.ERROR, "missing_value", "Price date and price are required.", line)
            continue
        if as_of > today:
            result.add(S.ERROR, "future_date", f"Price date {as_of} is in the future.", line)
            continue
        if price <= 0:
            result.add(S.ERROR, "bad_price", f"Price {price} must be positive.", line)
            continue
        instrument = securities.find(cusip, symbol)
        if instrument is None:
            # Custodian price files list everything they price; securities nobody holds here are skipped.
            unknown.append(cusip or symbol or f"line {line}")
            result.rows_ignored += 1
            continue
        key = (instrument.pk, as_of)
        if key in in_file:
            prior, prior_line = in_file[key]
            if prior != price:
                result.add(S.ERROR, "conflicting_price", f"{instrument}: {price} on {as_of} conflicts with {prior} on line {prior_line}.", line)
            else:
                result.rows_duplicate += 1
            continue
        in_file[key] = (price, line)
        result.prices.append(PriceRow(line, instrument, as_of, price))
    if unknown:
        sample = ", ".join(unknown[:10]) + (f" and {len(unknown) - 10} more" if len(unknown) > 10 else "")
        result.add(S.INFO, "untracked_security", f"{len(unknown)} price(s) skipped for securities not set up here: {sample}.")

    recorded = {
        (p.instrument_id, p.as_of): p
        for p in Price.objects.filter(instrument_id__in={p.instrument.pk for p in result.prices}, as_of__in={p.as_of for p in result.prices})
    }
    kept = []
    for p in result.prices:
        existing = recorded.get((p.instrument.pk, p.as_of))
        if existing is not None:
            if existing.price == p.price:
                result.rows_duplicate += 1
            else:
                result.add(S.ERROR, "conflicting_price", f"{p.instrument}: {p.price} on {p.as_of} conflicts with {existing.price} already recorded ({existing.source}).", p.row)
            continue
        prior = Price.objects.filter(instrument=p.instrument, as_of__lt=p.as_of).order_by("-as_of").first()
        if prior is not None and prior.price:
            move = ((p.price - prior.price) / prior.price * 100).quantize(CENT)
            if abs(move) > PRICE_MOVE_WARN_PCT:
                result.add(S.WARNING, "price_move", f"{p.instrument}: {p.price} on {p.as_of} is {move}% from {prior.price} on {prior.as_of}.", p.row)
        kept.append(p)
    result.prices = kept


def _write_prices(batch: ImportBatch, result: Result) -> None:
    Price.objects.bulk_create([
        Price(instrument=p.instrument, as_of=p.as_of, price=p.price, source=SOURCE, batch=batch) for p in result.prices
    ])


# --------------------------------------------------------------------------------------------- public API


class _Rollback(Exception):
    pass


def validate(batch: ImportBatch, fmt: FileFormat, *, write: bool, today: date | None = None) -> Result:
    """Run every check on the batch's file. With write=True keep the writes if nothing failed."""
    today = today or timezone.localdate()
    result = Result(fmt)
    if fmt.kind != batch.kind:
        result.add(S.ERROR, "wrong_format", f"{fmt.name} reads {fmt.kind}, but this batch is {batch.kind}.")
        return result
    if fmt.kind == ImportBatch.Kind.TRANSACTIONS:
        _validate_transactions(batch.workspace, fmt, batch.content, result,
                               create_securities=bool(batch.options.get("create_securities")), today=today)
    else:
        _validate_prices(batch.workspace, fmt, batch.content, result, today=today)
    if batch.file_sha256:
        earlier = (ImportBatch.objects.filter(workspace=batch.workspace, file_sha256=batch.file_sha256, status=ImportBatch.Status.COMMITTED)
                   .exclude(pk=batch.pk).order_by("-committed_at").first())
        if earlier is not None:
            result.add(S.WARNING, "file_already_imported", f"This exact file was imported on {timezone.localdate(earlier.committed_at)} (batch {earlier.pk}).")
    if result.errors:
        return result
    # Trial write: the ledger check needs the rows in place. Rolled back unless committing cleanly.
    try:
        with transaction.atomic():
            if fmt.kind == ImportBatch.Kind.TRANSACTIONS:
                _write_transactions(batch, result)
            else:
                _write_prices(batch, result)
            if not write or result.errors:
                raise _Rollback
    except _Rollback:
        pass
    except IntegrityError:
        result.add(S.ERROR, "concurrent_import", "Some of these rows were imported by another batch meanwhile. Validate the file again.")
    return result


def _record(batch: ImportBatch, result: Result, status: str) -> ImportBatch:
    batch.issues.all().delete()
    per_code: Counter[str] = Counter()
    issues = []
    for i in result.issues:
        per_code[i.code] += 1
        if per_code[i.code] <= MAX_ISSUES_PER_CODE:
            issues.append(ImportIssue(batch=batch, row_number=i.row, severity=i.severity, code=i.code, message=i.message[:500]))
    for code, count in per_code.items():
        if count > MAX_ISSUES_PER_CODE:
            issues.append(ImportIssue(batch=batch, severity=S.INFO, code=code, message=f"{count - MAX_ISSUES_PER_CODE} more {code} findings not listed."))
    ImportIssue.objects.bulk_create(issues)
    batch.rows_total, batch.rows_new = result.rows_total, result.rows_new
    batch.rows_duplicate, batch.rows_ignored = result.rows_duplicate, result.rows_ignored
    if len(result.family_ids) == 1:
        batch.family_id = next(iter(result.family_ids))
    batch.status = status
    if status == ImportBatch.Status.COMMITTED:
        batch.committed_at = timezone.now()
    batch.save()
    return batch


def stage(workspace: Workspace, *, fmt: FileFormat, content: bytes | str, file_name: str = "", created_by: str = "",
          create_securities: bool = False, today: date | None = None, created_by_id: str = "") -> ImportBatch:
    """Store the file as a batch and validate it without importing anything."""
    text = decode(content)
    with transaction.atomic():
        batch = ImportBatch.objects.create(
            workspace=workspace, source=SOURCE, reference=file_name, kind=fmt.kind, file_format=fmt.code,
            file_name=file_name, file_sha256=hashlib.sha256(text.encode()).hexdigest(), content=text,
            options={"create_securities": create_securities}, created_by=created_by, created_by_id=created_by_id,
            status=ImportBatch.Status.VALIDATED,
        )
        result = validate(batch, fmt, write=False, today=today)
        return _record(batch, result, ImportBatch.Status.REJECTED if result.errors else ImportBatch.Status.VALIDATED)


def commit(batch: ImportBatch, *, fmt: FileFormat | None = None, today: date | None = None) -> ImportBatch:
    """Import a validated batch. Re-runs every check; any error rejects the batch and imports nothing."""
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.status != ImportBatch.Status.VALIDATED:
            raise ImportFailed(f"Batch {batch.pk} is {batch.get_status_display().lower()}; only a validated batch can be imported.")
        fmt = fmt or resolve_format(batch.workspace, batch.file_format)
        result = validate(batch, fmt, write=True, today=today)
        return _record(batch, result, ImportBatch.Status.REJECTED if result.errors else ImportBatch.Status.COMMITTED)


def discard(batch: ImportBatch) -> ImportBatch:
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.status != ImportBatch.Status.VALIDATED:
            raise ImportFailed(f"Batch {batch.pk} is {batch.get_status_display().lower()}; only a validated batch can be discarded.")
        batch.status = ImportBatch.Status.DISCARDED
        batch.save(update_fields=["status"])
        return batch
