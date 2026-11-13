"""Campfire reference data (entities, GL accounts, tags) and the one-time GL setup for a workspace.

`setup_accounts` maps every posting role to a GL account: an existing account with the suggested number (or a
mapping already made) is reused; otherwise, with `create=True`, the account is created. `setup_dimensions`
creates the FO tag groups and a tag per family, asset class and holding. Both are safe to run again.
"""

from dataclasses import dataclass
from typing import Any

from campfire_integration.client import CampfireClient, CampfireError
from campfire_integration.models import Workspace
from entities.models import Family
from gl.models import AccountMapping, DimensionTag, Role
from investments.models import AssetClass, Instrument

ENTITY_PATH = "/coa/api/entity"
ACCOUNT_PATH = "/coa/api/account"
TAG_PATH = "/coa/api/tag"
TAG_GROUP_PATH = "/coa/api/tag-group"


@dataclass(frozen=True)
class SuggestedAccount:
    role: str
    number: str
    name: str
    account_type: str
    account_subtype: str


# A typical family-office layout; numbers can be changed in Campfire afterwards (the mapping keeps the id).
SUGGESTED_ACCOUNTS = [
    SuggestedAccount(Role.CASH, "1150", "Cash held at custodians", "ASSET", "BANK"),
    SuggestedAccount(Role.CASH_CLEARING, "1160", "Investment cash clearing", "ASSET", "OTHER_CURRENT_ASSET"),
    SuggestedAccount(Role.TRANSFERS, "1170", "Transfers clearing", "ASSET", "OTHER_CURRENT_ASSET"),
    SuggestedAccount(Role.INVESTMENTS, "1500", "Investments at cost", "ASSET", "OTHER_ASSET"),
    SuggestedAccount(Role.DIVIDEND_INCOME, "7100", "Dividend income", "OTHER_INCOME", "OTHER_INCOME"),
    SuggestedAccount(Role.INTEREST_INCOME, "7110", "Interest income", "OTHER_INCOME", "OTHER_INCOME"),
    SuggestedAccount(Role.FUND_INCOME, "7120", "Fund income distributions", "OTHER_INCOME", "OTHER_INCOME"),
    SuggestedAccount(Role.REALIZED_GAIN, "7200", "Realized gain / loss on investments", "OTHER_INCOME", "REALIZED_GAIN_LOSS"),
    SuggestedAccount(Role.INVESTMENT_FEES, "8100", "Investment management fees", "OTHER_EXPENSE", "OTHER_EXPENSE"),
]

TAG_GROUPS = {
    DimensionTag.Dimension.FAMILY: "FO Family",
    DimensionTag.Dimension.ASSET_CLASS: "FO Asset Class",
    DimensionTag.Dimension.HOLDING: "FO Holding",
}


def _results(data: Any) -> list[dict[str, Any]]:
    return data if isinstance(data, list) else data.get("results", [])


