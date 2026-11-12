# Family Office on Campfire: Product & Build Plan

Status: planning draft · Product: a family office app for **any** single- or multi-family office, built as a Campfire partner OAuth app · Reference requirements: one client's Requirements & Gap Matrix (109 requirements, 13 domains), used as a validation input, not the spec · Mapping: [`campfire_fit_map.csv`](campfire_fit_map.csv)

## 1. The short answer: configure Campfire, and build a Campfire OAuth app that lives inside it

You can **configure** Campfire (entities, chart of accounts, custom dimensions/fields, bank feeds, AP approvals, close checklist). Everything family-office-specific is built as a **Campfire OAuth app** with app extensions. Its tables and logic run in our service, but it surfaces *inside* Campfire's UI: a sidebar section, tabs and buttons on Campfire records, and sign-in with Campfire. See section 3a.

So the shape is:

- **Campfire = the books.** System of record for the GL, cash, AP and payments, consolidation and close.
- **Family Office app = everything a family office has that a company doesn't.** That covers entities as *legal/ownership* objects, investments and alternatives, reconciliation, documents and K-1s, liquidity, and the family-facing reporting and portal. It reads from and writes to Campfire with the workspace's OAuth token, and its screens appear inside Campfire. To the user it is one product.

That is essentially what Asseta.ai is: a multi-entity GL with an investments ledger, document AI and family reporting on one data model. The difference is that here the GL is Campfire's.

## 1a. Product scope: every family office, not one client

The reference matrix came from one single-family office (SFO) that already runs Masttro and Arch, so it under-weights things other family offices need. The product is designed for the general case:

**Tenancy.** One Campfire workspace = one tenant (`customer_id`). Inside it, `Family` is a first-class object, so the same app serves:
- an **SFO**: one workspace, one family;
- an **MFO**: one workspace, many client families, with staff permissions scoped per family;
- an **MFO that runs one workspace per family**: many installs, one app.

Every FO record is scoped `workspace → family`.

**Domains beyond the reference matrix** (all optional per tenant):

| Domain | What's needed | Campfire fit | Phase |
|---|---|---|---|
| Investment partnerships | Capital accounts, allocations of income/gain by %, contributions/withdrawals, partner statements | Build (posts to Campfire equity accounts) | P2 |
| Trust / fiduciary accounting | Principal vs income, trustee & beneficiary roles, distributions to beneficiaries | Build + Campfire dimensions | P2 |
| Philanthropy | Foundation grants (Campfire bills), 5% minimum distribution tracking, DAF tracking | Hybrid | P2 |
| MFO fee billing | AUM / flat / hourly fee calc per family → **Campfire AR invoices** | Hybrid (Campfire AR native) | P2 |
| Household & lifestyle | Personal bill pay per family member (Campfire AP), homes, aircraft, art, vehicles, insurance policies | Hybrid | P2 |
| Real estate & liabilities | Properties, mortgages, lines of credit, loans against portfolios, intra-family loans (AFR) | Build + Campfire GL | P2 |
| Direct private investments | Cap tables, rounds, SAFEs/notes, valuations | Build | P3 |
| Digital assets | Wallet/exchange balances, cost basis | Integrate | P3 |
| Multi-currency / international | FX on positions; non-US entities | Hybrid (Campfire FX native) | P2 |
| Entity compliance | Annual reports, registered agents, franchise tax, trust Crummey notices | Build (calendar + tasks) | P2 |
| Family governance | Meeting packs, education / next-gen access | Build | P3 |

**Configurability over customization.** Taxonomies (asset classes, entity types, roles), posting rules, anomaly thresholds and report packs are tenant settings, not code. That way a new family office is onboarded by configuration.

## 2. What the reference matrix says

| Campfire fit | Count | Meaning |
|---|---:|---|
| Native | 10 | Campfire does it out of the box (multi-entity GL, consolidation, bank rec, bill pay, approvals, API) |
| Configure | 1 | Campfire with setup (family-office chart of accounts) |
| Hybrid | 14 | Campfire covers the accounting half, the FO layer covers the rest |
| Build | 70 | Not in Campfire: investments, ownership, recon, docs/K-1, portal |
| Integrate | 5 | External data feeds (custodian, pricing, Arch, manager portals) |
| Process | 9 | Governance/policy. Shown as a deliverable, not software |

