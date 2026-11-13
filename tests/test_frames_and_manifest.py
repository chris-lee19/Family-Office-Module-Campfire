import json
from io import StringIO

import pytest
from django.core.management import call_command

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from tests.conftest import APP_URL, FRONTEND, context_token


@pytest.mark.django_db
def test_nav_frame_renders_inside_campfire_once(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    seed_alder(workspace)
    token = context_token(customer_id=workspace.customer_id, surface="nav")
    r = client.get(f"/fo/frame/entities?cf_ctx={token}")
    assert r.status_code == 200
    body = r.content.decode()
    assert "Alder Family Investments LP" in body and "Claire Moreno" in body
    assert r["Content-Security-Policy"] == f"frame-ancestors {FRONTEND}"
    assert "X-Frame-Options" not in r
    assert client.get(f"/fo/frame/entities?cf_ctx={token}").status_code == 401  # single use


@pytest.mark.django_db
def test_frame_refuses_missing_wrong_surface_record_token_and_unknown_workspace(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/fo/frame/entities").status_code == 401
    assert client.get(f"/fo/frame/entities?cf_ctx={context_token(customer_id=workspace.customer_id, surface='manage')}").status_code == 403
    record = context_token(customer_id=workspace.customer_id, surface="nav", object_type="invoice", object_ids=[1])
    assert client.get(f"/fo/frame/entities?cf_ctx={record}").status_code == 403
    assert client.get(f"/fo/frame/entities?cf_ctx={context_token(customer_id=999, surface='nav')}").status_code == 403


@pytest.mark.django_db
def test_other_workspaces_data_is_not_shown(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    other = Workspace.objects.create(customer_id=5, name="Other", access_token="a", refresh_token="r")
    seed_alder(other)
    r = client.get(f"/fo/frame/entities?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav')}")
    assert r.status_code == 200 and "Alder 2012 Dynasty Trust" not in r.content.decode()


@pytest.mark.django_db
def test_manage_link_signs_in(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"/campfire/manage?cf_ctx={context_token(customer_id=workspace.customer_id, surface='manage', user_id=3)}")
    assert r.status_code == 302
    assert client.session["workspace_id"] == workspace.pk
    assert client.get("/").status_code == 200


def test_manifest(settings) -> None:  # type: ignore[no-untyped-def]
    out = StringIO()
    call_command("build_manifest", "--output", "-", stdout=out)
    manifest = json.loads(out.getvalue())
    assert manifest["app"]["redirect_uris"] == [f"{APP_URL}/campfire/callback"]
    assert manifest["app"]["allowed_scopes"] == ["openid", "email", "profile"]
    assert manifest["allowed_domains"] == ["fo.test"]
    items = manifest["navigation"][0]["items"]
    assert [i["url_template"] for i in items] == [f"{APP_URL}/fo/frame/investments", f"{APP_URL}/fo/frame/entities", f"{APP_URL}/fo/frame/imports", f"{APP_URL}/fo/frame/gl", f"{APP_URL}/fo/frame/review", f"{APP_URL}/fo/frame/records"]
    assert manifest["event_subscriptions"] == []  # identity scopes only: no topic is covered

    from config.settings import DEFAULT_CAMPFIRE_SCOPES

    settings.CAMPFIRE = {**settings.CAMPFIRE, "SCOPES": DEFAULT_CAMPFIRE_SCOPES}
    out = StringIO()
    call_command("build_manifest", "--output", "-", stdout=out)
    manifest = json.loads(out.getvalue())
    assert manifest["app"]["allowed_scopes"] == DEFAULT_CAMPFIRE_SCOPES
    (subscription,) = manifest["event_subscriptions"]
    assert subscription["endpoint_url"] == f"{APP_URL}/campfire/events"
    assert set(subscription["topics"]) == {"JournalEntry.created", "JournalEntry.updated", "JournalEntry.deleted"}
