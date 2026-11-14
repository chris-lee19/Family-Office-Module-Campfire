# Campfire app permissions

The app's permissions are the most it can ever request. A connecting person is still limited to their own role in the workspace. Ask for what the roadmap needs, and nothing else.

**Rule: the app never takes Approve.** Everything the app creates in Campfire (journal entries, capital-call bills) goes through Campfire's approval chain, so a person always reviews what the app prepared. This is segregation of duties by design (SEC-07), and it's a selling point for family offices.

**Draft vs Write.** The manifest's ceiling includes Write on journal entries and bills, but a connection **requests the draft tier** (`journal_entries:draft`, `bills:draft`), so Campfire answers `202` and puts what the app creates in its approval queue. Setting `CAMPFIRE_POSTING_MODE=direct` requests Write instead (Campfire can still route a write to approval). Changing it takes a reconnect.

**Scope strings** come from Campfire's scope catalog (`GET /ca/api/v1/oauth-apps/scopes`, or the scopes reference in the OAuth apps guide). The default `CAMPFIRE_SCOPES` in `config/settings.py` is the P1 set below; `build_manifest` puts it in `allowed_scopes` and subscribes only to event topics those scopes cover.

## Tick now (P1: entity link, GL bridge, capital calls, net worth, tie-out)

| Group | Permission | Scope | Read | Draft | Write | Why |
|---|---|---|:-:|:-:|:-:|---|
| Accounting | Journal Entries | `journal_entries:write` | ✓ | ✓ | ✓ | Post investment activity (ACC-03, INT-04/05), read back for lineage, `JournalEntry.*` events |
| Accounting | General Ledger Transactions | `transactions:write` | ✓ | | ✓ | Ledger lines behind the statements; categorizing bank lines on the Cash page (changes the account on the line's entry) |
| Accounting | Bills | `bills:write` | ✓ | ✓ | ✓ | Capital call → bill → Campfire approval and payment (WF-01, INT-08), `Bill.paid` events |
| Accounting | Bill Payments | `bill_payments:read` | ✓ | | | Know when a capital call was paid (funded / unfunded update) |
| Accounting | Bank Reconciliation | `reconciliations:write` | ✓ | | ✓ | Bank reconciliation page: start a statement, mark the ledger lines that cleared, finish and approve (the app keeps who prepared and approved) |
| Reports | Financial Statements | `reports:read` | ✓ | | | Trial balance for the tie-out (REC-04); cash and liabilities per entity for net worth (REP-01, INT-03) |
| Cash Management | Bank Accounts | `bank_accounts:read` | ✓ | | | Consolidated cash (CASH-01) |
| Cash Management | Cash Management Transactions | `bank_transactions:read` | ✓ | | | Cash actuals for liquidity view |
| Organization | Entities | `entities:read` | ✓ | | | Link FO entities to Campfire entities |
| Organization | Chart of Accounts | `accounts:write` | ✓ | | ✓ | GL setup creates the FO accounts (custody cash, clearing, investments at cost, income, gains, fees) |
| Organization | Tags | `tags:write` | ✓ | | ✓ | FO dimensions: FO Family, FO Asset Class, FO Holding, Branch, Tax Treatment, Custodian; spending: FO Family Member, FO Property (also tags on bill lines, entry lines and categorized bank lines) |
| Organization | Vendors and Payees | `vendors:write` | ✓ | | ✓ | Funds/GPs as payees for capital-call bills |
| Organization | Lock Periods | `lock_periods:read` | ✓ | | | Don't post into closed periods |

Custom Fields are **not** needed: journal entries carry lineage in Campfire's own fields (`source` = "Family Office", `source_id` = the posting, `ref_number`, and `external_id` on each line).

## Add later (reconnect required)

| Phase | Permission | Level | For |
|---|---|---|---|
| P2 | Recurring Journal Entries | Read, Write | Year-end K-1 and amortization entries (ACC-07) |
| P2 | Intercompany Journal Entries | Read, Draft, Write | Intra-family loans, management-company charges |
| P2 | Invoices, Invoice Payments | Read, Draft, Write / Read | Multi-family-office fee billing to client families |
| P2 | Close Checklist | Read, Write | Tick "investment batch posted / tie-out signed" in Campfire's close |
| P2 | Users | Read | Map Campfire users to family members and per-family staff access |
| P2 | Audit Logs | Read | Full lineage and audit trail (REC-10, SEC-04) |
| P2 | Departments, Projects | Read | Only if a client uses them instead of tags |
| P3 | Entities | Write | Entity lifecycle: create/dissolve Campfire entities from the app (MD-04) |
| P3 | Fixed Assets… | Read, Write | Lifestyle assets (homes, aircraft, art) if booked as fixed assets |

## Never needed

Custom Fields (lineage uses native fields), Revenue (all), Check Printing, Credit/Debit Memos and their payments, Invoice Reminder templates/settings, Leases, Amortizations, Product Bundles, Products and Services, Payment Terms, Contract Templates, Contact Titles, Statistical Accounts, Tax Rates, Validation Rules, Auto Categorization Rules, Fixed Asset classes/rules, Cost Allocations (configured in Campfire directly), Webhooks (events come through the manifest), Custom Reports, Ember AI.

Identity scopes `openid email profile` are separate and always requested.
