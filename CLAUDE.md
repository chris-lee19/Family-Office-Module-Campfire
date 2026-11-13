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
frame token (`fo_t`, `campfire_integration/pages.py`) issued after the `cf_ctx` check. 65 tests passing.

## Next steps (in order)
1. **GL bridge (M3)**: posting rules → summarized, tag-dimensioned journal entries in Campfire as drafts, with lineage
   (custom fields `FO Source Ref` / `FO Batch ID`); subledger-to-GL tie-out (REC-04). Needs the API key and real scope names.
2. **Capital call → Campfire bill** (draft) → bill-paid event → `investments.services.privates.mark_call_paid`.
3. Importer follow-ups: confirm the Pershing-style columns/codes against a real extract; positions file for
   custody position reconciliation (REC-02); withholding on dividends; reverting a committed batch (needs the GL bridge
   to reverse its JEs too).

## Available
- `CAMPFIRE_API_KEY` (sandbox) is set as an environment secret and `api.meetcampfire.com` / `docs.campfire.ai` are
  reachable (verified 2026-11-13). Inspect real payloads (JEs, bills, accounts, tags, entities) before building the
  GL bridge. Header: `Authorization: Token <key>`; base URL `https://api.meetcampfire.com`; docs index
  `https://docs.campfire.ai/llms.txt`. Read-only calls only until the user says otherwise.

## Waiting on the user
- The exact OAuth scope strings: the `allowed_scopes` list from the app's Manifest tab in Campfire (UI labels ≠ scope
  names; e.g. "Chart of Accounts" = `accounts:write`). Then set the default `CAMPFIRE_SCOPES` so `build_manifest` matches.
- From the Campfire partner contact: `oauth_app_extensions` flag on the sandbox; which object types accept slots/panels
  (need bill, journal_entry, entity); event topic names (bill paid); rate limits.

## Conventions
- Run tests: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && DJANGO_DEBUG=1 .venv/bin/python -m pytest`
- Derived numbers (positions, cost, gains) are computed from transactions, never stored.
- Money `Decimal` with 2 dp; amounts on transactions are gross/positive, direction from the type.
- Every page is served framed in Campfire and standalone via `campfire_integration/pages.py`; add new sidebar pages to
  `campfire_integration/manifest.py` `NAV_PAGES`.
- Work on the branch the session names (currently `claude/epic-mayer-dri74n`); commit with clear messages; never commit `.env`, `manifest.json` or `docs/client/`.
