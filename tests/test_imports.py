from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Family
from investments.demo import ACTIVITY_HEADER, _csv, alder_activity_file, alder_price_file, seed_alder_investments
from investments.importers.custodian import ImportFailed, commit, discard, stage
from investments.importers.formats import PRESETS, FileFormat, FormatError, resolve_format
from investments.importers.parsing import ParseError, parse_decimal
from investments.models import Account, CustodianFormat, ImportBatch, Instrument, Price, Transaction
from investments.services.ledger import replay
from investments.services.valuation import holdings

TODAY = date(2026, 6, 30)
ACTIVITY = PRESETS["pershing_style_activity"]
PRICES = PRESETS["pershing_style_prices"]
GENERIC = PRESETS["generic_transactions"]
B = ImportBatch.Status
RRT = "4BX-104417"
DYN = "4BX-301290"


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    return family


def d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).strftime("%m/%d/%Y")


def activity(*rows: dict[str, str]) -> str:
    """A Pershing-style file from rows given by header name; blanks for the rest."""
    return _csv(ACTIVITY_HEADER, [[r.get(h, "") for h in ACTIVITY_HEADER] for r in rows])


def row(**kw: str) -> dict[str, str]:
    names = {
        "account": "Account Number", "trade": "Trade Date", "settle": "Settlement Date", "code": "Transaction Code",
        "desc": "Description", "symbol": "Symbol", "name": "Security Description", "type": "Security Type",
        "qty": "Quantity", "price": "Price", "principal": "Principal Amount", "commission": "Commission",
        "net": "Net Amount", "basis": "Cost Basis", "acquired": "Original Acquisition Date", "id": "Transaction ID",
    }
    out = {"Account Number": RRT, "Trade Date": d(-3)}
    out.update({names[k]: v for k, v in kw.items()})
    return out


def run(workspace: Workspace, content: str, fmt: FileFormat = ACTIVITY, *, create: bool = False) -> ImportBatch:
    batch = stage(workspace, fmt=fmt, content=content, file_name="test.csv", create_securities=create, today=TODAY)
    if batch.status == B.VALIDATED:
        batch = commit(batch, fmt=fmt, today=TODAY)
    return batch


def codes(batch: ImportBatch, severity: str | None = None) -> list[str]:
    qs = batch.issues.all()
    return [i.code for i in (qs.filter(severity=severity) if severity else qs)]


def acct(family: Family, number: str) -> Account:
    return Account.objects.get(family=family, external_id=number)


# ------------------------------------------------------------------------------------------- happy path


@pytest.mark.django_db
def test_sample_month_file_imports_with_lineage(alder: Family) -> None:
    ws = alder.workspace
    content = alder_activity_file(TODAY)
    before = Transaction.objects.count()

    rejected = stage(ws, fmt=ACTIVITY, content=content, file_name="activity.csv", today=TODAY)
    assert rejected.status == B.REJECTED and codes(rejected, "error") == ["unknown_security"]  # STTR not set up
    assert Transaction.objects.count() == before and not Instrument.objects.filter(ticker="STTR").exists()

    batch = stage(ws, fmt=ACTIVITY, content=content, file_name="activity.csv", today=TODAY, create_securities=True)
    assert batch.status == B.VALIDATED
    assert Transaction.objects.count() == before  # validating writes nothing
    assert (batch.rows_total, batch.rows_new, batch.rows_duplicate, batch.rows_ignored) == (9, 8, 0, 1)
    assert sorted(codes(batch)) == ["fee_classified", "new_security"]

    batch = commit(batch, today=TODAY)
    assert batch.status == B.COMMITTED and batch.committed_at is not None
    assert batch.family == alder  # every account in the file is the Alder family's
    assert Transaction.objects.count() == before + 8

    rrt = acct(alder, RRT)
    fee = rrt.transactions.get(source_ref="pershing:BX-0003")
    assert (fee.txn_type, fee.amount, fee.batch) == (Transaction.Type.FEE, Decimal("6250.00"), batch)
    buy = rrt.transactions.get(source_ref="pershing:BX-0002")
    assert (buy.amount, buy.fees, buy.quantity) == (Decimal("122000.00"), Decimal("15.00"), Decimal("2000"))
    state = replay(rrt, TODAY)
    intd = Instrument.objects.get(ticker="INTD")
    assert state.quantity(intd.pk) == Decimal("10000")
    assert state.cash == Decimal("611475.00") + Decimal("4850") - Decimal("122015") - Decimal("6250")
    assert state.expenses == Decimal("6250.00")

    sttr = Instrument.objects.get(workspace=ws, ticker="STTR")
    assert (sttr.kind, sttr.is_unitized, sttr.asset_class.code) == (Instrument.Kind.FUND, True, "public_equity")