Must-haves only (39): 21 Build, 6 Hybrid, 4 Integrate, 4 Process, 3 Native, 1 Configure.

Read this correctly: the reference matrix is investment- and reporting-heavy because that client already has Masttro and Arch. The domain where Campfire was named as the "TBD Accounting Tool" (Accounting & GL, Cash, AP) is almost entirely Native. The build work is the investment/FO layer, plus the **bridge** that removes re-keying between investments and the GL (ACC-03, INT-03/04/05, REC-04). That bridge is the single most valuable thing to demo.

## 3. Architecture

```
          ┌──────────────────────── Family Office layer (new app) ────────────────────────┐
 Sources  │ M5 Docs & AI      M2 Investments        M1 Entities &      M9 Reporting &    │
 ───────► │ notices, K-1s,    holdings, lots, alts, Ownership          Family Portal     │◄── family,
 custodian│ statements → LLM  commitments, calls,   legal entities,    net worth, perf,  │    trustees,
 files,   │ extraction +      NAV, valuations,      ownership graph,   dashboards,       │    principal
 PDFs,    │ review queue      performance           households         scoped access     │
 Arch,    │                                                                              │
 pricing  │ M4 Recon & DQ     M6 Tax                M7 Cash &          M8 Workflow &     │
          │ breaks, anomaly,  K-1 board, G/L,       Liquidity          Alerts            │
          │ GL tie-out        CCH export            forecast           cap-call flow     │
          │                                                                              │
          │                   M3 GL Bridge: posting engine + lineage + entity sync       │
          └───────────────────────────────┬───────────────────────────▲──────────────────┘
                     JEs, bills, entities │  Campfire REST API         │ balances, cash, AP status,
                                          ▼                            │ entities, COA, dimensions
          ┌──────────────────────────── Campfire (configured) ──────────────────────────┐
          │ Multi-entity GL · FO chart of accounts · custom dimensions · bank feeds &     │
          │ bank rec · bill pay + approvals · consolidation · close · Ember AI            │
          └──────────────────────────────────────────────────────────────────────────────┘
```

Design rules that make it feel like one product:

1. **Shared keys.** Every FO entity stores its `campfire_entity_id`. Every holding maps to a Campfire GL account and an `Asset Class` / `Holding` dimension value. No parallel IDs.
2. **Campfire owns money movement.** Capital calls, tax estimates and expenses become Campfire bills, approved and paid in Campfire. The FO layer never pays anything.
3. **Summarized posting, full lineage.** The FO layer posts summarized JEs (per entity, account and period) and each JE line carries a source reference that drills back to the transaction and PDF in the FO layer.
4. **Deep links both ways.** FO layer GL figures link into Campfire; Campfire JE memos and attachments link back to the FO record.
5. **One taxonomy.** The asset-class taxonomy lives in the FO layer and is pushed to Campfire as dimension values (fixes F2-type fragility).

## 3a. How the app plugs into Campfire (OAuth app extensions)

Reference: Campfire's sample partner app [`Campfire-eng/campfire_faketax_app`](https://github.com/Campfire-eng/campfire_faketax_app) (Flask; exercises every extension point) and https://docs.campfire.ai/guides/oauth-apps.

The whole app is declared in one **manifest** pasted into Developer → Apps: details, scopes, allowed domain, slots, panels, navigation, events, hooks. Extension points and how we use them:

