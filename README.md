# Family Office for Campfire

A Campfire partner app that adds family-office operations to Campfire's general ledger: legal entities and ownership, investments and alternatives, reconciliation, documents and K-1s, liquidity and family reporting. It installs as a **Campfire OAuth app**, signs people in with their Campfire account, and shows its pages **inside Campfire** (a "Family Office" sidebar section, and later tabs and buttons on bills and journal entries).

Plan and requirement mapping: [`docs/PLAN.md`](docs/PLAN.md), [`docs/campfire_fit_map.csv`](docs/campfire_fit_map.csv).

## What exists today (P0 foundation, M1, M2 Investments, custodian file import)

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
| Fictional demo family with entities and investments (dates relative to today) | `python manage.py seed_demo` |

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
4. `python manage.py build_manifest` writes `manifest.json` for your `APP_URL`. Paste it into the app's **Manifest** tab, save, then **Promote** the development version.
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

## Permissions (scopes)

`CAMPFIRE_SCOPES` (default `openid email profile`) feeds both the manifest's `allowed_scopes` and the scope requested at connect time, so they always match. Add data scopes as features need them, e.g. journal entries and bills for the GL bridge and capital-call flow. When a new scope is added, rebuild and re-paste the manifest, then reconnect the workspace so the token carries it.

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
templates/              server-rendered pages (frames + standalone)
tests/                  pytest
docs/                   plan and requirement mapping (client workbooks stay in docs/client/, gitignored)
scripts/                annotate a requirements matrix with the fit map
```
