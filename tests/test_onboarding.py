import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family, OwnershipInterest
from gl.models import GLSettings
from investments.demo import seed_alder_investments
from investments.models import ImportBatch, Transaction
from investments.services.portfolio import build_portfolio
from records.onboarding import SHEETS, build_workbook, commit, family_rows, stage

TODAY = date(2026, 6, 30)
B = ImportBatch.Status


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    return family


def blank(**sheets: list[list[object]]) -> bytes:
    return build_workbook({name: rows for name, rows in sheets.items()})


def onboard(ws: Workspace, content: bytes, **kw: object) -> ImportBatch:
    kw = {"family_name": "Alder Family", "family_code": "alder", **kw}
    return stage(ws, content=content, file_name="family.xlsx", as_of=TODAY, **kw)  # type: ignore[arg-type]


def messages(batch: ImportBatch) -> str:
    return " | ".join(i.message for i in batch.issues.all())


@pytest.mark.django_db
def test_export_then_onboard_reproduces_the_portfolio(alder: Family) -> None:
    original = build_portfolio(alder, TODAY)
    workbook = build_workbook(family_rows(alder, TODAY))
    fresh = Workspace.objects.create(customer_id=2024, name="Fresh")

    staged = onboard(fresh, workbook, set_gl_start=True)
    assert staged.status == B.VALIDATED, messages(staged)
    assert not Family.objects.filter(workspace=fresh).exists()  # validating keeps nothing
    assert staged.options["created"]["Entities"] == 8

    done = commit(staged)
    assert done.status == B.COMMITTED, messages(done)
    family = Family.objects.get(workspace=fresh)
    copy = build_portfolio(family, TODAY)
    assert copy.total == original.total == 23874900
    assert copy.unfunded == original.unfunded
    assert dict(copy.by_person) == dict(original.by_person)
    assert dict(copy.by_liquidity) == dict(original.by_liquidity)
    assert GLSettings.objects.get(workspace=fresh).post_from == TODAY + timedelta(days=1)
    assert Transaction.objects.filter(batch=done).exists()

    again = onboard(fresh, workbook)  # the same workbook again adds nothing
    assert again.status == B.VALIDATED and again.rows_new == 0 and again.rows_duplicate > 0


@pytest.mark.django_db
def test_template_has_every_sheet_with_headers_and_dropdowns() -> None:
    wb = load_workbook(io.BytesIO(build_workbook()))
    assert wb.sheetnames == ["Instructions", *[s.name for s in SHEETS]]
    entities = wb["Entities"]
    assert [c.value for c in entities[1]][:4] == ["Name", "Short name", "Type", "Tax classification"]
    assert entities.data_validations.dataValidation  # choice columns have dropdowns


@pytest.mark.django_db
def test_problems_are_reported_by_sheet_and_row_and_nothing_is_kept(workspace: Workspace) -> None:
    content = blank(
        Entities=[["Moss Trust", None, "Irrevocable trust", "Non-grantor trust"], ["Moss LLC", None, "Spaceship", "Partnership"]],
        Ownership=[["Moss Trust", "Nobody Known", "Member (LLC)", 100, "2020-01-01"]],
        Accounts=[["Moss Custody", "Moss Trust", "Brokerage / custody", "Taxable", None, None, None, "Yes", None, -5]],
    )
    batch = onboard(workspace, content, family_name="Moss Family", family_code="moss")
    assert batch.status == B.REJECTED
    text = messages(batch)
    assert "Entities: Type: 'Spaceship' isn't one of" in text
    assert "Ownership: Owner 'Nobody Known' is neither" in text
    assert "Accounts: Opening cash can't be negative" in text
    rows = {(i.code, i.row_number) for i in batch.issues.all()}
    assert ("Entities", 3) in rows and ("Ownership", 2) in rows
    assert not Family.objects.filter(code="moss").exists() and not Entity.objects.filter(name="Moss Trust").exists()