| Campfire extension | What it is | Family Office use |
|---|---|---|
| OAuth 2.0 + PKCE, OIDC (`openid email profile`) | "Login with Campfire"; per-workspace access/refresh tokens. Endpoints `/ca/oauth/authorize`, `/ca/oauth/token`, JWKS at `/.well-known/jwks.json` | No separate user accounts; Campfire role determines what a user can do |
| **Navigation** (sidebar section with pages, full-width iframes) | A "Family Office" section in Campfire's sidebar | Households & Net Worth, Entities & Ownership, Investments, Capital Calls, Reconciliation, Documents & K-1s, Liquidity |
| **Panels** (tabs on detail / edit / list pages) | Our iframe as a tab on a Campfire record | "Investment detail" tab on a JE (lineage back to source), "Capital call" tab on a bill (notice PDF, extraction, unfunded impact) |
| **Slots**: link / webhook / iframe buttons on detail header, list header, row menu, with `visible_when` and `confirm_text` | Buttons on Campfire records that call us | "Post investment batch", "Open source document", bulk "Tie out to FO ledger" |
| **Event subscriptions** (signed thin events) | Campfire tells us when records change | Bill paid → mark capital call funded and update unfunded (INT-08); JE changed → re-run tie-out |
| **Hooks** (synchronous pre-save, 5s budget, can modify or reject) | Validate or enrich a Campfire record before it saves | Reject a manual JE to an investment control account that bypasses the FO ledger; auto-tag `Holding` / `Asset Class` on capital-call bills |
| **Install config** (`/ca/api/v1/oauth-app-install/config`) | Per-workspace settings stored in Campfire | Posting rules, default accounts, anomaly thresholds |
| **Onboarding** (`connect_url` + `return_to`) and **manage link** | Guided setup after Connect | Create FO custom fields and dimensions, map COA, link entities, then return to Campfire |
| **App bridge** (`app-bridge.js`: context, toast, refresh, resize, close) | iframe ↔ Campfire messaging | Feedback inside Campfire and record refresh after a write |
| Draft queue (`202` + pending draft when approval required) and the **Draft** permission level | Campfire's approval control applies to app writes too | The app takes Draft/Write but **never Approve**: investment JEs and capital-call bills land in Campfire's approval queue (SoD, SEC-07). Permission set: [`campfire_permissions.md`](campfire_permissions.md) |

Platform facts that shape the build:

- Campfire's backend is Django. APIs live under `/coa/api/...` (accounting objects) and `/ca/api/...` (company/app). App tokens are checked against scopes such as `invoices:write`, `custom_fields:write` and `accounts:write`.
- Writes that replace child lines (e.g. invoice `lines`) are delete-and-recreate. Send every writable field back, and never store Campfire line IDs as durable keys.
- Closed periods return `400`. Approvals return `202` with a pending draft. Both must be handled as normal outcomes.
- Webhooks and events are HMAC-SHA256 signed. Iframe context tokens (`cf_ctx`) are single-use JWTs; frames use a `Partitioned` cookie.
- The workspace needs the **`oauth_app_extensions`** feature flag, or no slots appear and no events are sent. Ask the Campfire partner contact to enable it on the sandbox.
- Campfire must be able to reach the app over **public HTTPS** for webhooks, events and frames, so the dev loop needs a tunnel (ngrok) or a deployed dev instance.

## 3b. Masttro & Arch: can this app do it?

**Yes for the software, staged for the data feeds.** Separate what those products *are*:

| Function | Masttro / Arch today | This app |
|---|---|---|
| Investment book of record, performance, exposure, net worth, wealth maps, portal | Masttro | **Build (P1–P2).** This is M1/M2/M9 and is needed regardless |
| Alts: commitments, calls, distributions, NAV, K-1 tracking, document AI, tasks | Arch | **Build (P1–P2).** This is M2/M5/M6/M8 |
| Custodian data feeds (direct feeds to thousands of custodians, daily pricing) | Masttro's data network | **P1:** import custodian files (First Fam is ~99% Pershing, so one feed covers the book). **P2:** a production feed through a data provider or direct Pershing file transfer |
| Collecting documents from hundreds of GP/manager portals (logins, scraping) | Arch's operations | **P1:** upload / email-in plus AI extraction. **P2:** decide between a collection partner, keeping Arch as a source through an adapter, or GP email forwarding. Portal collection is a data-operations business, not code, and is the last thing to replicate |

The design that makes both paths work: the app is the **book of record** with **pluggable source adapters** (`pershing_file`, `document_ai`, `masttro_api`, `arch_api`, ...).

- **For First Fam:** the demo can show *replacing* Masttro + Arch (Pershing file + notices/K-1s through AI), or *coexisting* (Masttro/Arch adapters feed the same ledger).
- **For the product:** it stands alone.

Recommendation: demo replacement in P1 using a Pershing-style file and real-looking documents; build the Masttro/Arch adapters in P2 only if a client wants to keep them.

## 4. Campfire configuration (no code)