@pytest.mark.django_db
def test_reimport_is_idempotent(alder: Family) -> None:
    ws = alder.workspace
    content = alder_activity_file(TODAY)
    run(ws, content, create=True)
    count = Transaction.objects.count()

    again = run(ws, content, create=True)
    assert again.status == B.COMMITTED
    assert (again.rows_new, again.rows_duplicate) == (0, 8)
    assert "file_already_imported" in codes(again, "warning")
    assert again.family == alder  # even when every row was a duplicate
    assert Transaction.objects.count() == count


@pytest.mark.django_db
def test_rows_without_ids_dedupe_by_fingerprint_and_keep_identical_trades(alder: Family) -> None:
    ws = alder.workspace
    header = "account,trade_date,type,symbol,quantity,amount,fees\n"
    buy = f"4BX-104417,{(TODAY - timedelta(days=5)).isoformat()},buy,USTM,10,2700.00,1.00\n"
    first = run(ws, header + buy + buy, GENERIC)  # two identical buys the same day are two trades
    assert (first.status, first.rows_new) == (B.COMMITTED, 2)

    second = run(ws, header + buy + buy + buy, GENERIC)  # an overlapping, longer file
    assert (second.rows_new, second.rows_duplicate) == (1, 2)
    assert acct(alder, RRT).transactions.filter(batch__in=[first, second]).count() == 3


# ------------------------------------------------------------------------------------- REC-08 rules


@pytest.mark.django_db
@pytest.mark.parametrize(("line", "code"), [
    (row(code="DIV", symbol="USTM", net="-500.00"), "sign_mismatch"),             # dividend taking cash out
    (row(code="SEL", symbol="USTM", qty="10", principal="2700", net="2700"), "sign_mismatch"),  # sale with units in
    (row(code="BUY", symbol="USTM", qty="10", principal="2700", commission="10", net="-2700"), "net_mismatch"),
    (row(code="XYZ", net="1.00"), "unknown_code"),
    (row(code="DIV", symbol="USTM", net="1.00", trade="31/12/2025"), "bad_date"),
    (row(code="DIV", symbol="USTM", net="12,3x"), "bad_number"),
    (row(code="DIV", symbol="USTM", net="1.00", account="9ZZ-000001"), "unknown_account"),
    (row(code="DIV", symbol="USTM", net="1.00", trade=d(2)), "future_date"),
    (row(code="BUY", symbol="USTM", qty="1", principal="270", net="-270", settle=d(-5)), "settle_before_trade"),
    (row(code="REC", symbol="USTM", qty="10"), "transfer_without_basis"),
    (row(code="BUY", symbol="NOPE", qty="1", principal="10", net="-10"), "unknown_security"),
    (row(code="BUY", symbol="USTM", principal="10", net="-10"), "missing_value"),  # listed security, no quantity
    (row(code="SEL", symbol="USTM", qty="-99999", principal="1", net="1"), "ledger"),  # oversell
])
def test_rejected_rows(alder: Family, line: dict[str, str], code: str) -> None:
    before = Transaction.objects.count()
    good = row(code="DIV", symbol="USTM", net="10.00", id="OK-1")
    batch = run(alder.workspace, activity(good, line))
    assert batch.status == B.REJECTED
    assert code in codes(batch, "error")
    assert Transaction.objects.count() == before  # all or nothing: the good row isn't imported either


@pytest.mark.django_db
def test_missing_column_and_empty_file(alder: Family) -> None:
    batch = run(alder.workspace, "Account Number,Trade Date,Net Amount\nX,01/01/2026,1\n")
    assert codes(batch) == ["missing_column"]
    assert "'Transaction Code'" in batch.issues.get().message
    assert codes(run(alder.workspace, ",".join(ACTIVITY_HEADER) + "\n")) == ["empty_file"]


