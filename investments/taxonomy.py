"""Default asset-class taxonomy, created per workspace on first use. Tenants can rename, add and nest classes."""

from campfire_integration.models import Workspace
from investments.models import AssetClass

L = AssetClass.Liquidity

DEFAULT_ASSET_CLASSES: list[tuple[str, str, str]] = [
    ("cash", "Cash & equivalents", L.LIQUID),
    ("fixed_income", "Fixed income", L.LIQUID),
    ("public_equity", "Public equity", L.LIQUID),
    ("hedge_funds", "Hedge funds", L.SEMI_LIQUID),
    ("private_equity", "Private equity", L.ILLIQUID),
    ("venture_capital", "Venture capital", L.ILLIQUID),
    ("private_credit", "Private credit", L.ILLIQUID),
    ("real_estate", "Real estate", L.ILLIQUID),
    ("real_assets", "Real assets", L.ILLIQUID),
    ("collectibles", "Collectibles & other", L.ILLIQUID),
]


def ensure_default_taxonomy(workspace: Workspace) -> dict[str, AssetClass]:
    for order, (code, name, liquidity) in enumerate(DEFAULT_ASSET_CLASSES):
        AssetClass.objects.get_or_create(
            workspace=workspace, code=code, defaults={"name": name, "liquidity": liquidity, "sort_order": order}
        )
    return {a.code: a for a in AssetClass.objects.filter(workspace=workspace)}
