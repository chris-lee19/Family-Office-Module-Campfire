import pytest

from campfire_integration.models import Workspace
from entities.models import Family
from investments.models import Account
from tests.conftest import context_token


@pytest.mark.django_db
def test_testers_can_load_the_demo_family_from_campfire(client, workspace: Workspace, settings) -> None:  # type: ignore[no-untyped-def]
    settings.ALLOW_DEMO_DATA = True
    r = client.get(f"/fo/frame/investments?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav')}")
    page = r.content.decode()
    assert "Load the demo family" in page
    token = page.split('name="fo_t" value="')[1].split('"')[0]

    r = client.post("/fo/frame/demo", {"fo_t": token})
    assert r.status_code == 302 and r["Location"].startswith("/fo/frame/investments?fo_t=")
    family = Family.objects.get(workspace=workspace, code="alder")
    accounts = Account.objects.filter(family=family).count()
    assert accounts and "Load the demo family" not in client.get(r["Location"]).content.decode()

    client.post("/fo/frame/demo", {"fo_t": token})  # a second click changes nothing
    assert Family.objects.filter(workspace=workspace).count() == 1 and Account.objects.filter(family=family).count() == accounts
    assert client.get(f"/fo/frame/demo?fo_t={token}").status_code == 405


@pytest.mark.django_db
def test_demo_data_is_off_unless_allowed(client, workspace: Workspace, settings) -> None:  # type: ignore[no-untyped-def]
    settings.ALLOW_DEMO_DATA = False
    session = client.session
    session["workspace_id"] = workspace.pk
    session.save()
    assert "Load the demo family" not in client.get("/fo/investments").content.decode()
    assert client.post("/fo/demo").status_code == 404
    assert not Family.objects.exists()


def test_encryption_secret_becomes_a_fernet_key() -> None:
    from cryptography.fernet import Fernet

    from config.settings import _derived_key

    Fernet(_derived_key("any long random string a host generated").encode())