| Area | Setup for the demo |
|---|---|
| Entities | ~10–15 demo entities: family trusts (GST/non-GST), LLCs, an investment partnership, a foundation, a management company. Validate pricing/limits at First Fam's ~100 entities. |
| Chart of accounts | FO template: brokerage cash, marketable securities (cost / unrealized), alternative investments, capital-call receivable/payable, unfunded commitment memo, K-1 income by type, realized/unrealized G/L, mgmt fees, trustee fees, tax estimates, due to/from. |
| Custom dimensions | `Asset Class`, `Holding / Fund`, `Custodian`, `Family Branch`, `Tax Treatment` (taxable / deferred / exempt). |
| Custom fields | On JEs: `FO Source Ref`, `FO Batch ID` (lineage). |
| Bank feeds | Operating accounts per entity; bank rec rules. |
| AP | Vendors for GPs/funds, trustees, advisors; approval chain (preparer ≠ approver, amount thresholds) for capital-call wires. |
| Close | FO close checklist: post investment batch, GL tie-out signed, K-1 accruals, consolidation review. |
| Reporting | Saved P&L/BS per entity and consolidated; cash-flow statement. |

## 5. The FO layer modules

| Module | Purpose | Key objects |
|---|---|---|
| M1 Entities & Ownership | Legal entity register beyond the books; ownership graph; households/branches | `entity`, `ownership_edge(pct, from, to, effective dates)`, `household`, `person` |
| M2 Investments | Unified book of record for liquid, alternative and illiquid assets | `account`, `security`, `holding`, `tax_lot`, `transaction`, `valuation`, `commitment`, `capital_event` |
| M3 GL Bridge | Turn investment activity into Campfire JEs; sync entities and dimensions; pull balances | `posting_rule`, `posting_batch`, `je_link`, `sync_log` |
| M4 Recon & DQ | Position, cash, cross-source and subledger-to-GL recon; anomaly rules; breaks | `recon_run`, `break`, `rule`, `signoff` |
| M5 Docs & AI | Vault, classification, LLM extraction of notices, statements and K-1s with review | `document`, `extraction`, `review_task` |
| M6 Tax | K-1 status board, realized G/L, CCH export, estimates | `k1`, `k1_line`, `tax_export` |
| M7 Cash & Liquidity | Consolidated cash, call pacing, liquidity forecast | `forecast`, `projection_assumption` |
| M8 Workflow & Alerts | Capital-call workflow, digest, alerts, calendar | `workflow`, `task`, `alert`, `calendar_event` |
| M9 Reporting & Portal | Net worth, performance, dashboards, scoped family portal | views over everything above + row-level security by household |

## 6. Demo storyline (Phase 1)

Each scene shows Campfire and the FO layer working as one. The demo uses a fictional family; the finding references (F-codes) are from the reference client's matrix and show how a prospect's pain maps to a scene.

1. **"One family, one picture."** Household → branch → entity → holding drill-down. Net worth combines investments (FO) with cash and liabilities pulled live from Campfire. *(REP-01, MD-01/02/03, DASH-01/05, CASH-01, INT-03)*
2. **"A capital call, start to finish."** Drop a capital-call PDF. AI extracts fund, amount and due date, and an anomaly check runs. Reviewer approves. A **Campfire bill** is created via API → approved and paid in Campfire → status syncs back, and unfunded commitment updates. *(DA-04, WF-01, INT-08, WF-03, CASH-04, INV-05/06/07)*
3. **"Month-end without re-keying."** Import the custodian month file. The FO layer posts summarized, dimension-tagged JEs into Campfire. The **GL tie-out** shows FO ledger = Campfire GL per entity, including the bond account that historically never tied, and the GL line drills back to source. *(ACC-03, INT-04/05/10/11, REC-04 — F22, F24)*
4. **"Caught before it posted."** The morning exception report shows a document-derived valuation with a +480% change, held for review with preparer/reviewer separation. *(REC-05/06, SEC-07 — F19, F11)*
5. **"K-1 season."** Upload K-1s → structured lines 1–20 + footnotes → reviewer approves → CCH-ready export + auto year-end K-1 JE in Campfire. *(TAX-02, INT-06, ACC-07 — F21)*
6. **"Campfire does the books."** Quick tour of the native side: consolidation, bank rec, close checklist, Ember. *(ACC-01/02/04/08)*

