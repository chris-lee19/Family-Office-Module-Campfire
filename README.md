# Family Office for Campfire

A Campfire partner app that adds family-office operations to Campfire's general ledger: legal entities and ownership, investments and alternatives, reconciliation, documents and K-1s, liquidity and family reporting. It installs as a **Campfire OAuth app**, signs people in with their Campfire account, and shows its pages **inside Campfire** (a "Family Office" sidebar section, and later tabs and buttons on bills and journal entries).

Plan and requirement mapping: [`docs/PLAN.md`](docs/PLAN.md), [`docs/campfire_fit_map.csv`](docs/campfire_fit_map.csv).

## What exists today (P0 foundation, M1, M2 Investments, custodian file import, M3 GL bridge)

| Area | Where |
|---|---|
| Campfire OAuth: authorization code + PKCE, OpenID Connect sign-in, token refresh under a row lock, tokens encrypted at rest | `campfire_integration/client.py`, `views.py`, `models.py` |
| Verifying Campfire: HMAC-signed events, single-use `cf_ctx` context tokens, RS256 ID tokens | `campfire_integration/security.py`, `frames.py` |
| Event intake with audit log and a topic → handler registry | `/campfire/events`, `campfire_integration/events.py` |
| Manifest generated from settings | `python manage.py build_manifest` |
| Tenancy: Campfire workspace → Family (SFO = one family, MFO = many) | `entities/models.py` |
| M1 Entities & Ownership: entities with legal/tax type, effective-dated ownership, roles (trustees, managers), branches/generations | `entities/models.py` |
| Look-through ownership, branch roll-up, over/under-allocation checks, cycle detection | `entities/services/ownership.py` |
| "Entities & Ownership" page in Campfire's sidebar | `/fo/frame/entities` |
| M2 Investments book of record: per-workspace asset-class taxonomy, instruments, accounts owned by an entity or a person (taxable / tax-deferred / tax-exempt), transactions with import batch + source reference (lineage), prices, NAV/appraisal marks, commitments, capital calls | `investments/models.py` |
| Positions from transactions: cash, FIFO tax lots, cost basis, realized gains (short/long), income | `investments/services/ledger.py` |
| Valuation: price × quantity, NAV rolled forward for cash flows, stale-mark flags, **anomaly hold** on marks that move more than the threshold vs. expected (REC-06) | `investments/services/valuation.py` |
| Private funds: called, distributed, recallable, unfunded, DPI / RVPI / TVPI, IRR; paying a capital call books the contribution once | `investments/services/privates.py` |
| Net worth by person (look-through), owner, asset class, liquidity, tax treatment | `investments/services/portfolio.py` |
| "Investments" page in Campfire's sidebar | `/fo/frame/investments` (standalone: `/fo/investments`) |
| Custodian file import: layouts as presets (Pershing-style activity and price files, generic spreadsheet) with per-workspace overrides (`CustodianFormat`, edited in `/admin`); validate → review → import, all or nothing; REC-08 checks (field mapping, signs, principal/fees/net tie-out, fee vs transfer, duplicates by `source_ref`, ledger replay refuses oversells); every row keeps its batch and source reference | `investments/importers/` |
| "Imports" page in Campfire's sidebar: upload, findings, preview, import or discard | `/fo/frame/imports` (standalone: `/fo/imports`) |
| M3 GL bridge: posting rules (at cost, FIFO cost relief from the ledger) turn unposted transactions into balanced journal entries, one per Campfire entity and month, summarized by GL account and FO tags; sent as drafts for Campfire approval with lineage (`source`, `source_id`, line `external_id`); `JournalEntry.*` events confirm or release them; GL start date for go-live | `gl/` |
| GL setup: link entities, map roles to GL accounts (narrower mappings by entity / asset class / account in `/admin`), create the suggested FO accounts and the FO Family / Asset Class / Holding dimensions | `gl/services/setup.py` |
| Subledger-to-GL tie-out (REC-04): custody cash and investments at cost per entity vs Campfire's trial balance, with unposted and awaiting-approval amounts explained | `gl/services/tieout.py` |
| "General ledger" page in Campfire's sidebar | `/fo/frame/gl` (standalone: `/fo/gl`) |
| "Records" page: set up and maintain families, branches, people, entities, ownership, roles, asset classes, securities, accounts, commitments, capital calls, transactions (incl. opening positions), NAV statements and prices. Choices scoped to the workspace; every change audited (before/after); ledger and ownership checks undo a bad change; GL-posted, imported and call-paid transactions are locked; deletes never cascade through the books | `records/` |
| Onboarding from one Excel workbook (Records → Onboard a family): branches, people, entities, ownership, roles, securities, accounts with opening cash, prices, opening positions as tax lots, fund commitments with flows to date, marks, optional history; validate → review → import, all or nothing; re-upload adds nothing; sets the GL start date after the opening date. The same workbook exports a family (example and backup) | `records/onboarding.py` |
| "Review" page (SEC-07): accept / reject / correct marks held by the anomaly check, sign off imports that came in with warnings, see uploads left waiting; preparer ≠ reviewer enforced by Campfire user id (switchable for one-person offices, audited); names of people seen signing in show in audit trails | `records/review.py` |
| "Capital calls" page (INT-08 / WF-01): a call notice goes to Campfire as a draft bill to the fund's GP vendor, in the owning entity, on Investments at cost with FO tags (`source_id` fo-call-<pk>); Bill events or "Check Campfire" move it to approved and paid; payment books the contribution (unfunded drops) and the GL bridge skips it; a deleted bill sends the call back. GP vendor per fund (auto-match by manager name); record distributions (return of capital, recallable, income, gains) | `gl/services/calls.py` |
| "Month-end close" page: per Campfire entity and month, data loaded (confirmed, with evidence: custody activity, last import, stale prices and marks) → reviewed → posted → approved in Campfire → ties to the trial balance at month end → signed off by a second person; next action in place (send that entity's entries, check Campfire, run the tie-out, sign off, reopen with a reason) | `gl/services/close.py` |
| "Home" (REC-05): everything that needs a person, most urgent first, each with a button to the page that fixes it: held marks, imports to sign off or re-import, overdue / unsent / blocked capital calls, stale prices and marks, GL setup gaps, entries held back / ready / failed / slow to approve, last month not closed, tie-out breaks, setup gaps; headline net worth, unfunded and calls due | `records/attention.py` |
| Inside Campfire's records (Phase B1): a "Family Office" drawer button on bills and a tab in the bill edit form (the capital call, commitment, called / unfunded, payment check; a GP bill keyed in Campfire can be recorded as a capital call), a "Capital calls" tab on the bill list, and a tab / button on journal entries with the entry's lines, the transactions behind them and their source files. Record-bound frames: the context token must name the record | `gl/panel_views.py`, `campfire_integration/pages.py` |
| Net worth with Campfire (Reporting): each booked entity's bank cash, other assets and liabilities from its trial balance, added to the investment book at market value and split to people by look-through; accounts the GL bridge mirrors (every role-mapped account) are left out so nothing is counted twice; dated snapshots refreshed from the Investments page | `gl/services/networth.py` |
| Bank feed (Cash Management) → capital calls and distributions: recent Campfire bank lines (signed: in > 0, out < 0) that match an open call (amount, fund/GP named, dates) → mark paid (no bill) or "pay the bill in Campfire" (with a bill); money in naming a fund → record the distribution, split, summing to the wire; used lines aren't suggested again; "Not related" hides one | `gl/services/bankmatch.py` |
| Part of Campfire's close: the Month-end close page is the investment part of Campfire's Close checklist; it lists the five tasks to add to that checklist once and says when to tick "Investments closed" (Campfire has no public close-checklist API yet) | `templates/gl/close.html` |
| Look and feel: one design system (`static/fo/app.css`: tokens, light + dark, KPI tiles, section boxes, tables, pills, tabs, charts) and one script (`static/fo/app.js`). Shell: grouped sidebar with icons and open-item counts (standalone), a top bar with **search** (Ctrl/Cmd+K or `/`: pages, actions, record lists, entities, people, accounts, funds) and a **New** menu, both also inside Campfire, where links stay in the frame. Home is a dashboard (headline numbers with the change since last month-end, the work list, allocation, last month's close, upcoming calls, recent changes); Investments has an allocation switcher (asset class / owner / person / liquidity / tax), sortable tables and a holdings filter; General ledger is tabbed; Review is one decision card per item; Month-end close is a card per entity with a step tracker; Imports has a drop zone; Entities shows ownership as bars; Records is a tile per record type. Inside Campfire `static/fo/bridge.js` takes Campfire's theme, resizes the frame and shows results as Campfire toasts | `templates/base.html`, `templates/includes/`, `campfire_integration/templatetags/nav.py`, `investments/templatetags/viz.py` |
| Market value in the GL: an accounting-policy date on General ledger → Setup ("Book market value from"; empty keeps the books at cost). From that month, each month end gets one entry per Campfire entity that brings unrealized appreciation (1630) to market value less cost against unrealized gain / loss (7300), per holding, for the change since what is already booked: the first entry catches up, a sold holding reverses, a released month is booked again. Sent with the month's activity, part of the close's "Posted to GL" step, and covered by the tie-out | `gl/services/fairvalue.py` |
| Financial statements from Campfire's books, read in the app: balance sheet, income statement, cash flow, trial balance; one entity or consolidated; one column, by month or by quarter; headline totals; CSV; any account opens its ledger lines | `gl/services/statements.py`, `gl/statement_views.py` |
| Fictional demo family with entities and investments (dates relative to today) | `python manage.py seed_demo` |
| Demo books in a Campfire sandbox for the Alder entities (after `campfire_demo_setup`): opening balances that carry the investment book's custody cash and cost, this year's operating activity, operating bank accounts with recent lines, open bills; `--dry-run`, `--teardown` | `python manage.py campfire_demo_books` |

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # then fill it in (see below)
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser   # optional, for /admin
.venv/bin/python -m pytest
```

### 1. Start ngrok and the app

Campfire calls the app (OAuth redirect, frames, events), so it needs a public HTTPS URL.

```bash
ngrok http 8000
```

Put the `https://….ngrok-free.app` URL in `.env` as `APP_URL`, then:

```bash
.venv/bin/python manage.py runserver 8000
```

A reserved ngrok domain keeps `APP_URL` stable. Otherwise every new URL means rebuilding and re-pasting the manifest.

### 2. Create the app in Campfire (sandbox)

1. **Developer → Apps → New app.** Only the name matters here; the manifest sets everything else, including permissions/scopes.
2. Copy the **client ID** and **client secret** into `.env` (`CAMPFIRE_CLIENT_ID`, `CAMPFIRE_CLIENT_SECRET`).
3. **Rotate the signing secret** and copy the `whsec_…` value (shown once) into `CAMPFIRE_SIGNING_SECRET`.
4. Apply the manifest for your `APP_URL`: `python manage.py push_manifest` (uses `CAMPFIRE_API_KEY`; `--dry-run`
   shows the changes first). Or `python manage.py build_manifest` and paste `manifest.json` into the app's
   **Manifest** tab. Then **Promote** the development version on the app page.
5. Ask Campfire to enable the **`oauth_app_extensions`** feature flag on the sandbox workspace. Without it, no sidebar pages or buttons appear and no events are sent.

### 3. Connect and seed

1. Click **Connect** on the app's page in Campfire, or open `APP_URL` and choose **Log in with Campfire**.
2. Seed the demo family into that workspace (its customer ID is in `/admin` → Workspaces):

   ```bash
   .venv/bin/python manage.py seed_demo --customer-id <id>
   ```
3. In Campfire, open **Family Office → Entities & Ownership** in the sidebar.

### 4. Import a custodian file

On **Family Office → Imports**, choose a layout, upload the file and review the findings; nothing is posted until
you click **Import**. The demo family's sample files are linked on that page. From the command line:

```bash
.venv/bin/python manage.py import_custodian_file --write-sample activity activity.csv
.venv/bin/python manage.py import_custodian_file --customer-id <id> activity.csv --create-securities           # validate only
.venv/bin/python manage.py import_custodian_file --customer-id <id> activity.csv --create-securities --commit  # import
.venv/bin/python manage.py import_custodian_file --customer-id <id> --list-formats
```

Custodian rows are matched to accounts by the account's **external ID** (the custodian account number) and to
securities by CUSIP, then symbol. Re-importing a file, or a file that overlaps an earlier one, only adds rows not
already in the book. The Pershing-style layout is illustrative; check its columns and activity codes against a real
extract and adjust the preset or add a workspace layout.

### 5. Post to the GL

On **Family Office → General ledger**: set the go-live date (**Post activity from**), link each FO entity to its
Campfire entity, then **Preview suggested setup** → **Create in Campfire** (or map roles to existing accounts by hand).
**Ready to post** shows the entries and anything holding one back; **Send** puts them in Campfire's approval queue.
**Compare with Campfire's GL** runs the tie-out.

## Shared test environment on Render

For people other than the developer to test in the Campfire sandbox, the app runs on Render with its own HTTPS
address (no ngrok). `render.yaml` defines the web service and a Postgres database.

1. **Deploy.** In Render: **New → Blueprint**, connect this GitHub repository and pick the branch. Render creates
   `family-office` (web) and `family-office-db` (Postgres), generates `DJANGO_SECRET_KEY` and
   `FIELD_ENCRYPTION_SECRET`, and asks for the Campfire values:
   - `CAMPFIRE_CLIENT_ID`, `CAMPFIRE_CLIENT_SECRET`: Campfire → Developer → Apps → Family Office → Actions → View credentials
   - `CAMPFIRE_SIGNING_SECRET`: rotate the signing secret on the app page and copy it (shown once)
   - `CAMPFIRE_API_KEY`: a workspace admin's API key (only for `push_manifest`)

   `APP_URL` defaults to the service's `https://….onrender.com` address. Migrations run on every start; every push
   to the branch redeploys.
2. **Point Campfire at it.** In the service's **Shell**: `python manage.py push_manifest` (try `--dry-run` first).
   This sets the redirect URL, connect/manage URLs, allowed domain, sidebar pages and events.
3. **Promote** the new version on the app page in Campfire.
4. **Connect.** In the sandbox workspace: Connections → Family Office → Connect. The app's API calls use the most
   recent connection's permissions, so the last person to connect should be able to create journal entries and bills.
5. **Sandbox structure (optional, for the demo).** `python manage.py campfire_demo_setup` (API key; `--dry-run`,
   `--teardown`) creates the Alder entities under a consolidation parent with an eliminations entity, operating
   GL accounts and the demo funds' GPs as vendors. Matching is by name, so it is safe to run again.
6. **Demo data.** Open Family Office → Investments and click **Load the demo family** (only when `ALLOW_DEMO_DATA=1`;
   keep it off where real client data lives).
7. **GL.** Family Office → General ledger: set the go-live date, **Link by matching name**, **Preview suggested
   setup** → **Create in Campfire** (an existing account is reused only when its name matches; a taken number moves
   to the next free one), then **Send**.
8. **Testers.** People in the sandbox workspace connect from Connections. People in other Campfire workspaces: share
   the app by email on its **Installs** tab; each of their workspaces then connects and loads its own demo data.

Optional: `python manage.py createsuperuser` in the Shell for `/admin` (custodian layouts, narrower GL mappings).

## Permissions (scopes)

`CAMPFIRE_SCOPES` is the app's permission ceiling, written to the manifest's `allowed_scopes`. Leave it empty for the
default P1 set (`config/settings.py`, explained in `docs/campfire_permissions.md`). At connect time the app requests
the same scopes, except journal entries and bills at the **draft** tier, so everything it creates goes through
Campfire approval (`CAMPFIRE_POSTING_MODE=direct` requests write instead). After changing scopes, rebuild and re-paste
the manifest, promote the version, then reconnect the workspace so the token carries them.

## Secrets

Never commit `.env` or paste credentials into chat or tickets. Production requires `DJANGO_SECRET_KEY` and `FIELD_ENCRYPTION_KEYS` (Fernet keys; the first encrypts, all decrypt, so keys can rotate). Tokens are stored encrypted with them.

## Layout

```
config/                 Django settings and URLs
core/                   encryption, framing helpers
campfire_integration/   OAuth, API client, security, events, manifest
entities/               M1: families, branches, people, entities, ownership, roles
investments/            M2: instruments, accounts, transactions, marks, commitments; ledger, valuation, privates, portfolio;
                        importers/ (custodian file layouts, parsing, validation and import)
gl/                     M3: posting rules, journal postings to Campfire, entity/account/tag setup, tie-out
records/                setup and data entry for everything above (registry of record types, audit trail)
templates/              server-rendered pages (frames + standalone)
tests/                  pytest
docs/                   plan and requirement mapping (client workbooks stay in docs/client/, gitignored)
scripts/                annotate a requirements matrix with the fit map
```
