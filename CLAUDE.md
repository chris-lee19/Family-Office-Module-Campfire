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
4. ~~Bills and payments~~ (done). 5. ~~Journal entries~~ (done, see below). Agreed next (user 2026-11-13, in this order):
   ~~partnership accounting and K-1s~~ (done) -> ~~Connectors page~~ (done) -> ~~roles~~ (done; entity-level limits and per-user connections not yet) -> ~~liquidity forecast~~ (done 2026-11-14, user moved it first) -> ~~bank
   reconciliation~~ (done 2026-11-14) -> family reporting pack.
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
- The categorize PATCH is verified (see Bills below). Test writes on demo data in the sandbox are approved by the user
  (2026-11-13): restore what you change; anything beyond demo data still needs asking. Needs scope `transactions:write` (added to the defaults): after `push_manifest` the user must
  Promote the app version in Campfire and reconnect, or categorizing answers "Reconnect".
- Demo bank accounts show a bank-feed balance of 0 (the seed sent 0); the page uses book balances instead.
- Campfire's own sidebar: one "Family Office" group, items in the app's order (Home; Investments, Entities; Capital
  calls, Imports, Review, Month-end close; Cash, Financial statements, General ledger; Records). Campfire draws that
  menu: no group headings, icons or counts on the items. Splitting into several groups is possible but untested.
  149 tests. `push_manifest` applied 2026-11-13 (menu live; `transactions:write` waits in a development version for the
  user to Promote and reconnect).

## Bills page and branding (2026-11-13)
- `gl/services/bills.py` + `gl/bill_views.py`, sidebar "Bills" (`gl:bills`; framed URL `/fo/frame/bills-page`, because
  `/fo/frame/bills` is the bill-list panel): open bills from `/coa/api/v1/bill` (overdue / due soon), KPIs, "Enter a
  bill" form -> `BillRequest` (pending) -> approval IN THE APP by someone other than who entered it
  (`ControlSettings.require_second_person`, actor ids) -> `POST /coa/api/v1/bill` (source "Family Office", source_id
  `fo-bill-<pk>`): 201 = in Campfire, 202 = Campfire also holds it for approval (draft-tier connection; to make the
  app's approval the only one set `CAMPFIRE_POSTING_MODE=direct` on Render and reconnect: that also posts journal
  entries directly, user to decide). Reject needs a reason. "Mark paid" -> `POST /coa/api/v1/bill/<id>/pay` with
  `empty_transactions` [{account_id, account_name, amount, payment_date, memo, external_id}] from a BANK account.
- Verified against the sandbox 2026-11-13 (user approved test writes on demo data; everything restored): categorize
  (PATCH ledger line -> 200, line leaves the waiting list; reverted), bill create with `bills.payload` (201), pay with
  `empty_transactions` (bill -> paid, payment listed), payment removed (DELETE .../payment/<id>/void -> 204), bill
  deleted (404 after). Paying needs `bill_payments:write` (in the defaults; pending promote of v4).
  Campfire's public API has no endpoint to approve its own drafts (bill-draft is the invoice inbox), so approval lives
  in the app.
- Branding (user: make it feel like Campfire, "Family Office on Campfire"): sidebar brand, sign-in card, page titles
  "<page> · Family Office on Campfire"; light theme uses warm neutrals like Campfire's interface (cream sidebar).
  Campfire's logo is not used. 154 tests.

