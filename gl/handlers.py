"""Campfire events about journal entries: confirm postings when they land in the GL, free them when deleted.

Events are thin (topic, id, changed fields), so a created/updated entry is re-read with the workspace's token.
"""

import logging

from campfire_integration import events
from campfire_integration.client import CampfireClient
from campfire_integration.models import CampfireEvent
from gl.models import SOURCE, JournalPosting
from gl.services.posting import JE_PATH, mark_posted, posting_for_source_id, release

log = logging.getLogger(__name__)


@events.on("JournalEntry.created", "JournalEntry.updated")
def journal_entry_saved(event: CampfireEvent) -> None:
    if event.workspace is None or not event.object_id or event.payload.get("test"):
        return
    je = CampfireClient(event.workspace).get_json(f"{JE_PATH}/{event.object_id}")
    if je.get("source") != SOURCE:
        return
    posting = posting_for_source_id(event.workspace, je.get("source_id"))
    if posting is None:
        log.warning("journal entry %s claims source_id %s, which isn't ours", event.object_id, je.get("source_id"))
        return
    if je.get("is_deleted"):
        release(posting, f"Journal entry {event.object_id} was deleted in Campfire.")
    elif posting.status != JournalPosting.Status.POSTED or posting.campfire_journal_id != int(je["id"]):
        mark_posted(posting, int(je["id"]))


@events.on("JournalEntry.deleted")
def journal_entry_deleted(event: CampfireEvent) -> None:
    if event.workspace is None or not str(event.object_id).isdigit():
        return
    for posting in JournalPosting.objects.filter(workspace=event.workspace, campfire_journal_id=int(event.object_id)).exclude(status=JournalPosting.Status.RELEASED):
        release(posting, f"Journal entry {event.object_id} was deleted in Campfire.")