@pytest.mark.django_db
def test_duplicate_transaction_id_in_file(alder: Family) -> None:
    r = row(code="DIV", symbol="USTM", net="10.00", id="X-1")
    batch = run(alder.workspace, activity(r, {**r, "Net Amount": "11.00"}))
    assert codes(batch, "error") == ["duplicate_id_in_file"]


@pytest.mark.django_db
def test_fee_versus_transfer(alder: Family) -> None:
    batch = run(alder.workspace, activity(
        row(code="JNL", desc="CUSTODY CHARGE", net="-100.00", id="J-1"),         # journal out + fee words -> fee
        row(code="JNL", desc="JOURNAL FROM 4BX-208802", net="250.00", id="J-2"),  # journal in -> deposit
        row(code="JNL", desc="JOURNAL TO 4BX-208802", net="-75.00", id="J-3"),    # journal out -> withdrawal
        row(code="WD", desc="ADVISORY FEE PAID BY CHECK", net="-40.00", id="J-4"),  # flagged, still a withdrawal
        row(code="FEE", desc="WIRE TRANSFER OUT", net="-30.00", id="J-5"),         # flagged, still a fee
    ))
    assert batch.status == B.COMMITTED
    types = dict(Transaction.objects.filter(batch=batch).values_list("source_ref", "txn_type"))
    assert types == {"pershing:J-1": "fee", "pershing:J-2": "deposit", "pershing:J-3": "withdrawal",
                     "pershing:J-4": "withdrawal", "pershing:J-5": "fee"}
    assert sorted(codes(batch, "warning")) == ["fee_classified", "fee_like_withdrawal", "transfer_like_fee"]


@pytest.mark.django_db
def test_soft_checks_warn_but_import(alder: Family) -> None:
    batch = run(alder.workspace, activity(
        row(code="BUY", symbol="USTM", qty="10", price="300.00", principal="2700.00", net="-2700.00", id="W-1"),
        row(code="DIV", symbol="USTM", principal="100.00", net="85.00", id="W-2"),
        row(code="DIV", symbol="SWEEPX", net="3.10", id="W-3"),
    ))
    assert batch.status == B.COMMITTED
    assert sorted(codes(batch, "warning")) == ["gross_net_differs", "price_check", "untracked_security"]
    assert Transaction.objects.get(source_ref="pershing:W-2").amount == Decimal("85.00")
    assert Transaction.objects.get(source_ref="pershing:W-3").instrument is None


@pytest.mark.django_db
def test_transfer_in_with_basis_and_parenthesised_amounts(alder: Family) -> None:
    batch = run(alder.workspace, activity(
        row(code="REC", symbol="INTD", qty="100", basis="5,000.00", acquired=d(-900), id="T-1"),
        row(code="WD", net="(1,000.00)", id="T-2"),
    ))
    assert batch.status == B.COMMITTED, codes(batch)
    rec = Transaction.objects.get(source_ref="pershing:T-1")
    assert (rec.txn_type, rec.amount, rec.acquired_on) == ("transfer_in", Decimal("5000.00"), TODAY - timedelta(days=900))
    assert Transaction.objects.get(source_ref="pershing:T-2").amount == Decimal("1000.00")


# ------------------------------------------------------------------------------------- batch lifecycle


@pytest.mark.django_db
def test_commit_revalidates_and_lifecycle(alder: Family) -> None:
    ws = alder.workspace
    content = activity(row(code="DIV", symbol="USTM", net="10.00", id="L-1"), row(code="DIV", symbol="USTM", net="20.00", id="L-2"))
    staged = stage(ws, fmt=ACTIVITY, content=content, today=TODAY)
    run(ws, activity(row(code="DIV", symbol="USTM", net="10.00", id="L-1")))  # L-1 lands first via another file
    committed = commit(staged, today=TODAY)
    assert (committed.rows_new, committed.rows_duplicate) == (1, 1)
    with pytest.raises(ImportFailed):
        commit(committed, today=TODAY)

    other = stage(ws, fmt=ACTIVITY, content=content, today=TODAY)
    assert discard(other).status == B.DISCARDED
    with pytest.raises(ImportFailed):
        commit(other, today=TODAY)