## 7. Phasing

| Phase | Scope | Reqs |
|---|---|---:|
| P0 · Foundation | Django app, Campfire OAuth connect + OIDC sign-in, signed events, in-Campfire frame pages, manifest, tenancy, M1 entity & ownership model | — |
| P1 · Core demo | Campfire config + M1 UI, core M2 (holdings, alts, commitments), M3 bridge, M5 extraction, REC-04/05/06, net-worth dashboard, cap-call flow | 42 |
| P2 · Extended demo | Performance (TWR/IRR), pricing, recon suite, K-1 board + CCH export, liquidity forecast, family portal with scoped access, alerts and digest, capital accounts, plus the section 1a domains marked P2 | 43 |
| P3 · Product roadmap | Look-through, benchmarks, entity lifecycle, report scheduling and meeting packs, next-gen views, retention | 17 |
| Process | Governance deliverables (SoR matrix, data dictionary, stewardship, access reviews, runbooks) | 7 |

Phase is per requirement in `campfire_fit_map.csv`. Edit that file and rerun `scripts/annotate_matrix.py` to refresh counts.

## 8. Things to verify with Campfire (partner channel)

Resolved: we are a Campfire partner with sandbox + API access, and Campfire supports OAuth apps with in-product extensions (section 3a).

Still open:

1. **`oauth_app_extensions` flag** enabled on our sandbox workspace.
2. **Which `object_type`s accept slots/panels.** The sample only shows `invoice`; we need `bill`, `journal_entry`, `vendor` and ideally `entity`.
3. **Event topics and hook topics** for bills (created / approved / **paid**), journal entries and entities.
4. **OAuth scopes** for journal entries, bills, entities, dimensions/tags, attachments and reports (trial balance / balances by entity and dimension). The search results confirm `journal_entries:write`.
5. **Entity model limits.** Count and pricing at ~100 entities; whether entities can be created by API (MD-04 lifecycle).
6. **Rate limits** and bulk endpoints for posting month-end batches.
7. **MFA/SSO** for SEC-06, and whether non-accounting users (family members) can hold a Campfire seat or need portal access outside Campfire.
8. **Bank feed coverage** for the client's banks; whether Pershing cash can feed directly.
9. **Go-to-market.** Private app for HCVT clients vs a listed app in Campfire's directory (affects app review and versioning).

## 9. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Shape: Campfire = books; FO app as a Campfire OAuth app with in-product extensions | **Agreed** |
| 2 | Product for all family offices (SFO + MFO tenancy), book of record with pluggable source adapters; Masttro/Arch adapters only on client demand | **Agreed** |
| 3 | Stack aligned to Campfire's backend: **Python / Django + Django REST Framework, Postgres** (row-level security for portal scoping), **React (Vite)** for the iframe pages styled to Campfire, Claude API for document/K-1 extraction, a typed Campfire client modeled on the sample app's (`campfire.py`: PKCE, refresh, scope-aware calls, signature + `cf_ctx` verification) | **Agreed** (Django) |
| 4 | Demo data: fictional families (no client names or figures) | Recommended |
| 5 | Dev hosting: **ngrok** tunnel to local Django | **Agreed** |

## 10. Credentials & access (never share a Campfire login)

The app never uses a person's username/password. Set up in the sandbox:

1. **Developer → Apps → New app** ("Family Office", private). Copy `CLIENT_ID` / `CLIENT_SECRET`; rotate and copy the signing secret (`whsec_...`).
2. A **sandbox API key** (admin role) for scripted Campfire configuration and demo-data seeding only.
3. Store as environment secrets, never in the repo or chat: `CAMPFIRE_CLIENT_ID`, `CAMPFIRE_CLIENT_SECRET`, `CAMPFIRE_SIGNING_SECRET`, `CAMPFIRE_API_KEY`, plus `CAMPFIRE_BASE_URL=https://api.meetcampfire.com` and `CAMPFIRE_FRONTEND_ORIGIN=https://app.meetcampfire.com` (or the sandbox equivalents).
4. Network allowlist for the build environment: `api.meetcampfire.com`, `app.meetcampfire.com`, `docs.campfire.ai`.