## Family-office vocabulary and Journal entries (2026-11-13)
- User: "speak family office". Statements follow personal-financial-statement terms: tabs Net worth / Changes in net
  worth / Cash flow / Trial balance; "Statement of net worth" (equity section = "Net worth", "Total liabilities and
  net worth"); "Statement of changes in net worth" = income and expense sections, "Excess of income over expenses",
  then a bridge from the trial balance: contributions and distributions (movements on equity accounts), bank lines not
  categorized, "Other changes" only if it doesn't foot, change in net worth, net worth at start and end. Verified to
  foot on the sandbox. Use these words in new pages (payee, net worth, contributions/distributions), not corporate ones.
- Journal entries page (`gl/services/journals.py`, `gl/journal_views.py`, `gl:journals`): ledger entries of the last
  120 days with their lines; "Write an entry" (8 lines, live balance check) and "Transfer between entities" (two
  entries: payer Dr due from related / Cr operating cash; receiver Dr operating cash / Cr due to related; accounts
  found by name) -> `JournalRequest` -> approval in the app by a second person -> `POST /coa/api/journal_entry` per
  entity (source "Family Office", source_id `fo-journal-<pk>-<n>`, ref `FO-JE-<pk>`); a refusal keeps what was posted
  and approving again sends the rest. Campfire's intercompany endpoint is not used (write shape unclear).
- Lesson from the write test: removing a bill payment (DELETE .../payment/<id>/void) leaves the payment's entry behind
  as an uncategorized entry; delete that journal entry too. 158 tests.

## Partnerships & K-1s and Connectors (2026-11-14)
- `gl/services/partnership.py` + `gl/partnership_views.py`, sidebar "Partnerships & K-1s" (`gl:partnerships`, tabs by
  `?view=`): partnerships = entities with tax classification Partnership or type LP/GP. Capital accounts for a year
  from the entity's trial balance (opening/closing = assets - liabilities; contributions = credits and distributions =
  debits on equity accounts; income by K-1 line from the income account's name: interest, dividend, gain, rent, else
  ordinary; expenses = deductions), split pro rata by direct ownership on the period's last day (`active_interests`),
  rounding to the largest partner; what doesn't allocate (uncategorized bank lines) is stated. No partner dimension in
  the ledger: special allocations and uneven contributions are not reflected (said on the page). "K-1s to issue" =
  the same figures per partner for the preparer. "K-1s to receive": `investments.K1` (account, instrument, tax year,
  status expected/estimate/final, received date, figures); one expected per private-fund / direct-private interest
  with activity up to the tax year; recorded in place, audited.
- Connectors (`investments/connectors.py` catalog, `connector_views.py`, `investments:connectors`): honest statuses.
  Connected = Campfire, bank accounts through Campfire; By file = custodian files, onboarding workbook; On request =
  J.P. Morgan, Schwab, Fidelity, Pershing, Northern Trust, Goldman, Morgan Stanley, Addepar, Masttro, Black Diamond,
  Orion, Arch, Canoe, iCapital, Carta (no adapters built: "Request" stores `ConnectorRequest`, never pretends to be
  connected). Next real adapter to build is whichever the user's prospects ask for first.
- Page tests pin `timezone.localdate` (a test broke when the date rolled over). 163 tests.

## Roles (2026-11-14)
- `campfire_integration/access.py`: capabilities view / prepare / approve / manage; roles Admin (all), Controller
  (prepare + approve), Accountant (prepare), Approver (approve), Viewer, No access. `Membership` (workspace, user_key
  "cfu:<id>", role); `Workspace.default_role` (viewer | none) for people not listed. No memberships = roles OFF
  (everyone is admin, as before); the first person to click "Turn roles on" becomes admin (audited).
- Enforced centrally: `pages.framed` / `record_framed` / `standalone` call `access.check` after the actor is known.
  GET needs view. POST: `READ_ACTIONS` (refresh, tieout, refresh_balances, setup_preview, sync) need view;
  `APPROVE_ACTIONS` and `MANAGE_ACTIONS` are (page, action) pairs; pages in `MANAGE_PAGES` (load_demo, access) need
  manage; any other POST needs prepare (so a NEW write action is closed to viewers by default; add approve/manage ones
  to the sets). A refusal renders `campfire/message.html` with 403 and nothing runs.
- People & roles page (`records/access_views.py`, `records:access`, sidebar Data): people seen in the workspace
  (`CampfireUser`, now also remembered from frames), role per person, default role, turn off; the only admin can't be
  demoted. Sidebar shows the role; viewers see a "View only" pill. Second-person rules still apply on top of roles.