@pytest.mark.django_db
def test_accounts_resolve_within_the_workspace_only(alder: Family) -> None:
    other_ws = Workspace.objects.create(customer_id=777, name="Other")
    batch = run(other_ws, activity(row(code="DEP", net="10.00", id="I-1")))
    assert codes(batch, "error") == ["unknown_account"]


# ---------------------------------------------------------------------------------------------- prices


@pytest.mark.django_db
def test_price_file(alder: Family) -> None:
    ws = alder.workspace
    batch = run(ws, alder_price_file(TODAY), PRICES)
    assert batch.status == B.COMMITTED
    assert (batch.rows_new, batch.rows_ignored) == (4, 2)  # STTR and ZQXV aren't set up here
    ustm = Instrument.objects.get(ticker="USTM")
    price = Price.objects.get(instrument=ustm, as_of=TODAY - timedelta(days=1))
    assert (price.price, price.batch, price.source) == (Decimal("274.85"), batch, "custodian_file")
    h = next(h for h in holdings(acct(alder, RRT), TODAY) if h.instrument == ustm)
    assert h.market_value == Decimal("1374250.00")  # 5000 x 274.85

    again = run(ws, alder_price_file(TODAY), PRICES)
    assert (again.rows_new, again.rows_duplicate) == (0, 4)

    when = d(-1)
    conflict = run(ws, f"CUSIP,Symbol,Price Date,Price\n,USTM,{when},275.00\n", PRICES)
    assert codes(conflict, "error") == ["conflicting_price"]
    jump = run(ws, f"CUSIP,Symbol,Price Date,Price\n,INTD,{d(0)},99.00\n", PRICES)
    assert jump.status == B.COMMITTED and codes(jump, "warning") == ["price_move"]
    bad = run(ws, f"CUSIP,Symbol,Price Date,Price\n,INTD,{d(-3)},0\n", PRICES)
    assert codes(bad, "error") == ["bad_price"]


# ------------------------------------------------------------------------------------ configurable layouts


@pytest.mark.django_db
def test_workspace_layout_overrides_a_preset(alder: Family) -> None:
    ws = alder.workspace
    CustodianFormat.objects.create(
        workspace=ws, code="bayview", name="Bayview activity", preset="pershing_style_activity",
        config={"columns": {"net_amount": "Amount", "account": "Acct"}, "codes": {"CDV": "dividend"}, "date_formats": ["%Y-%m-%d"]},
    )
    fmt = resolve_format(ws, "bayview")
    assert fmt.namespace == "pershing" and fmt.columns["trade_date"] == "Trade Date"
    content = f"Acct,Trade Date,Transaction Code,Symbol,Amount,Transaction ID\n{RRT},{(TODAY - timedelta(days=2)).isoformat()},cdv,USTM,12.50,C-1\n"
    batch = run(ws, content, fmt)
    assert batch.status == B.COMMITTED and batch.file_format == "bayview"
    assert Transaction.objects.get(source_ref="pershing:C-1").txn_type == "dividend"


@pytest.mark.django_db
def test_invalid_layouts_are_refused(workspace: Workspace) -> None:
    for config in ({"columns": {"nonsense": "X"}}, {"codes": {"Q": "not_a_type"}}, {"signed_amounts": "yes"}, {"bogus": 1},
                   {"columns": {"net_amount": "", "principal": ""}}):
        with pytest.raises(ValidationError):
            CustodianFormat(workspace=workspace, code="x", name="X", preset="pershing_style_activity", config=config).clean()
    with pytest.raises(ValidationError):
        CustodianFormat(workspace=workspace, code="x", name="X", preset="nope").clean()
    with pytest.raises(FormatError):
        resolve_format(workspace, "unknown_layout")


def test_parse_decimal_spellings() -> None:
    assert parse_decimal("$1,234.56") == Decimal("1234.56")
    assert parse_decimal("(1,234.56)") == Decimal("-1234.56")
    assert parse_decimal("1,234.56-") == Decimal("-1234.56")
    assert parse_decimal("  ") is None
    with pytest.raises(ParseError):
        parse_decimal("NaN")


# ---------------------------------------------------------------------------------------------- command


