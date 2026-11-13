import pytest
from django.core.management.base import CommandError

from campfire_integration.management.commands.push_manifest import desired_manifest, summarize

CURRENT = {
    "app": {"name": "Family Office", "tagline": "Our own tagline", "description": "d", "category": "", "icon_url": None,
            "redirect_uris": [], "connect_url": None, "manage_url": None, "allowed_scopes": ["custom_fields:write", "journal_entries:write"]},
    "allowed_domains": [], "slots": [{"key": "keep-me"}], "panels": [], "navigation": [], "event_subscriptions": [], "hooks": [],
}


def test_scopes_only_changes_scopes_and_keeps_everything_else(settings) -> None:  # type: ignore[no-untyped-def]
    settings.CAMPFIRE = {**settings.CAMPFIRE, "SCOPES": ["openid", "journal_entries:write", "entities:read"]}
    desired = desired_manifest(CURRENT, scopes_only=True)
    assert desired["app"] == {**CURRENT["app"], "allowed_scopes": ["openid", "journal_entries:write", "entities:read"]}
    assert desired["slots"] == [{"key": "keep-me"}]  # a section left out would be deleted by Campfire
    assert "allowed_domains" not in desired  # left out = unchanged
    assert summarize(CURRENT, desired) == ["add scopes: entities:read openid", "remove scopes: custom_fields:write"]


def test_full_manifest_keeps_branding_and_needs_a_public_url(settings) -> None:  # type: ignore[no-untyped-def]
    desired = desired_manifest(CURRENT, scopes_only=False)  # APP_URL is https://fo.test in tests
    assert desired["app"]["tagline"] == "Our own tagline"
    assert desired["app"]["redirect_uris"] == ["https://fo.test/campfire/callback"]
    assert desired["allowed_domains"] == ["fo.test"] and desired["navigation"]
    settings.APP_URL = "https://your-subdomain.ngrok-free.app"
    with pytest.raises(CommandError, match="--scopes-only"):
        desired_manifest(CURRENT, scopes_only=False)