- NOT done: limiting a person to certain entities or families; per-user Campfire connections (API calls still use the
  workspace's one stored connection). Buttons a role can't use are still shown (the refusal explains). 165 tests.

## Report names and structure: the naming authority (user's reporting reference, 2026-11-14)
- The user uploaded a generic "Family Office Financial Reporting Reference" (not in the repo; ask to re-upload if needed):
  "family office and wealth management accounting is different than GAAP". Its names and layers are the rule for every
  statement and report; never use "balance sheet" / "income statement" for the consolidated view. This supersedes the
  tab names in "Family-office vocabulary" below.
- Two layers. Entity layer (transactional): each legal entity keeps conventional books (ledger, bank, bills, entries,
  trial balance), often at cost. Consolidated layer (reporting): rolled up, re-measured at fair value, shown as net
  worth, changes in net worth, liquidity and performance.
- Names by whose books are shown (`gl/services/statements.py` `NAMES`, `Scope.kind`; `classify` marks partnership-like
  app entities as investment vehicles):
  - FAMILY (a consolidated group; ASC 274, estimated current value): **Statement of Net Worth** (formal name Statement
    of Financial Condition), **Statement of Changes in Net Worth**; equity section "Net worth".
  - INVESTMENT (FLP, pooled LLC, fund; ASC 946, fair value): **Statement of Assets and Liabilities**, **Statement of
    Operations and Changes in Net Assets**; "Net assets", "Net increase (decrease) from operations".
  - ENTITY (any other legal entity; its books at cost): **Entity Balance Sheet**, **Statement of Income and Expenses**;
    "Equity". Also for all: Statement of Cash Flows, Trial Balance, **Tax Basis and Unrealized Gain Report** (`view=tx`).
- Fair value in the statements (one column only; columns over time show the books): `FairValues` from the investment
  records (`fairvalue.holdings_for`), less what the unrealized account (role UNREALIZED) already carries -> line
  "Investments: adjustment to estimated current value" (or "to fair value") in assets and "Unrealized appreciation not
  in the books" in net worth. FAMILY only: "Estimated income taxes on unrealized appreciation" as a liability at
  `GLSettings.estimated_tax_rate` (default 0 = no line; set on the page, manage capability, audited). The changes
  statement carries "Change in unrealized appreciation not in the books" and "Change in estimated income taxes".
- Bank lines nobody has categorized: Campfire leaves them out of equity, so its own statement doesn't balance while any
  are open. Both statements show "Bank lines not categorized yet" as a line and net worth = assets - liabilities, so
  the two statements agree (verified on the sandbox: 22,796,838 on both; every scope foots).
- Supporting report names in use: Schedule of investments, Asset allocation, Private investments: performance
  (Investments page), Capital account statements (Partnerships & K-1s).
- From the reference, NOT built yet (the Liquidity and Cash Flow Forecast is built, see below): Budget vs. Actual / Lifestyle Expense Report (needs spending tags: family member, property,
  category); Investment Performance Report (time-weighted returns for liquid holdings; only private IRR / multiples
  exist); Statement of Cash Receipts and Disbursements; Statement of Net Worth with columns by entity or family member;
  Entity / Ownership Structure Chart as a drawing (a table today); realized vs unrealized gains as separate lines in
  the changes statement; per-entity accounting basis (cash / tax / accrual). 168 tests.

## Liquidity and Cash Flow Forecast, and Campfire's mark (2026-11-14)
- `gl/services/liquidity.py` + `gl/liquidity_views.py`, sidebar "Liquidity forecast" (`gl:liquidity`, Books, after Cash; in
  NAV_PAGES; page title is the reference's name). A view, nothing is posted. Per entity and for the family, by month
  (3 / 6 / 12 months): opening cash from the books (`cash._balances`: operating + at custodians; an entity whose custody
  cash isn't in the books gets it from the investment records, and the page says so), then
  - known: open bills on their due dates (overdue ones count today), `BillRequest`s waiting for approval, open
    `CapitalCall` notices (a call's own Campfire bill is left out so it counts once);
  - expected: `PlannedCashFlow` (workspace, Campfire entity id + name, label, in/out, amount, once / monthly /
    quarterly / twice a year / yearly, first date, optional last date; add / remove on the page, audited, needs prepare);
  - what-if `?pace=12|24|36|48`: unfunded commitments without a notice, called evenly over that many months (not stored).
  Entities are joined by Campfire entity NAME (balance sheet columns and bills carry names). Each entity pays from its
  own cash: one that goes below zero is flagged (covered by its own liquid investments or not) with a link to
  Transfer between entities. Also: liquidity tiers (cash from the books; liquid / semi-liquid / illiquid from the
  investment records by asset class), unfunded commitments with coverage (cash + liquid investments / unfunded), CSV.
- "Suggest from the books" (`suggest`, GET `?suggest=1`, then POST `add_suggested`): reads the ledger for the last 12
  complete months, takes every entry that touched a BANK account and reads it by the accounts on its other side, per
  entity and account. Monthly / about quarterly (gaps of 2-4 months) / twice a year by the months it occurred in,
  amount = the average time; irregular = averaged per month; one-offs are not suggested. Left out: entries whose memo
  says "opening", accounts mapped to investment roles, due to / due from / intercompany, Accounts Payable and bill
  lines (open bills are counted on their own), uncategorized lines. "Prepaid", "depreciation" etc. are offered
  unticked ("usually not cash"). A suggestion keeps `campfire_account_id`, so it isn't offered twice. Ledger cap 6000
  lines (the page says when older entries were left out).
- Chart: month-end cash as thin columns on one zero baseline (`.fc-*` in app.css; a shortfall hangs below in the
  status red with a "Shortfall" pill; labels on the first, lowest and last month; hover / focus tooltip; the table is
  the full view). Verified on the sandbox and at phone width. Not done: a Home item for an entity that runs short;
  minimum cash per entity; editing a planned item in place (remove and add again). 173 tests.
- Brand tile (user, 2026-11-14: "instead of the F put the Campfire logo there too"): `static/fo/campfire-mark.png` is
  Campfire's own mark as its docs site serves it (not redrawn), in the sidebar and on the sign-in card. This
  supersedes "Campfire's logo is not used". The mark is Campfire's trademark: the user should confirm with the partner
  contact that partner apps may show it.

## Bank reconciliation (2026-11-14)
- `gl/services/reconcile.py` + `gl/reconcile_views.py`, sidebar "Bank reconciliation" (`gl:reconcile`, Books, after Cash;
  badge = statements waiting for approval; in NAV_PAGES). Campfire keeps the report (`/coa/api/v2/reconciliation`); the
  app drives it and keeps who prepared / approved in `BankReconciliation` (every call goes through the workspace's one
  connection, so Campfire only ever sees "Family Office Connector").
- A "bank account" here = an entity's balance on a GL account of subtype BANK (balance sheet `group_by=entity`; the
  account id is the row's last `hierarchy` element). Flow: Start (account, statement's last day, ending balance) ->
  tick the ledger lines that cleared -> Save -> Finish (only at zero difference; status PENDING_APPROVAL) -> Approve by
  someone else when a second person is required (APPROVED) -> Reopen (only the account's latest statement; approve
  capability) / Discard (a draft only). A statement follows the account's last one (start = last end + 1, starting =
  last ending). The FIRST statement starts at the account's first ledger line with a starting balance of 0, so every
  line is accounted for; a typed first day + starting balance starts part-way (earlier lines then stay open and the
  proof says so). Target shown: every account reconciled through the last month end.
- Figures: difference = (ending - starting) - cleared lines (0 = reconciles). Proof beside it: statement balance +
  deposits in transit - payments outstanding = adjusted bank balance vs the book balance on the statement date (trial
  balance). Both update live as lines are ticked (`form[data-recon]` in app.js). Lines the bank feed posted
  (`has_parent_bank_transaction`) are ticked for the person on a fresh statement and carry the bank's description.
- Verified on the sandbox 2026-11-14 with the admin key (test statements on Alder Management LLC, all discarded; 0 reports
  and no reconciled lines left):
  - create `POST` {entity, account (GL account id), statement_starting_date, statement_ending_date, starting_balance,
    ending_balance} -> 201; an overlapping date range for the same account -> 400 {"error": ...}.
  - `GET /{id}/source-transactions` = this report's cleared lines + EVERY line no report has cleared (any date; filters
    start_date / end_date / unreconciled / reconciled_only); `is_cleared`, amounts in `debit_amount_in_currency` /
    `credit_amount_in_currency`.
  - `POST /{id}/transactions/confirm` {selected: [gl line ids]} -> 204 and REPLACES the selection.
  - `PATCH` {status} moves freely between DRAFT / PENDING_APPROVAL / APPROVED; `approved_by` is read-only and stays
    empty; `reconciled_by` is the connection's user. Campfire does NOT lock a finished report (confirm, PATCH and
    DELETE all still work on APPROVED): the locks are the app's, and approve re-checks that it still reconciles.
  - `DELETE /{id}` -> 204 and its lines are open again. Campfire's own `reconciliation_difference` = cleared - statement
    activity (same test, opposite sign). The ledger list sorts ascending with `sort=posted_at` (`ordering=` does not).
