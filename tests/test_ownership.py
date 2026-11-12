from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family, OwnershipInterest, Person
from entities.services.ownership import (
    OwnershipCycleError,
    branch_ownership,
    effective_ownership,
    ownership_issues,
)

TODAY = date(2026, 6, 30)


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    return seed_alder(workspace)


def _by_name(family: Family, as_of: date = TODAY) -> dict[str, dict[str, Decimal]]:
    names = {p.id: p.first_name for p in Person.objects.filter(family=family)}
    entities = {e.id: e.name for e in Entity.objects.filter(family=family)}
    return {
        entities[eid]: {(names[pid] if pid else "-"): (share * 100).quantize(Decimal("0.0001")) for pid, share in shares.items()}
        for eid, shares in effective_ownership(family, as_of).items()
    }


@pytest.mark.django_db
def test_look_through_multi_level(alder: Family) -> None:
    result = _by_name(alder)
    # LP: 1% GP via Management LLC (50/50 the two revocable trusts) + 24.5% each RT + 50% Dynasty (50/50 Claire/James)
    expected = {"Robert": Decimal("25.0000"), "Eleanor": Decimal("25.0000"), "Claire": Decimal("25.0000"), "James": Decimal("25.0000")}
    assert result["Alder Family Investments LP"] == expected
    assert result["Alder Real Estate Holdings LLC"] == expected  # 100% owned by the LP
    assert result["Alder Management LLC"] == {"Robert": Decimal("50.0000"), "Eleanor": Decimal("50.0000")}
    assert result["Alder Family Foundation"] == {"-": Decimal("100.0000")}  # no owners: unallocated
    assert ownership_issues(alder, TODAY) == []


@pytest.mark.django_db
def test_branch_rollup(alder: Family) -> None:
    lp = Entity.objects.get(family=alder, short_name="AFI LP")
    branches = {b.id: b.name for b in alder.branches.all()}
    rolled = {branches.get(k, "-"): (v * 100).quantize(Decimal("0.01")) for k, v in branch_ownership(alder, TODAY)[lp.id].items()}
    assert rolled == {"Founders (G1)": Decimal("50.00"), "Branch A: Claire": Decimal("25.00"), "Branch B: James": Decimal("25.00")}


@pytest.mark.django_db
def test_effective_dating_and_under_allocation(alder: Family) -> None:
    lp = Entity.objects.get(family=alder, short_name="AFI LP")
    dynasty = Entity.objects.get(family=alder, name="Alder 2012 Dynasty Trust")
    stake = OwnershipInterest.objects.get(owned=lp, owner_entity=dynasty)
    stake.effective_to = date(2026, 3, 31)  # interest ends; nothing replaces it yet
    stake.save()
    before = _by_name(alder, date(2026, 3, 31))["Alder Family Investments LP"]
    after = _by_name(alder, date(2026, 4, 1))["Alder Family Investments LP"]
    assert before["Claire"] == Decimal("25.0000")
    assert "Claire" not in after and after["-"] == Decimal("50.0000")
    issues = ownership_issues(alder, date(2026, 4, 1))
    assert [(i.entity.short_name, i.kind) for i in issues] == [("AFI LP", "under-allocated")]


@pytest.mark.django_db
def test_cycle_detected(alder: Family) -> None:
    mgmt = Entity.objects.get(family=alder, short_name="AM LLC")
    re_llc = Entity.objects.get(family=alder, short_name="ARE LLC")
    OwnershipInterest.objects.create(owned=mgmt, owner_entity=re_llc, percentage=Decimal("1"), interest_type="member", effective_from=date(2020, 1, 1))
    with pytest.raises(OwnershipCycleError):
        effective_ownership(alder, TODAY)


@pytest.mark.django_db
def test_constraints_and_validation(alder: Family, workspace: Workspace) -> None:
    lp = Entity.objects.get(family=alder, short_name="AFI LP")
    robert = Person.objects.get(family=alder, first_name="Robert")
    with transaction.atomic(), pytest.raises(IntegrityError):  # both owner kinds set
        OwnershipInterest.objects.create(owned=lp, owner_entity=lp, owner_person=robert, percentage=1, interest_type="lp", effective_from=TODAY)
    with pytest.raises(ValidationError):
        OwnershipInterest(owned=lp, owner_entity=lp, percentage=1, interest_type="lp", effective_from=TODAY).full_clean()

    other_family = Family.objects.create(workspace=workspace, name="Other", code="other")
    outsider = Person.objects.create(family=other_family, first_name="Out", last_name="Sider")
    with pytest.raises(ValidationError, match="same family"):
        OwnershipInterest(owned=lp, owner_person=outsider, percentage=1, interest_type="lp", effective_from=TODAY).full_clean()

    lp.campfire_entity_id = 9001
    lp.save()
    other_entity = Entity(family=other_family, name="X", entity_type="llc", tax_classification="partnership", campfire_entity_id=9001)
    with pytest.raises(ValidationError, match="already linked"):
        other_entity.full_clean()
