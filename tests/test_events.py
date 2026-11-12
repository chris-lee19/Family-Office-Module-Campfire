import pytest

from campfire_integration import events
from campfire_integration.models import CampfireEvent, Workspace
from tests.conftest import as_json, signed_headers


@pytest.mark.django_db
def test_unsigned_event_is_refused(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/campfire/events", data=as_json({"topic": "Bill.updated"}), content_type="application/json")
    assert r.status_code == 401
    assert CampfireEvent.objects.count() == 0


@pytest.mark.django_db
def test_signed_event_is_stored_and_dispatched(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []

    @events.on("Test.happened")
    def handler(event: CampfireEvent) -> None:
        seen.append(event.object_id)

    @events.on("Test.happened")
    def broken(event: CampfireEvent) -> None:
        raise RuntimeError("boom")

    body = as_json({"topic": "Test.happened", "id": 55, "customer": {"id": workspace.customer_id}})
    r = client.post("/campfire/events", data=body, content_type="application/json", **signed_headers(body))
    assert r.status_code == 200
    event = CampfireEvent.objects.get()
    assert event.workspace == workspace and event.object_id == "55" and event.processed_at is not None
    assert seen == ["55"]
    assert "broken: boom" in event.error  # one failing handler doesn't stop the others


@pytest.mark.django_db
def test_stale_signature_is_refused(client) -> None:  # type: ignore[no-untyped-def]
    body = as_json({"topic": "X"})
    r = client.post("/campfire/events", data=body, content_type="application/json", **signed_headers(body, timestamp=1))
    assert r.status_code == 401