@pytest.mark.django_db
def test_into_an_existing_family_and_ownership_loop(alder: Family) -> None:
    lp = Entity.objects.get(name="Alder Family Investments LP")
    owned = OwnershipInterest.objects.filter(owned__family=alder, owner_entity__isnull=False).first()
    content = blank(
        Entities=[["Alder 2027 GST Trust", None, "irrevocable_trust", "non_grantor_trust", "US-SD", "2027-01-02"]],
        Ownership=[[owned.owner_entity.name, owned.owned.name, "member", 1, "2000-01-01"]],  # the reverse: a loop
    )
    batch = stage(alder.workspace, content=content, file_name="x.xlsx", as_of=TODAY, family_id=alder.pk)
    assert batch.status == B.REJECTED and "Ownership loops" in messages(batch)
    assert not Entity.objects.filter(name="Alder 2027 GST Trust").exists()

    ok = stage(alder.workspace, content=blank(Entities=[["Alder 2027 GST Trust", None, "irrevocable_trust", "non_grantor_trust"]]),
               file_name="x.xlsx", as_of=TODAY, family_id=alder.pk)
    assert commit(ok).status == B.COMMITTED
    assert Entity.objects.get(name="Alder 2027 GST Trust").family == alder and lp.family == alder


@pytest.mark.django_db
def test_not_a_workbook(workspace: Workspace) -> None:
    batch = onboard(workspace, b"name,amount\nx,1\n")
    assert batch.status == B.REJECTED and "isn't an Excel workbook" in messages(batch)


@pytest.mark.django_db
def test_onboarding_screens_in_campfire(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from django.core.files.uploadedfile import SimpleUploadedFile

    from tests.conftest import context_token

    ws = alder.workspace
    r = client.get(f"/fo/frame/records?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=4)}")
    token = unquote(r.content.decode().split("fo_t=")[1].split('"')[0].split("&")[0])
    page = client.get("/fo/frame/records/onboarding", {"fo_t": token}).content.decode()
    assert "Blank template" in page and "Alder Family as it is today" in page

    template = client.get("/fo/frame/records/onboarding/template.xlsx", {"fo_t": token})
    assert template["Content-Type"].startswith("application/vnd.openxmlformats") and template.content[:2] == b"PK"
    export = client.get(f"/fo/frame/records/onboarding/export/{alder.pk}.xlsx", {"fo_t": token})
    assert export.status_code == 200

    # The family's own workbook, loaded as a second family in the same workspace.
    upload = SimpleUploadedFile("copy.xlsx", export.content)
    assert client.post("/fo/frame/records/onboarding", {"fo_t": token, "family": "new", "family_name": "Alder Copy",
                                                        "family_code": "alder-copy", "file": upload}).status_code == 400  # no date
    upload.seek(0)
    r = client.post("/fo/frame/records/onboarding", {"fo_t": token, "family": "new", "family_name": "Alder Copy", "family_code": "alder-copy",
                                                     "as_of": TODAY.isoformat(), "file": upload})
    assert r.status_code == 302 and "/fo/frame/records/onboarding/" in r["Location"]
    batch = ImportBatch.objects.get(source="onboarding")
    assert batch.status == B.VALIDATED, messages(batch)
    assert "Import" in client.get(r["Location"]).content.decode()

    r = client.post(f"/fo/frame/records/onboarding/{batch.pk}", {"fo_t": token, "action": "commit"})
    assert "Imported." in r.content.decode()
    copy = Family.objects.get(workspace=ws, code="alder-copy")
    assert build_portfolio(copy, TODAY).total == build_portfolio(alder, TODAY).total

    other = Workspace.objects.create(customer_id=31337, name="Other")
    theirs = stage(other, content=blank(), file_name="x.xlsx", as_of=TODAY, family_name="X", family_code="x")
    assert client.get(f"/fo/frame/records/onboarding/{theirs.pk}", {"fo_t": token}).status_code == 404
    assert client.get(f"/fo/frame/records/onboarding/export/{Family.objects.create(workspace=other, name='Y', code='y').pk}.xlsx", {"fo_t": token}).status_code == 404