def _all(client: CampfireClient, path: str, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset, limit = 0, 500
    while True:
        data = client.get_json(path, params={**params, "limit": limit, "offset": offset})
        page = _results(data)
        items += page
        if isinstance(data, list) or not data.get("next") or not page:
            return items
        offset += len(page)


def list_entities(client: CampfireClient) -> list[dict[str, Any]]:
    return [e for e in _all(client, ENTITY_PATH) if not e.get("is_deleted") and not e.get("is_elimination_entity")]


def list_accounts(client: CampfireClient) -> list[dict[str, Any]]:
    return sorted((a for a in _all(client, ACCOUNT_PATH) if not a.get("is_deleted")), key=lambda a: (a.get("number") or "", a.get("name") or ""))


def account_label(a: dict[str, Any]) -> str:
    return " ".join(x for x in (a.get("number") or "", a.get("name") or "") if x)


def _post(client: CampfireClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    r = client.request("POST", path, json=body)
    if r.status_code not in (200, 201):
        raise CampfireError(r)
    return r.json()


@dataclass
class SetupStep:
    role_or_dimension: str
    action: str  # "mapped", "reuse", "create", "created", "missing"
    label: str


def _norm(name: str | None) -> str:
    return " ".join((name or "").lower().split())


def free_number(wanted: str, taken: set[str]) -> str:
    """The suggested number, or the next free one after it (1500 taken -> 1501 ...). Never reuse a number."""
    if wanted not in taken:
        return wanted
    n = int(wanted)
    while str(n) in taken:
        n += 1
    return str(n)


@dataclass
class PlannedAccount:
    existing: dict[str, Any] | None  # an account with the same name: reused
    number: str  # for a new account
    name: str
    account_type: str
    account_subtype: str

    @property
    def label(self) -> str:
        return account_label(self.existing) if self.existing else f"{self.number} {self.name}"


def plan_accounts(wanted: list[tuple[str, str, str, str]], accounts: list[dict[str, Any]]) -> list[PlannedAccount]:
    """Match (number, name, type, subtype) against the chart: same name -> reuse; else create on a free number.

    A number alone never matches: a sandbox's 1500 may be Intercompany Receivable, not Investments at cost.
    """
    by_name = {_norm(a.get("name")): a for a in accounts}
    taken = {str(a.get("number")) for a in accounts if a.get("number")}
    planned = []
    for number, name, account_type, subtype in wanted:
        existing = by_name.get(_norm(name))
        if existing is None:
            number = free_number(number, taken)
            taken.add(number)
        planned.append(PlannedAccount(existing, number, name, account_type, subtype))
    return planned


def create_planned(client: CampfireClient, p: PlannedAccount) -> dict[str, Any]:
    return _post(client, ACCOUNT_PATH, {"number": p.number, "name": p.name, "account_type": p.account_type, "account_subtype": p.account_subtype})


def setup_accounts(workspace: Workspace, client: CampfireClient, *, create: bool) -> list[SetupStep]:
    existing = {m.role: m for m in AccountMapping.objects.filter(workspace=workspace, campfire_entity_id=None, asset_class=None, account=None)}
    todo = [s for s in SUGGESTED_ACCOUNTS if s.role not in existing]
    plans = dict(zip([s.role for s in todo], plan_accounts([(s.number, s.name, s.account_type, s.account_subtype) for s in todo], list_accounts(client)) if todo else []))
    steps = []
    for s in SUGGESTED_ACCOUNTS:
        if s.role in existing:
            steps.append(SetupStep(Role(s.role).label, "mapped", existing[s.role].campfire_account_label))
            continue
        plan = plans[s.role]
        if not create:  # preview: say what would happen, change nothing
            steps.append(SetupStep(Role(s.role).label, "reuse" if plan.existing else "create", plan.label))
            continue
        account = plan.existing or create_planned(client, plan)
        AccountMapping.objects.create(workspace=workspace, role=s.role, campfire_account_id=int(account["id"]), campfire_account_label=account_label(account))
        steps.append(SetupStep(Role(s.role).label, "mapped" if plan.existing else "created", account_label(account)))
    return steps


def _wanted_tags(workspace: Workspace) -> dict[str, list[tuple[str, str]]]:
    D = DimensionTag.Dimension
    return {
        D.FAMILY: [(f.code, f.name) for f in Family.objects.filter(workspace=workspace, is_active=True)],
        D.ASSET_CLASS: [(a.code, a.name) for a in AssetClass.objects.filter(workspace=workspace)],
        D.HOLDING: [(str(i.pk), (f"{i.ticker} – {i.name}" if i.ticker else i.name)[:100]) for i in Instrument.objects.filter(workspace=workspace)],
    }


def setup_dimensions(workspace: Workspace, client: CampfireClient, *, create: bool) -> list[SetupStep]:
    have = {(t.dimension, t.key) for t in DimensionTag.objects.filter(workspace=workspace)}
    wanted = _wanted_tags(workspace)
    steps = []
    if not create:
        for dimension, values in wanted.items():
            todo = [v for v in values if (dimension, v[0]) not in have]
            steps.append(SetupStep(TAG_GROUPS[dimension], "create" if todo else "mapped", f"{len(todo)} of {len(values)} values to create"))
        return steps
    groups = {g.get("name"): g for g in _all(client, TAG_GROUP_PATH)}
    tags = {(t.get("group"), t.get("name")): t for t in _all(client, TAG_PATH) if not t.get("is_deleted")}
    for dimension, values in wanted.items():
        group = groups.get(TAG_GROUPS[dimension]) or _post(client, TAG_GROUP_PATH, {"name": TAG_GROUPS[dimension]})
        created = 0
        for key, name in values:
            if (dimension, key) in have:
                continue
            tag = tags.get((group["id"], name)) or _post(client, TAG_PATH, {"name": name, "group": group["id"]})
            created += 1
            DimensionTag.objects.create(workspace=workspace, dimension=dimension, key=key, campfire_tag_id=int(tag["id"]), label=name)
        steps.append(SetupStep(TAG_GROUPS[dimension], "created" if created else "mapped", f"{created} values added"))
    return steps
