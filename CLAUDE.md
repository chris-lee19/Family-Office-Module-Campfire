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
manifest over the API (API key). Render deploy (`render.yaml`) + "Load the demo family" button (`ALLOW_DEMO_DATA`).
86 tests passing.

## Next steps (in order)
1. **Capital call → Campfire bill** (draft, `bills:draft`) → `Bill.paid` event (add to `EVENT_TOPICS`) →
   `investments.services.privates.mark_call_paid`. The bill's line account should be the investments-at-cost mapping:
   the GL bridge skips contributions whose call has a `campfire_bill_id` (the bill booked them).
2. GL follow-ups: period-end unrealized gain (FMV) entries; FX for non-USD accounts; lock-period check before sending
   (`lock_periods:read`); tie-out drill-down to GL lines (`transactions:read`).
3. Importer follow-ups: confirm the Pershing-style columns/codes against a real extract; positions file for
   custody position reconciliation (REC-02); withholding on dividends; reverting a committed batch (needs the GL bridge
   to reverse its JEs too).

## Available
- `CAMPFIRE_API_KEY` (sandbox) is set as an environment secret and `api.meetcampfire.com` / `docs.campfire.ai` are
  reachable (verified 2026-11-13); real JE, entity, account, tag and trial-balance payloads were inspected for the
  GL bridge. Header: `Authorization: Token <key>`; base URL `https://api.meetcampfire.com`; docs index
  `https://docs.campfire.ai/llms.txt`. Read-only calls only until the user says otherwise.

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
  entity); operating accounts 1040/1180/1250/2150/2180/6510/6520; vendors Summit Growth Partners, Crescent Ventures.
  `--teardown` removes them. The sandbox's own 1500 is "Intercompany Receivable": setup now matches accounts by
  name, never by number alone (`gl.services.setup.plan_accounts`).
- App connected 2026-11-13 (v2 promoted): token has `journal_entries:draft`, `bills:draft`.

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
