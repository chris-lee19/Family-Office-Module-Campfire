# Campfire app permissions

The app's permissions are the most it can ever request. A connecting person is still limited to their own role in the workspace. Ask for what the roadmap needs, and nothing else.

**Rule: the app never takes Approve.** Everything the app creates in Campfire (journal entries, capital-call bills) goes through Campfire's approval chain, so a person always reviews what the app prepared. This is segregation of duties by design (SEC-07), and it's a selling point for family offices.

**Draft vs Write.** The app takes both on journal entries and bills. It defaults to **Draft**, which sends entries into Campfire's approval queue. A workspace can switch to direct posting in the app's settings (`posting_mode` install config) if its controls allow.

## Tick now (P1: entity link, GL bridge, capital calls, net worth, tie-out)

| Group | Permission | Read | Draft | Write | Why |
|---|---|:-:|:-:|:-:|---|
| Accounting | Journal Entries | ✓ | ✓ | ✓ | Post investment activity (ACC-03, INT-04/05) and read back for lineage |
| Accounting | General Ledger Transactions | ✓ | | | Subledger-to-GL tie-out (REC-04), drill-down |
| Accounting | Bills | ✓ | ✓ | ✓ | Capital call → bill → Campfire approval and payment (WF-01, INT-08) |
| Accounting | Bill Payments | ✓ | | | Know when a capital call was paid (funded / unfunded update) |
| Reports | Financial Statements | ✓ | | | Cash and liabilities per entity for net worth (REP-01, INT-03) |
| Cash Management | Bank Accounts | ✓ | | | Consolidated cash (CASH-01) |
| Cash Management | Cash Management Transactions | ✓ | | | Cash actuals for liquidity view |
| Organization | Entities | ✓ | | | Link FO entities to Campfire entities |
| Organization | Chart of Accounts | ✓ | | ✓ | Onboarding creates the FO accounts (investments, unrealized G/L, capital-call payable…) |
| Organization | Custom Fields | ✓ | | ✓ | `FO Source Ref` / `FO Batch ID` on JE lines (lineage) |
| Organization | Tags | ✓ | | ✓ | FO dimensions: Asset Class, Holding/Fund, Family, Branch, Tax Treatment |
| Organization | Vendors and Payees | ✓ | | ✓ | Funds/GPs as payees for capital-call bills |
| Organization | Lock Periods | ✓ | | | Don't post into closed periods |

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

Revenue (all), Check Printing, Credit/Debit Memos and their payments, Invoice Reminder templates/settings, Leases, Amortizations, Product Bundles, Products and Services, Payment Terms, Contract Templates, Contact Titles, Statistical Accounts, Tax Rates, Validation Rules, Auto Categorization Rules, Fixed Asset classes/rules, Cost Allocations (configured in Campfire directly), Webhooks (events come through the manifest), Bank Reconciliation, Custom Reports, Ember AI.

Identity scopes `openid email profile` are separate and always requested.