- Needs scope `reconciliations:write` (added to the defaults, requested at the write tier also in draft posting mode;
  scopes read / draft / write / approve exist). Until the user Promotes the new app version and reconnects, the page
  answers "Promote the app's new version in Campfire, then reconnect". The OAuth-token path (vs the admin key) is
  therefore still to be confirmed on the live app.
- Not done: statement PDF / CSV upload and line matching (Campfire has parse-statement-page and bulk-match); a
  month-end close step and a Home item for accounts not reconciled; custody cash reconciles the same way but custody
  POSITION reconciliation (REC-02) is separate. 177 tests.

## Executive dashboard = Home, and the UX direction (2026-11-14)
- Manager's feedback (relayed by the user, who agrees on dashboards, less sure on "UX needs work"): bones and features
  are solid; it needs charts and diagrams throughout instead of heavy text, an executive dashboard that summarizes
  positions and actions, and higher fidelity overall, because we can't use Campfire's UX and now compete with Rillet,
  Sage and Asseta. Take: charts where they answer a question (never decoration); the working pages stay task-first.
- Home (`records:attention`, "/" standalone) is now the executive dashboard (`records/attention_views.home_page`,
  `templates/records/home.html`): net worth hero (the Statement of Net Worth's figure, change since last month end and
  this year, assets - liabilities - estimated taxes) with a month-end trend line; "Needs a decision" (top 6 items) +
  approvals waiting (bills, entries, reconciliations, marks/imports) + "Coming due"; Liquidity (4 figures + the 12-month
  cash chart + entities that run short); Net worth by entity (ranked bars); Investments by asset class; Cash by entity
  (operating / at custodians); Private fund commitments (called vs committed meters, TVPI, IRR); close, bank
  reconciliation and bills ageing. The full work list moved to "To do" (`records:work`, /fo/work, in NAV and NAV_PAGES).
