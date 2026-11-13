# Family Office for Campfire: working notes for Claude

Read `docs/PLAN.md` (product plan) and `README.md` (setup, what exists) before changing anything.

## What this is
A family-office product built as a **Campfire partner OAuth app** (we are a Campfire partner with sandbox + API access).
- Campfire = system of record for the books: GL, cash, AP/bill pay, approvals, consolidation, close.
- This app = everything family-office specific: entities & ownership, investments & alternatives, reconciliation,
  documents/K-1s, liquidity, family reporting. It shows inside Campfire via app extensions (sidebar navigation,
  panels, slots, events, hooks) and signs people in with Campfire (OIDC).
- Built for **all** single- and multi-family offices, not one client. Tenancy: Campfire workspace (`customer_id`) → `Family` → everything.
  A client requirements matrix (109 reqs) was used only as validation input; mapping in `docs/campfire_fit_map.csv`.
  Client workbooks live in `docs/client/` (gitignored, not in the repo; ask the user to re-upload if needed).
- Reference for Campfire extension mechanics: public sample app `Campfire-eng/campfire_faketax_app` (clone read-only if needed).

## Decisions already made (don't re-litigate)
- Stack: Python / Django 5.2, Postgres in prod (SQLite locally), server-rendered frames for now (React later if needed).
- Dev: ngrok tunnel to local Django on the user's machine (Campfire must reach the app over public HTTPS; a cloud session can't).
- Shared testing (user decision 2026-11-13): hosted on **Render** from `render.yaml` (web + Postgres, gunicorn, WhiteNoise,
  `APP_URL` from `RENDER_EXTERNAL_URL`). Netlify was considered and doesn't fit (no long-running Python server).
  Testers: same sandbox workspace connects directly; other workspaces via sharing on the app's Installs tab.
- The app takes Read/Draft/Write permissions but **never Approve**: app-created JEs and bills go through Campfire approval
  (default `posting_mode` = draft). Permission set by phase: `docs/campfire_permissions.md`.
- **Direction change (user, 2026-11-13, after Campfire's CTO):** Campfire modules can't be switched on/off per workspace, so
  "the Campfire GL runs in the background and the family office app is the face". Everything a family-office CFO does
  lives in our app (no confusion about what is inside or outside it): financial statements, cash, bills, journal entries,
  approvals, close. The standalone app (own sidebar, Campfire sign-in) is the front door. This supersedes "don't rebuild
  Campfire's screens". Approvals also go through our app, for the demo (supersedes "never Approve" for now; the public API
  has no approve endpoints found yet, technicalities to be worked out later).
- Masttro/Arch: the app is the investment book of record with pluggable source adapters; Masttro/Arch adapters only on demand.
- Demo data is fictional (the Alder family); never put client names or figures in code, seeds or tests.

