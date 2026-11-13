"""File layouts for custodian imports: which column holds what, what the activity codes mean, sign conventions.

A layout is a built-in preset, optionally overridden per workspace (`CustodianFormat.config`), so a new
custodian or a changed extract is configuration, not code. Override keys are the `FileFormat` fields:

    {"columns": {"net_amount": "Amount"},          # merged into the preset's columns
     "codes": {"CDV": "dividend", "XFER": "cash_journal"},   # merged into the preset's codes
     "date_formats": ["%d/%m/%Y"], "fee_keywords": [...], ...}  # replace the preset's value

Activity codes map to a `Transaction.Type` value, to `cash_journal` (a cash movement classified as a fee,
deposit or withdrawal by its sign and description: REC-08 fee-vs-transfer), or to `ignore` (memo lines).

The Pershing-style layout is illustrative: column names and codes follow common custodian activity
extracts and must be checked against a real file before go-live (then adjusted here or per workspace).
"""

from dataclasses import dataclass, field, fields, replace
from typing import Any

from investments.models import CustodianFormat, ImportBatch, Instrument, Transaction

CASH_JOURNAL = "cash_journal"
IGNORE = "ignore"

TRANSACTION_FIELDS = (
    "account", "trade_date", "settle_date", "code", "description", "cusip", "symbol", "security_name",
    "security_type", "quantity", "price", "principal", "commission", "fees", "net_amount", "cost_basis",
    "acquired_on", "txn_id",
)
PRICE_FIELDS = ("cusip", "symbol", "as_of", "price")

KINDS = {ImportBatch.Kind.TRANSACTIONS.value: TRANSACTION_FIELDS, ImportBatch.Kind.PRICES.value: PRICE_FIELDS}
REQUIRED = {
    ImportBatch.Kind.TRANSACTIONS.value: (("account",), ("trade_date",), ("code",), ("net_amount", "principal")),
    ImportBatch.Kind.PRICES.value: (("as_of",), ("price",), ("cusip", "symbol")),
}
CODE_TARGETS = {t.value for t in Transaction.Type} | {CASH_JOURNAL, IGNORE}
INSTRUMENT_KINDS = {k.value for k in Instrument.Kind}


class FormatError(ValueError):
    pass


@dataclass(frozen=True)
class FileFormat:
    code: str
    name: str
    kind: str
    columns: dict[str, str]  # field -> column header in the file
    # IDs from the same custodian de-duplicate across layouts that share a namespace.
    namespace: str = ""
    codes: dict[str, str] = field(default_factory=dict)
    date_formats: tuple[str, ...] = ("%m/%d/%Y", "%Y-%m-%d")
    # True: a negative net amount is cash out of the account (and a negative quantity is units out).
    # False: amounts and quantities are unsigned and the code alone gives the direction.
    signed_amounts: bool = True
    signed_quantities: bool = True
    fee_keywords: tuple[str, ...] = ()
    transfer_keywords: tuple[str, ...] = ()
    security_types: dict[str, str] = field(default_factory=dict)  # file security type -> Instrument.Kind
    delimiter: str = ","
    skip_rows: int = 0  # lines above the header row

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise FormatError(f"{self.code}: kind must be one of {', '.join(KINDS)}.")
        unknown = set(self.columns) - set(KINDS[self.kind])
        if unknown:
            raise FormatError(f"{self.code}: unknown fields in columns: {', '.join(sorted(unknown))}.")
        for alternatives in REQUIRED[self.kind]:
            if not any(self.columns.get(f) for f in alternatives):
                raise FormatError(f"{self.code}: columns must map {' or '.join(alternatives)}.")
        bad_codes = {c: t for c, t in self.codes.items() if t not in CODE_TARGETS}
        if bad_codes:
            raise FormatError(f"{self.code}: codes map to unknown types: {bad_codes}.")
        bad_kinds = {s: k for s, k in self.security_types.items() if k not in INSTRUMENT_KINDS}
        if bad_kinds:
            raise FormatError(f"{self.code}: security_types map to unknown instrument kinds: {bad_kinds}.")
        if len(self.delimiter) != 1:
            raise FormatError(f"{self.code}: delimiter must be one character.")
        # Codes and security types are matched case-insensitively.
        object.__setattr__(self, "codes", {k.strip().upper(): v for k, v in self.codes.items()})
        object.__setattr__(self, "security_types", {k.strip().upper(): v for k, v in self.security_types.items()})
        if not self.namespace:
            object.__setattr__(self, "namespace", self.code)

    def with_overrides(self, *, code: str, name: str, config: dict[str, Any]) -> "FileFormat":
        allowed = {f.name for f in fields(self)} - {"code", "name", "kind"}
        unknown = set(config) - allowed
        if unknown:
            raise FormatError(f"{code}: unknown settings: {', '.join(sorted(unknown))}.")
        changes: dict[str, Any] = {}
        for key, value in config.items():
            current = getattr(self, key)
            if isinstance(current, dict):
                if not isinstance(value, dict):
                    raise FormatError(f"{code}: {key} must be an object.")
                changes[key] = {**current, **value}
            elif isinstance(current, tuple):
                if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
                    raise FormatError(f"{code}: {key} must be a list of strings.")
                changes[key] = tuple(value)
            elif type(current) is not type(value):
                raise FormatError(f"{code}: {key} must be a {type(current).__name__}.")
            else:
                changes[key] = value
        return replace(self, code=code, name=name, **changes)


