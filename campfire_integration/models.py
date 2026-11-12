from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from core.fields import EncryptedTextField


class Workspace(models.Model):
    """One Campfire workspace (Campfire calls it a customer) that has connected the app: the tenant."""

    customer_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    access_token = EncryptedTextField(blank=True, default="")
    refresh_token = EncryptedTextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True, default="")
    # Mirror of the install config stored in Campfire (string values only).
    install_config = models.JSONField(default=dict, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    onboarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name or f"Workspace {self.customer_id}"

    @property
    def is_connected(self) -> bool:
        return bool(self.access_token or self.refresh_token) and self.disconnected_at is None

    def apply_tokens(self, tokens: dict[str, Any]) -> None:
        """Store a token response. Refresh tokens rotate; keep the old one if none was returned."""
        self.access_token = tokens["access_token"]
        self.refresh_token = tokens.get("refresh_token") or self.refresh_token
        self.token_expires_at = timezone.now() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
        if tokens.get("scope"):
            self.scopes = tokens["scope"]
        self.connected_at = self.connected_at or timezone.now()
        self.disconnected_at = None

    def drop_grant(self) -> None:
        self.access_token = ""
        self.refresh_token = ""
        self.token_expires_at = None
        self.disconnected_at = timezone.now()
        self.save(update_fields=["access_token", "refresh_token", "token_expires_at", "disconnected_at", "updated_at"])


class UsedContextToken(models.Model):
    """`jti`s of Campfire context tokens already spent: every `cf_ctx` is single use."""

    jti = models.CharField(max_length=128, primary_key=True)
    expires_at = models.DateTimeField(db_index=True)

    @classmethod
    def claim(cls, jti: str, exp: float) -> bool:
        """Record the jti; False when it was already used."""
        expires_at = datetime.fromtimestamp(exp, tz=dt_timezone.utc)
        try:
            with transaction.atomic():
                cls.objects.create(jti=jti, expires_at=expires_at)
        except IntegrityError:
            return False
        cls.objects.filter(expires_at__lt=timezone.now() - timedelta(hours=1)).delete()
        return True


class CampfireEvent(models.Model):
    """Every signed event Campfire delivered, kept as an audit trail and for replay."""

    workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    customer_id = models.BigIntegerField(db_index=True)
    topic = models.CharField(max_length=128, db_index=True)
    object_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.topic} {self.object_id} ({self.customer_id})"
