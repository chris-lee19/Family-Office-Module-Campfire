"""Bank feed (Cash Management) → capital calls and distributions.

Campfire stores each bank-feed line with `posted_at`, a signed `amount` (in > 0, out < 0), `merchant_name` and
`bank_description`, and posts it as bank cash vs Uncategorized until someone categorizes it. Here the app reads recent
lines and suggests what they are:

- money out that matches an open capital call (same amount, the fund or its GP named, within the call's dates):
  - no Campfire bill: "mark the call paid" books the contribution on the wire date (categorize the bank line to
    Investment cash clearing in Campfire; the GL bridge posts the other side against clearing);
  - with a bill: pay the bill with this bank line in Campfire (the bill's payment books the contribution here);
- money in that names a fund or its GP: "record a distribution" with the amount and date filled in.

Each suggestion can be dismissed. A bank line already used (its id is the source_ref of a booked transaction) isn't
suggested again.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from gl.models import BankLineDismissal
from investments.models import CapitalCall, Commitment, Transaction
from investments.services.privates import mark_call_paid

BANK_TXN_PATH = "/ca/api/transaction"
LOOKBACK_DAYS = 120
STOP = {"lp", "l", "p", "llc", "fund", "the", "inc", "co", "partners", "capital", "ventures", "of", "and", "ii", "iii", "iv", "v", "vi"}


def ref(line: dict[str, Any]) -> str:
    return f"campfire-bank:{line['id']}"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _names(c: Commitment) -> list[str]:
    """How a fund shows up on a wire: its GP vendor, its manager, or the fund's name without the legal suffix."""
    fund = re.sub(r",?\s*(l\.?p\.?|llc|ltd)\.?$", "", c.instrument.name, flags=re.I)
    return [n for n in {_norm(c.campfire_vendor_name), _norm(c.instrument.sponsor), _norm(fund)} if n]


def mentions(c: Commitment, text: str) -> bool:
    text = f" {_norm(text)} "
    for name in _names(c):
        if f" {name} " in text:
            return True
        words = [w for w in name.split() if w not in STOP and len(w) > 2]
        if words and all(f" {w} " in text for w in words):  # e.g. "SUMMIT GROWTH" for "Summit Growth Partners IV"
            return True
    return False


@dataclass
class Suggestion:
    line: dict[str, Any]
    kind: str  # "call_paid" | "pay_bill_in_campfire" | "distribution"
    call: CapitalCall | None = None
    commitment: Commitment | None = None

    @property
    def amount(self) -> Decimal:
        return Decimal(str(self.line.get("amount") or 0))

    @property
    def posted(self) -> date:
        return date.fromisoformat(str(self.line.get("posted_at"))[:10])

    @property
    def text(self) -> str:
        return " · ".join(x for x in (self.line.get("merchant_name"), self.line.get("bank_description"), self.line.get("external_memo")) if x)


def bank_lines(client: CampfireClient, since: date) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.get_json(BANK_TXN_PATH, params={"limit": 200, "offset": offset, "exclude_drafts": "true"})
        rows = page.get("results", []) if isinstance(page, dict) else page
        lines += [r for r in rows if not r.get("is_deleted") and not r.get("excluded") and str(r.get("posted_at") or "")[:10] >= since.isoformat()]
        if not isinstance(page, dict) or not page.get("next") or not rows or offset > 5000:
            return lines
        offset += len(rows)


def suggestions(workspace: Workspace, client: CampfireClient, today: date | None = None) -> list[Suggestion]:
    today = today or timezone.localdate()
    used = set(Transaction.objects.filter(account__family__workspace=workspace, source_ref__startswith="campfire-bank:").values_list("source_ref", flat=True))
    dismissed = set(BankLineDismissal.objects.filter(workspace=workspace).values_list("bank_line_id", flat=True))
    commitments = list(Commitment.objects.filter(account__family__workspace=workspace).select_related("instrument", "account__entity"))
    calls = list(CapitalCall.objects.filter(commitment__in=commitments, status__in=CapitalCall.OPEN_STATUSES).select_related("commitment__instrument"))
    out: list[Suggestion] = []
    for line in bank_lines(client, today - timedelta(days=LOOKBACK_DAYS)):
        if ref(line) in used or int(line["id"]) in dismissed:
            continue
        s = Suggestion(line, "")
        text = s.text
        if s.amount < 0:
            match = next((c for c in calls if abs(c.amount + s.amount) <= Decimal("0.01") and mentions(c.commitment, text)
                          and c.notice_date - timedelta(days=10) <= s.posted <= c.due_date + timedelta(days=45)), None)
            if match is not None:
                s.call, s.commitment = match, match.commitment
                s.kind = "pay_bill_in_campfire" if match.campfire_bill_id or match.status == CapitalCall.Status.SUBMITTED else "call_paid"
                out.append(s)
        elif s.amount > 0:
            fund = next((c for c in commitments if mentions(c, text)), None)
            if fund is not None:
                s.commitment, s.kind = fund, "distribution"
                out.append(s)
    return sorted(out, key=lambda x: x.posted, reverse=True)


def mark_paid_from_line(workspace: Workspace, call_id: int, line: dict[str, Any]) -> CapitalCall:
    call = CapitalCall.objects.filter(commitment__account__family__workspace=workspace, pk=call_id).first()
    if call is None or call.status not in (CapitalCall.Status.RECEIVED, CapitalCall.Status.APPROVED):
        raise ValueError("That call can't be marked paid from the bank (it's not open, or it has a Campfire bill).")
    with db_transaction.atomic():
        mark_call_paid(call, date.fromisoformat(str(line["posted_at"])[:10]), source_ref=ref(line))
        call.refresh_from_db()
        call.bill_note = f"Paid by wire (Campfire bank line {line['id']}); categorize that line to Investment cash clearing in Campfire."
        call.save(update_fields=["bill_note"])
    return call


def dismiss(workspace: Workspace, line_id: int, actor: str) -> None:
    BankLineDismissal.objects.get_or_create(workspace=workspace, bank_line_id=line_id, defaults={"dismissed_by": actor})