T = Transaction.Type
K = Instrument.Kind

PRESETS: dict[str, FileFormat] = {
    f.code: f
    for f in (
        FileFormat(
            code="pershing_style_activity",
            name="Pershing-style activity file",
            kind="transactions",
            namespace="pershing",
            columns={
                "account": "Account Number", "trade_date": "Trade Date", "settle_date": "Settlement Date",
                "code": "Transaction Code", "description": "Description", "cusip": "CUSIP", "symbol": "Symbol",
                "security_name": "Security Description", "security_type": "Security Type", "quantity": "Quantity",
                "price": "Price", "principal": "Principal Amount", "commission": "Commission", "fees": "Fees",
                "net_amount": "Net Amount", "cost_basis": "Cost Basis", "acquired_on": "Original Acquisition Date",
                "txn_id": "Transaction ID",
            },
            codes={
                "BUY": T.BUY, "SEL": T.SELL, "DIV": T.DIVIDEND, "INT": T.INTEREST, "FEE": T.FEE,
                "DEP": T.DEPOSIT, "WD": T.WITHDRAWAL, "REC": T.TRANSFER_IN, "DEL": T.TRANSFER_OUT,
                "JNL": CASH_JOURNAL, "MEMO": IGNORE,
            },
            fee_keywords=("FEE", "ADVISORY", "CUSTODY CHARGE", "MANAGEMENT CHG"),
            transfer_keywords=("TRANSFER", "WIRE", "JOURNAL TO", "JOURNAL FROM", "ACH"),
            security_types={"EQ": K.EQUITY, "ETF": K.FUND, "MF": K.FUND, "FI": K.FIXED_INCOME, "MM": K.CASH_EQUIVALENT},
        ),
        FileFormat(
            code="pershing_style_prices",
            name="Pershing-style price file",
            kind="prices",
            namespace="pershing",
            columns={"cusip": "CUSIP", "symbol": "Symbol", "as_of": "Price Date", "price": "Price"},
        ),
        # A plain spreadsheet layout: type column holds this app's own transaction types, amounts unsigned.
        FileFormat(
            code="generic_transactions",
            name="Generic transactions (spreadsheet)",
            kind="transactions",
            columns={
                "account": "account", "trade_date": "trade_date", "settle_date": "settle_date", "code": "type",
                "description": "description", "cusip": "cusip", "symbol": "symbol", "quantity": "quantity",
                "price": "price", "principal": "amount", "fees": "fees", "cost_basis": "cost_basis",
                "acquired_on": "acquired_on", "txn_id": "id",
            },
            codes={t.value: t.value for t in T},
            date_formats=("%Y-%m-%d", "%m/%d/%Y"),
            signed_amounts=False,
            signed_quantities=False,
        ),
        FileFormat(
            code="generic_prices",
            name="Generic prices (spreadsheet)",
            kind="prices",
            columns={"cusip": "cusip", "symbol": "symbol", "as_of": "date", "price": "price"},
            date_formats=("%Y-%m-%d", "%m/%d/%Y"),
        ),
    )
}


def from_model(obj: CustodianFormat) -> FileFormat:
    preset = PRESETS.get(obj.preset)
    if preset is None:
        raise FormatError(f"Unknown preset {obj.preset!r}; choose one of {', '.join(PRESETS)}.")
    if not isinstance(obj.config, dict):
        raise FormatError("config must be an object.")
    return preset.with_overrides(code=obj.code, name=obj.name, config=obj.config)


def resolve_format(workspace, code: str) -> FileFormat:  # type: ignore[no-untyped-def]
    """A workspace's own layout with this code, else the built-in preset."""
    obj = CustodianFormat.objects.filter(workspace=workspace, code=code, is_active=True).first()
    if obj is not None:
        return from_model(obj)
    if code in PRESETS:
        return PRESETS[code]
    raise FormatError(f"Unknown file format {code!r}.")


def available_formats(workspace) -> list[FileFormat]:  # type: ignore[no-untyped-def]
    own = {}
    for obj in CustodianFormat.objects.filter(workspace=workspace, is_active=True):
        try:
            own[obj.code] = from_model(obj)
        except FormatError:
            continue  # a broken saved layout shouldn't hide the others; admin validation reports it
    return [*own.values(), *(p for code, p in PRESETS.items() if code not in own)]