- Figures come from the pages' own services (`gl/services/dashboard.py`: `net_worth`, `cash_and_liquidity`, `bills`,
  `reconciliation`), so a dashboard number equals the number on its page (net worth by entity sums to net worth before
  the family's estimated taxes). Reading them takes ~9 s of Campfire calls, so they are kept in `DashboardSnapshot`
  (JSON of strings / numbers, one per workspace): the first visit builds it, later visits show it at once (<1 s) and,
  when older than 15 minutes or from another day, the page refreshes it behind itself (`form[data-auto-refresh]` in
  app.js: POST action=refresh with `X-Requested-With: fetch` -> 204 / 502, then reloads via a frame-token URL, at most
  once per two minutes). "Refresh" does it on demand. What comes from the app's own database (work items, approvals,
  allocation, commitments, close) is always live. Not done: marking the snapshot stale from Campfire's events.
- Chart components (all in app.css, plain HTML/SVG, light + dark, tooltips on hover and focus, the page's table or
  figures as the full view): `.trend` (one series over time; points sit mid-column so labels and hit areas line up),
  `.fc-*` columns on a zero baseline, `.hbars` ranked bars (one hue; `.track.split` for two series with a key),
  `.meter` and `.ring` (a ratio against a limit), `{% alloc %}` stacked bar. Series colours in fixed order
  (`--s1`, `--s2`, ...); status red only with a label. Reuse these on other pages.
- Sidebar: 21 items, rows tightened; the signed-in person stays visible at the bottom.
- Proposed next for the UX pass (not built): an ownership structure diagram on Entities; charts on Cash (in / out by
  month), Bills (ageing), Capital calls (called / unfunded over time), Partnerships (capital account bridge),
  Investments (value over time, performance); fewer words on the working pages; print-ready statements. 180 tests.

## Sign-in and new permissions (lesson, 2026-11-13)
- Adding `transactions:write` to the defaults broke sign-in for everyone: Campfire answers `invalid_scope` ("This app may
  not request: ...") for a scope that is only in the app's development version. Fixed: the callback retries the
  authorization without the refused scopes (`SESSION_REFUSED_SCOPES`, `client.connect_scopes(without=)`), once per new
  refusal; pages that need the scope say "Reconnect". Never ship a new scope assuming it is promoted: push the manifest
  and have it promoted BEFORE the deploy that requests it. State now: production version v3 (promoted 2026-11-13) allows
  `transactions:write` and no longer `transactions:read`; the app requests write only (Campfire's tiers read < draft <
  write look cumulative: the app already reads bills and entries with draft scopes; if ledger reads fail, re-add read).
  150 tests.

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
- Known limit for testers: one stored connection per workspace (the last connector's permissions). Roles exist now
  (off until an admin turns them on); entity-level limits don't, so still demo data only.
- From the Campfire partner contact: whether the `oauth_app_extensions` flag is still needed on the sandbox.

## Conventions
- Run tests: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && DJANGO_DEBUG=1 .venv/bin/python -m pytest`
- Derived numbers (positions, cost, gains) are computed from transactions, never stored.
- Money `Decimal` with 2 dp; amounts on transactions are gross/positive, direction from the type.
- Every page is served framed in Campfire and standalone via `campfire_integration/pages.py`; add new sidebar pages to
  `campfire_integration/manifest.py` `NAV_PAGES`.
- Work on the branch the session names (currently `claude/epic-mayer-dri74n`); commit with clear messages; never commit `.env`, `manifest.json` or `docs/client/`.