## State (as of 2026-11-13)
Done: P0 foundation (OAuth/OIDC, signed events, single-use `cf_ctx` frames, manifest builder, encrypted tokens) ·
M1 Entities & Ownership (look-through ownership) · M2 Investments core (ledger replay with FIFO lots, valuation with
NAV roll-forward + stale flags + anomaly hold, private-fund metrics incl. IRR, capital calls, net worth roll-ups) ·
Custodian file importer (`investments/importers/`: presets + per-workspace `CustodianFormat` overrides; staged
validate → commit, all or nothing; REC-08 checks; `source_ref` de-dup; workspace-scoped `ImportBatch` + `ImportIssue`).
Three sidebar pages: Investments, Entities & Ownership, Imports. Framed pages that navigate or POST carry a signed
frame token (`fo_t`, `campfire_integration/pages.py`) issued after the `cf_ctx` check ·
M3 GL bridge (`gl/`): rules at cost (`gl/services/rules.py`, cost relief from `ledger.AccountState.relieved`), one JE per
Campfire entity and month, summarized by account + tags, sent as drafts (connect requests `journal_entries:draft`);
lineage in native fields (`source`="Family Office", `source_id`=`fo-posting-<pk>`, line `external_id`); 201/202
handling, `JournalEntry.*` events + `refresh_statuses`, release; `GLSettings.post_from` go-live date; setup of
accounts/tags; REC-04 tie-out vs trial balance. Fourth sidebar page: General ledger. `push_manifest` applies the
manifest over the API (API key). Render deploy (`render.yaml`) + "Load the demo family" button (`ALLOW_DEMO_DATA`) ·
Phase A1 Records (`records/`): generic list/add/edit/delete per record type from `records/registry.py` (`Kind`: form,
columns, search, locks, after-save checks, delete guards); forms scoped by `records.forms.scoped` (fail closed);
`AuditEntry` before/after; fifth sidebar page "Records" · Phase A2 onboarding (`records/onboarding.py`): one .xlsx
(openpyxl) with `SHEETS`, staged like custodian files (`ImportBatch` source "onboarding", content base64), opening
balances on the opening date (marked holdings dated ≤ first mark so roll-forward doesn't double count), export =
same workbook (`family_rows`); round-trip test reproduces the portfolio exactly · Phase A3 review queue
(`records/review.py`, sidebar "Review"): held marks accept/reject/correct, imports with warnings signed off, waiting
uploads; preparer ≠ reviewer via `request.fo_actor_id` ("cfu:<campfire user id>", same in frame and session;
`campfire_integration.frames.actor_for`, `CampfireUser` remembers names); `ControlSettings.require_second_person`
(default on, audited); Valuation/ImportBatch carry created_by_id and reviewed_by* · Phase A4 capital calls
(`gl/services/calls.py`, sidebar "Capital calls"): bill payload verified against the sandbox (201, source/source_id kept,
found by vendor + q; test bill deleted, GET then 404); `Bill.*` events in the manifest; statuses received → submitted
(202 draft) → billed → paid; `Commitment.campfire_vendor_id` · Phase A5 month-end close (`gl/services/close.py`, sidebar
"Month-end close"): `MonthClose` per (workspace, Campfire entity, month); reviewed/posted/approved derived, data and
sign-off confirmed, tie-out stored and stale once something posts later; posting can be limited to one entity
(`send(..., campfire_entity_id=)`), JournalPosting.created_by_id for sign-off ≠ sender · Phase A6 Home
(`records/attention.py`, first sidebar item; standalone "/" when signed in; entities moved to /fo/entities): items
urgent / to do / FYI with target page. **Phase A complete.** · Phase B1 panels (`gl/panel_views.py`; manifest
`RECORD_BUTTONS` / `RECORD_PANELS`): bill drawer + bill edit tab + bill list tab work today; journal-entry tab/button
are declared and show once Campfire adds extensions to that page (docs: buttons today on invoice page, invoice list,
bill page; panels on invoices and bill list/edit). `pages.record_framed` binds the frame to `{{record.id}}` · Phase B2a
net worth with Campfire (`gl/services/networth.py`, `CampfireBalanceSnapshot`): trial balance per booked entity,
ASSET/LIABILITY only, role-mapped accounts excluded; new roles UNREALIZED (1630) / UNREALIZED_GAIN (7300) so the
unrealized account is excluded now and FMV entries can use it later. Verified read-only on sandbox Entity 1 · Phase B2b
bank-feed matching (`gl/services/bankmatch.py`, Capital calls page). Sandbox test (user approved, all deleted): bank
account create (`/ca/api/account`, entity + chart_of_accounts_account + type CHECKING) 201; bank feed
(`/ca/api/bank-feed/<id>`) needs `posted_at` (not `date`), signed `amount`, `merchant_name` kept (`description` not);
each line posts a JE bank cash vs 000000 Uncategorized; deletes: bank txn / JE / account via POST .../delete. 131 tests passing.
Close folded into Campfire (2026-11-13): no public close-checklist or lock-period endpoints (schema has none; probes 404),
so the close page is "the investment part of Campfire's close" (intro + the 5 tasks to add to Campfire's checklist once;
tick "Investments closed" there); `close_checklist` scope not requested. Partner ask: close-checklist + lock-period API ·
UI refresh: design system `static/fo/app.css` (tokens on :root, dark via prefers-color-scheme and `data-theme`), grouped
sidebar (`campfire_integration/templatetags/nav.py` NAV, shown standalone when `signed_in`), framed pages load
`static/fo/bridge.js` (Campfire app bridge: ready → theme, resize, `[data-toast]` → toast; origin = CAMPFIRE FRONTEND_ORIGIN).
UI v2 (2026-11-13, user asked for a modern, answer-first tool; competitor named: Asseta, not to be copied): shell in
`campfire_integration/templatetags/nav.py` (`shell_context`: NAV with icons + badge counts, ACTIONS for the New menu,
search index; wrappers set `request.fo_workspace`; shown when that is set and not a panel) → `templates/includes/sidebar.html`
(standalone only) and `topbar.html` (search palette + New, also framed; index URLs carry no frame token, `app.js` adds it).
`static/fo/app.js`: palette (Ctrl/Cmd+K, `/`), menus, mobile drawer, `[data-tabs]`/`[data-tab]`/`[data-panel]` (+
`[data-tab-go]`), `table[data-sort]`, `input[data-filter]`, loading state on submit (never disables: button name/value
must post). Home = dashboard (`records/attention_views.py`; `Headline` has by_asset_class + change since last month-end).
Charts: `{% alloc rows %}` (`investments/templatetags/viz.py`, stacked bar + legend-as-table, colours by label not rank,
palette `--s1..--s8` validated light/dark). Primary buttons are ink (`--primary`), orange is the accent. The dev
server caches templates with `--noreload`; restart it after template edits. 133 tests passing.
Remaining pages reworked (2026-11-13): Review = one decision card per held mark / import (expected → reported → change,
actions beside it), empty sections hidden, control in a collapsed block · Month-end close = overall progress + a card
per entity with a step tracker and the next action · Imports = drop zone (`[data-dropzone]`) + how it works + filterable
table · Entities = KPIs, ownership as bars, filter, links to the entity record · Records = a tile per record type
(`[data-filter-cards]`), sortable lists. Every page checked at phone width (no sideways scroll).
In-frame page links (2026-11-13, user: Campfire's sidebar scroll jumps when a Family Office page is clicked; that
sidebar is Campfire's and a framed app can't control it): framed pages show `.frame-nav` (`topbar.html`, from the same
NAV) so moving between pages stays inside the frame. Trade-off: Campfire's highlighted sidebar item and its page title
above the frame don't follow in-frame moves. Never call scrollIntoView on load in a frame (it can scroll Campfire).
Frame token still lasts 1 h (not renewed on in-frame navigation). Partner ask: keep the sidebar scroll on navigation.
Market value in the GL (2026-11-13, `gl/services/fairvalue.py`): accounting policy `GLSettings.fair_value_from` (empty =
books stay at cost; set on General ledger → Setup). From that month, each month end gets one entry per Campfire entity
(`JournalPosting.kind` = fair_value, memo "market value adjustment"): Dr/Cr UNREALIZED (1630) vs UNREALIZED_GAIN (7300)
per holding for (market − cost at month end) − what open entries already booked (`FairValueItem`, summed over
OPEN_STATUSES). So the first entry is the catch-up, a sold holding reverses, a released/failed month is re-booked, a
month with no change has no entry. Proposals come through `build_preview` (after the month's activity), so Send, the
close's "Posted to GL" step and Home include them; a held month blocks that entity's later months; one live entry per
entity+month (conditional unique constraint). Tie-out adds the unrealized account, compared as of the latest month
end. Held marks aren't used; non-USD accounts are skipped. Securities without a price on the date value at cost. 140 tests.

## Next steps (in order) — Phase A (usable without us) is done; then B (native feel), then C (differentiators)
- Agreed order (user, 2026-11-13): ~~fold Month-end close into Campfire Close~~ (done as far as the API allows)
  → ~~FMV / unrealized entries into the GL~~ (done) → roles + per-user connections.
- Seamless Campfire (user question): FO staff roles without Revenue/AR; a clean sandbox workspace for the demo; ask the
  partner contact for per-workspace module switches, an app page as landing page, and app widgets on Campfire Home.
  C: documents & AI, family reports, FMV JEs, liquidity.
1. GL follow-ups: FX for non-USD accounts; lock-period check before sending
   (`lock_periods:read`); tie-out drill-down to GL lines (`transactions:read`).
2. Importer follow-ups: confirm the Pershing-style columns/codes against a real extract; positions file for
   custody position reconciliation (REC-02); withholding on dividends; reverting a committed batch (needs the GL bridge
   to reverse its JEs too).

## Available
- `CAMPFIRE_API_KEY` (sandbox) is set as an environment secret and `api.meetcampfire.com` / `docs.campfire.ai` are
  reachable (verified 2026-11-13); real JE, entity, account, tag and trial-balance payloads were inspected for the
  GL bridge. Header: `Authorization: Token <key>`; base URL `https://api.meetcampfire.com`; docs index
  `https://docs.campfire.ai/llms.txt`. Sandbox writes were approved for the clean-up and the demo books (2026-11-13); ask before other writes.

## Answered from the docs / live API (2026-11-13)
- Scope strings: live catalog `GET /ca/api/v1/oauth-apps/scopes` (works with the API key). Default set in settings.
- Rate limit 10 req/s per connection, `429` + `Retry-After`. Event topics: `JournalEntry.created/.updated/.deleted`,
  `Bill.created/.updated/.deleted/.paid/.payment` (thin payloads; re-read the record). Buttons/panels: `invoice`,
  `bill`, `journal_entry`, `vendor` only (no entity). A change that needs approval returns `202` with a draft.

## Sandbox write test (2026-11-13, user approved; everything created was deleted)
- Created with our code's request shapes: account (`number`, `name`, `account_type`, `account_subtype`) → 201;
  tag group (`name`) → 201; tag (`name`, `group`) → 201; JE from `gl.services.posting._payload` → 201 with the admin
  API key (the `202` draft path needs an app token with `journal_entries:draft`: check on first real connect).
- Campfire keeps `source`, `source_id`, `ref_number`, line `external_id`, `bank_description` and tags; listing JEs with
  `source=Family Office` finds the entry. Deleted records then 404 on GET.
- Deletes: JE, account, tag via `POST .../{id}/delete` (204); tag group only via `DELETE /coa/api/tag-group/{id}`.
- Sandbox app "Family Office" id `4992b82b-105c-411a-b346-1861cc1ff78d`: `push_manifest --scopes-only` saved v2 (dev)
  with the P1 scopes; v1 (production) had no redirect URL / URLs / extensions yet.

## Sandbox demo structure (2026-11-13, user approved)
- `campfire_demo_setup` created in workspace "Holthouse" (customer 37990): "Alder Family (Consolidated)" (58617) with
  the 8 Alder entities (58618–58625) and "Alder Family - Eliminations" (58626, linked as the parent's eliminations
  entity); the FO chart of accounts (35 accounts: operating cash, due to/from, real estate, art, notes, capital calls
  payable, mortgage, equity by entity kind, K-1 income, unrealized G/L, rental income, fees, grants, interest, taxes);
  9 fictional payees (fund GPs, trustee, counsel, custodian, CPA, adviser, insurance, property manager).
  `--teardown` removes them. Not used for the FO: departments (tags instead), products/rate cards/contract
  templates/tax rates/withholding (revenue side; MFO billing is P2), cost allocations (split by account/tag, not
  entity: management-company recharges will be intercompany JEs), fixed assets (lifestyle assets, P3).
- The app's first GL setup ran before the name-matching fix and mapped "Investments at cost" to 1500 Intercompany
  Receivable; 1501 Investments at cost (1511871) was created and the user must remap the role on the GL page.
- Dimensions now also Family Branch, Tax Treatment, Custodian (account-level, on every line). The sandbox's own 1500 is "Intercompany Receivable": setup now matches accounts by
  name, never by number alone (`gl.services.setup.plan_accounts`).
- App connected 2026-11-13 (v2 promoted): token has `journal_entries:draft`, `bills:draft`.

## App-as-the-face build order (agreed 2026-11-13)
1. ~~Clean the sandbox and seed Alder dummy data~~ (done, see below).
2. ~~Financial statements in the app~~ (done 2026-11-13, see below; was: balance sheet, income statement, cash flow, trial balance; per entity and
   consolidated; drill-down). API: `/ca/api/get_balance_sheet`, `get_income_statement`, `get_cash_flow`,
   `get_trial_balance`, `/coa/api/general-ledger`, `/coa/api/transaction`.
3. ~~Cash~~ (page done 2026-11-13, see below; bank reconciliation `/coa/api/v2/reconciliation` still to do).
4. Bills and payments (`/coa/api/v1/bill`, `bill-draft`, `bill-payments`, `/coa/api/vendor`). 5. Journal entries
   (`/coa/api/journal_entry`, `intercompany-journal-entry`). 6. Approvals in the app. 7. Roles and per-user access.
   Later: budgets, fixed assets (property, art), documents/K-1s, liquidity, reporting pack.
- Added by the user 2026-11-13 (don't lose): **partnership accounting and K-1s** (capital accounts per partner,
  allocations of income/gain, K-1 tracking and figures by entity and person) and a **Connectors page** to bring in
  investment data from the tools family offices use: Addepar, Masttro, J.P. Morgan, and the other common custodians and
  aggregators (the app already has pluggable source adapters and the custodian file importer to build on).
- Gaps for the CTO: approve endpoints; close checklist and period locks; keep sidebar scroll on navigation.

## Financial statements (2026-11-13) and the setup checklist
- `gl/services/statements.py` + `gl/statement_views.py`, sidebar "Financial statements" (`gl:statements`, in NAV_PAGES):
  Campfire computes (`reports:read`, already in the app's scopes): `/ca/api/get_balance_sheet`, `get_income_statement`,
  `get_cash_flow` (v2 is "Feature not enabled"), `get_trial_balance`; columns come back as `<period>_Total` (+ `Total`
  on the income statement), rows carry `hierarchy`. Scope = one entity or a parent with `entity_rollup=true`
  (inactive entities left out). Period presets, one column / by month / by quarter, headline totals, CSV download.
  Account names open the ledger lines (`/coa/api/transaction`, `gl:ledger`). The balance sheet shows uncategorized bank
  lines below the totals (Campfire leaves them out). Campfire's cash flow only classifies accounts that have a cash-flow
  classification, so today it is mostly net income.
- General ledger -> Setup now opens with a checklist (entities that hold accounts linked; every role mapped; go-live
  and market value optional). When the required items are done it reads "Setup is complete", the tab badge goes and the
  forms fold into "Change the setup". The setup preview lists only what still needs creating (user asked why lines
  stay after setup). 146 tests.

## Cash page (2026-11-13)
- `gl/services/cash.py` + `gl/cash_views.py`, sidebar "Cash" (`gl:cash`): cash on the books by entity from one balance
  sheet per consolidated group with `group_by=entity` (accounts of subtype BANK; "custod" in the name = at custodians);
  bank lines of the last 90 days (`/ca/api/transaction`) joined to the entry each posted (`/coa/api/transaction?
  has_parent_bank_transaction=true`, the non-BANK side; `account_type` UNCATEGORIZED = waiting). Each waiting line: pick
  the account (and payee) -> `PATCH /coa/api/transaction/<gl line id>` {account, vendor}; audited; a payee categorized
  before is suggested; lines that look like a capital call or distribution point to Capital calls.
- NOT verified against the live API: the categorize PATCH (a test write on a demo line was blocked: ask the user before
  any sandbox write). Needs scope `transactions:write` (added to the defaults): after `push_manifest` the user must
  Promote the app version in Campfire and reconnect, or categorizing answers "Reconnect".
- Demo bank accounts show a bank-feed balance of 0 (the seed sent 0); the page uses book balances instead.
- Campfire's own sidebar: one "Family Office" group, items in the app's order (Home; Investments, Entities; Capital
  calls, Imports, Review, Month-end close; Cash, Financial statements, General ledger; Records). Campfire draws that
  menu: no group headings, icons or counts on the items. Splitting into several groups is possible but untested.
  149 tests. `push_manifest` applied 2026-11-13 (menu live; `transactions:write` waits in a development version for the
  user to Promote and reconnect).

## Sandbox clean-up and demo books (2026-11-13, user: "OK to delete, and add the data needed to operate as a family office")
- Removed (sample data on Entity 1/2/3, nothing refused): 115 invoices, 91 bills, 10 intercompany entries, 168 journal
  entries, 87 revenue transactions, 5 contracts, 8 customers, 6 products, 51 vendors, 7 departments, 6 "Product Tag" tags,
  36 default accounts (revenue, COGS, payroll, startup equity...). Kept as structural: AR x2, AP x2, intercompany x6,
  Unapplied Credits, Deferred Revenue 230000, CTA, Opening Balance Equity, Realized/Unrealized Gain/Loss (FX),
  000000 Uncategorized, and 3000 Equity (parent of two of them).
- The 4 sample entities can't be deleted ("referenced by other objects"): set `active` false instead (PATCH).
- Mistake to remember: the FO chart had reused the default "Insurance Expense" (same name, different case), the wipe
  matched names case-sensitively and deleted it; `campfire_demo_setup` recreated 6550 Insurance expense (1514133).
  Match account names case-insensitively before deleting anything.
- `python manage.py campfire_demo_books` (source "Alder demo books", `--dry-run`, `--teardown`): per entity an opening
  entry on 2025-12-31 (operating cash, note receivable, art, mortgage 4.2M, related-party balances, plus custody cash and
  investments at cost from the app's investment book on that date, equity as the balancing line), one "Operating
  activity" entry per entity and month Jan-Oct 2026 (rent, mortgage, property tax, fees, grants, distributions,
  estimated taxes), 8 operating bank accounts on 1040 with 10 recent uncategorized lines (one names the Summit fund:
  bank matching), 8 open bills (67,500; one overdue). Read back: every entity balances; assets 24.7M, liabilities 4.3M.
- Campfire facts learned: bank feed lines are de-duplicated on `external_transaction_id` (without one only the first
  line per account is kept); a list filtered with `source=` was served stale minutes after creates (a rerun duplicated
  67 entries, removed again), so list unfiltered and compare (entity, date, memo) too; bank feed lines post entries of
  type expense/deposit against 000000 Uncategorized; bills post type bill to 210000 Accounts Payable.
- In the app (user): General ledger -> Setup: Link by matching name; Preview suggested setup -> Create (also creates the
  missing FO Family Branch / Tax Treatment / Custodian tag groups); "Post activity from" 2026-01-01; then Send.

## Waiting on the user
- Render: live at `https://family-office-wl37.onrender.com` (2026-11-13). `push_manifest` applied for that URL:
  allowed domain, 4 sidebar pages and JournalEntry events are live; redirect/connect/manage URLs + P1 scopes sit in
  v2 (development) until the user clicks Promote. Then: Connect, load demo data, GL setup; confirm the first draft JE
  returns 202. The host is in this environment's allowed domains (verified: healthz 200, connect -> authorize 302).
- Known limit for testers: one stored connection per workspace (the last connector's permissions); no per-user
  access control yet, so demo data only.
- From the Campfire partner contact: whether the `oauth_app_extensions` flag is still needed on the sandbox.

## Conventions
- Run tests: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && DJANGO_DEBUG=1 .venv/bin/python -m pytest`
- Derived numbers (positions, cost, gains) are computed from transactions, never stored.
- Money `Decimal` with 2 dp; amounts on transactions are gross/positive, direction from the type.
- Every page is served framed in Campfire and standalone via `campfire_integration/pages.py`; add new sidebar pages to
  `campfire_integration/manifest.py` `NAV_PAGES`.
- Work on the branch the session names (currently `claude/epic-mayer-dri74n`); commit with clear messages; never commit `.env`, `manifest.json` or `docs/client/`.
