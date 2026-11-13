"""Audit trail for changes made through the Records screens: who changed what, from what, to what."""

from django.db import models

from campfire_integration.models import Workspace


class AuditEntry(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        DELETED = "deleted", "Deleted"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="audit_entries")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=8, choices=Action.choices)
    kind = models.CharField(max_length=40, help_text="Record type slug")
    object_id = models.CharField(max_length=40)
    object_repr = models.CharField(max_length=255)
    changes = models.JSONField(default=dict, blank=True, help_text="field label -> [before, after]")

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["workspace", "kind", "object_id"])]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M} {self.actor} {self.action} {self.kind} {self.object_repr}"


class ControlSettings(models.Model):
    """A workspace's internal-control choices."""

    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="control_settings")
    # Preparer ≠ reviewer (SEC-07): whoever recorded a mark or uploaded a file can't review it. A one-person office
    # can switch it off; the switch is audited.
    require_second_person = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "control settings"

    def __str__(self) -> str:
        return f"Controls for {self.workspace}"

    @classmethod
    def for_workspace(cls, workspace: Workspace) -> "ControlSettings":
        return cls.objects.get_or_create(workspace=workspace)[0]
