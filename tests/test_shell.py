import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from investments.demo import seed_alder_investments
from investments.templatetags.viz import segments


@dataclass
class Row:
    label: str
    value: Decimal
    pct: Decimal


def test_allocation_segments_keep_colours_and_fold_the_tail() -> None:
    rows = [Row(f"Class {c}", Decimal(100 - i), Decimal(10)) for i, c in enumerate("JIHGFEDCBA")]  # largest first
    segs = segments(rows)
    assert len(segs) == 8 and segs[-1]["label"] == "Other (3)" and segs[-1]["slot"] == 0
    assert segs[-1]["value"] == Decimal(93 + 92 + 91)  # the three smallest
    # Colours follow the label (alphabetical), not the rank: dropping the largest row leaves the others' colours alone.
    few = segments(rows[:4])
    assert {s["label"]: s["slot"] for s in few} == {"Class G": 1, "Class H": 2, "Class I": 3, "Class J": 4}
    assert segments([Row("Debt", Decimal(-5), Decimal(0))]) == []


@pytest.mark.django_db
def test_shell_in_campfire_and_standalone(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import context_token

    family = seed_alder(workspace)
    seed_alder_investments(family, date(2026, 6, 30))
    page = client.get(f"/fo/frame/home?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=2)}").content.decode()
    # Inside Campfire: no sidebar (Campfire lists the pages), but search and the New menu, with links that stay in the frame.
    assert 'class="side"' not in page and 'id="fo-palette"' in page and "Record a capital call notice" in page
    assert "/fo/frame/records/capital-calls/new?fo_t=" in page
    # Page links inside the frame, so moving between pages never touches Campfire's sidebar.
    strip = page.split('data-frame-nav>')[1].split("</nav>")[0]
    assert strip.count("<a ") == 22 and 'href="/fo/frame/close?fo_t=' in strip and 'class="active" aria-current="page">Home' in strip
    index = json.loads(page.split('<script id="fo-index" type="application/json">')[1].split("</script>")[0])
    titles = {i["t"] for i in index}
    assert {"Investments", "Upload a custodian file", "Alder Family Investments LP", "Summit Growth Partners IV, L.P."} <= titles
    assert all("fo_t" not in i["u"] and i["u"].startswith("/fo/frame/") for i in index)  # the page adds the token itself

    session = client.session
    session["workspace_id"] = workspace.pk
    session["campfire_user"] = {"email": "ops@example.test", "campfire_user_id": 2}
    session.save()
    home = client.get("/").content.decode()
    assert 'class="side"' in home and 'aria-current="page"' in home and "Investments by asset class" in home
    assert "Sign out" in home and 'href="/fo/gl"' in home and "data-frame-nav" not in home
    assert 'class="side"' not in client.get("/campfire/logout").content.decode()