@pytest.mark.django_db
def test_import_command(alder: Family, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "activity.csv"
    call_command("import_custodian_file", str(path), write_sample="activity")
    cid = alder.workspace.customer_id
    with pytest.raises(CommandError, match="rejected"):
        call_command("import_custodian_file", str(path), customer_id=cid, stdout=StringIO())
    out = StringIO()
    call_command("import_custodian_file", str(path), customer_id=cid, commit=True, create_securities=True, stdout=out)
    assert "imported: 9 rows, 8 new" in out.getvalue()


# ---------------------------------------------------------------------------------------------- page


def _frame_token(html: str) -> str:
    import re

    m = re.search(r'name="fo_t" value="([^"]+)"', html)
    assert m, "frame token missing from the page"
    return m.group(1)


@pytest.mark.django_db
def test_imports_page_in_campfire_frame(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils import timezone

    from tests.conftest import context_token

    ws = alder.workspace
    r = client.get(f"/fo/frame/imports?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=7)}")
    assert r.status_code == 200 and "frame-ancestors" in r["Content-Security-Policy"]
    token = _frame_token(r.content.decode())
    assert "Sample activity file" in r.content.decode()

    # Upload without Django's CSRF cookie (third-party iframe): the frame token authorizes it.
    csrf_client = type(client)(enforce_csrf_checks=True)
    upload = SimpleUploadedFile("activity.csv", alder_activity_file(timezone.localdate()).encode(), content_type="text/csv")
    r = csrf_client.post("/fo/frame/imports", {"fo_t": token, "format": "pershing_style_activity", "file": upload, "create_securities": "on"})
    assert r.status_code == 302 and "fo_t=" in r["Location"]
    batch = ImportBatch.objects.get(workspace=ws, source="custodian_file")
    assert (batch.status, batch.created_by) == (B.VALIDATED, "Campfire user 7")

    r = csrf_client.get(r["Location"])
    page = r.content.decode()
    assert r.status_code == 200 and "Import 8 rows" in page and "Short-Term Treasury ETF (new)" in page

    r = csrf_client.post(f"/fo/frame/imports/{batch.pk}", {"fo_t": token, "action": "commit"})
    assert r.status_code == 200 and "Imported." in r.content.decode()
    batch.refresh_from_db()
    assert batch.status == B.COMMITTED and Transaction.objects.filter(batch=batch).count() == 8

    assert csrf_client.post("/fo/frame/imports", {"fo_t": token + "x", "format": "x"}).status_code == 401
    assert csrf_client.post("/fo/frame/imports", {"format": "x"}).status_code == 401
    assert csrf_client.get(f"/fo/frame/imports/sample/activity?fo_t={token}")["Content-Type"] == "text/csv"


@pytest.mark.django_db
def test_frame_token_expires_and_is_bound_to_its_workspace(client, alder: Family, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from campfire_integration import pages

    other = Workspace.objects.create(customer_id=99, name="Other", access_token="a", refresh_token="r",
                                     token_expires_at=alder.workspace.token_expires_at, connected_at=alder.workspace.connected_at)
    batch = stage(alder.workspace, fmt=ACTIVITY, content=alder_activity_file(TODAY), today=TODAY)
    other_token = pages.issue_frame_token(other, {})
    assert client.get(f"/fo/frame/imports/{batch.pk}?fo_t={other_token}").status_code == 404

    token = pages.issue_frame_token(alder.workspace, {})
    assert client.get(f"/fo/frame/imports?fo_t={token}").status_code == 200
    monkeypatch.setattr(pages, "FRAME_TOKEN_MAX_AGE", -1)
    assert client.get(f"/fo/frame/imports?fo_t={token}").status_code == 401


@pytest.mark.django_db
def test_imports_page_standalone(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/fo/imports").status_code == 302  # not signed in
    session = client.session
    session["workspace_id"] = alder.workspace.pk
    session["campfire_user"] = {"name": "Ops", "email": "ops@example.test"}
    session.save()
    assert client.post("/fo/imports", {"format": "pershing_style_activity"}).status_code == 400  # no file
    from django.core.files.uploadedfile import SimpleUploadedFile

    bad = SimpleUploadedFile("bad.csv", b"Account Number,Trade Date\n1,2\n")
    r = client.post("/fo/imports", {"format": "pershing_style_activity", "file": bad})
    assert r.status_code == 302 and "fo_t" not in r["Location"]
    page = client.get(r["Location"]).content.decode()
    assert "Rejected" in page and "missing_column" in page
    assert ImportBatch.objects.get(file_name="bad.csv").created_by == "ops@example.test"
