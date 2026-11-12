# Family Office on Campfire: Demo & Build Plan

Status: planning draft · Source: First Fam Requirements & Gap Matrix (109 requirements, 13 domains) · Mapping: [`campfire_fit_map.csv`](campfire_fit_map.csv)

## 1. The short answer: configure *and* build, but build *beside* Campfire, not inside it

Campfire is a closed, multi-tenant SaaS ERP. You can **configure** it (entities, chart of accounts, custom dimensions/fields, bank feeds, AP approvals, close checklist) and **extend** it through its REST API. You cannot add screens or tables inside Campfire itself.

So the shape is:

- **Campfire = the books.** System of record for the GL, cash, AP and payments, consolidation and close.
- **A Family Office layer (new app) = everything a family office has that a company doesn't.** That covers entities as *legal/ownership* objects, investments and alternatives, reconciliation, documents and K-1s, liquidity, and the family-facing reporting and portal. It reads from and writes to Campfire through the API, so to the user the two feel like one product.

That is essentially what Asseta.ai is: a multi-entity GL with an investments ledger, document AI and family reporting on one data model. The difference is that here the GL is Campfire's.

## 2. What the 109 requirements say

| Campfire fit | Count | Meaning |
|---|---:|---|
| Native | 10 | Campfire does it out of the box (multi-entity GL, consolidation, bank rec, bill pay, approvals, API) |
| Configure | 1 | Campfire with setup (family-office chart of accounts) |
| Hybrid | 14 | Campfire covers the accounting half, the FO layer covers the rest |
| Build | 70 | Not in Campfire: investments, ownership, recon, docs/K-1, portal |
| Integrate | 5 | External data feeds (custodian, pricing, Arch, manager portals) |
| Process | 9 | Governance/policy. Shown as a deliverable, not software |

Must-haves only (39): 21 Build, 6 Hybrid, 4 Integrate, 4 Process, 3 Native, 1 Configure.

Read this correctly: the matrix is investment- and reporting-heavy because First Fam already has Masttro and Arch. The domain where Campfire was named as the "TBD Accounting Tool" (Accounting & GL, Cash, AP) is almost entirely Native. The build work is the investment/FO layer, plus the **bridge** that removes re-keying between investments and the GL (ACC-03, INT-03/04/05, REC-04). That bridge is the single most valuable thing to demo.

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

Each scene shows Campfire and the FO layer working as one, and closes a named First Fam finding.

1. **"One family, one picture."** Household → branch → entity → holding drill-down. Net worth combines investments (FO) with cash and liabilities pulled live from Campfire. *(REP-01, MD-01/02/03, DASH-01/05, CASH-01, INT-03)*
2. **"A capital call, start to finish."** Drop a capital-call PDF. AI extracts fund, amount and due date, and an anomaly check runs. Reviewer approves. A **Campfire bill** is created via API → approved and paid in Campfire → status syncs back, and unfunded commitment updates. *(DA-04, WF-01, INT-08, WF-03, CASH-04, INV-05/06/07)*
3. **"Month-end without re-keying."** Import the custodian month file. The FO layer posts summarized, dimension-tagged JEs into Campfire. The **GL tie-out** shows FO ledger = Campfire GL per entity, including the bond account that historically never tied, and the GL line drills back to source. *(ACC-03, INT-04/05/10/11, REC-04 — F22, F24)*
4. **"Caught before it posted."** The morning exception report shows a document-derived valuation with a +480% change, held for review with preparer/reviewer separation. *(REC-05/06, SEC-07 — F19, F11)*
5. **"K-1 season."** Upload K-1s → structured lines 1–20 + footnotes → reviewer approves → CCH-ready export + auto year-end K-1 JE in Campfire. *(TAX-02, INT-06, ACC-07 — F21)*
6. **"Campfire does the books."** Quick tour of the native side: consolidation, bank rec, close checklist, Ember. *(ACC-01/02/04/08)*

## 7. Phasing

| Phase | Scope | Reqs |
|---|---|---:|
| P1 · Core demo | Campfire config + M1, core M2 (holdings, alts, commitments), M3 bridge, M5 extraction, REC-04/05/06, net-worth dashboard, cap-call flow | 41 |
| P2 · Extended demo | Performance (TWR/IRR), pricing, recon suite, K-1 board + CCH export, liquidity forecast, family portal with scoped access, alerts and digest | 40 |
| P3 · Product roadmap | Look-through, benchmarks, capital accounts, entity lifecycle, report scheduling and meeting packs, next-gen views, retention | 19 |
| Process | Governance deliverables (SoR matrix, data dictionary, stewardship, access reviews, runbooks) | 9 |

Phase is per requirement in `campfire_fit_map.csv`. Edit that file and rerun `scripts/annotate_matrix.py` to refresh counts.

## 8. Things to verify with Campfire before building

1. **Sandbox and API access** (partner/dev tenant, API token scopes, rate limits). This blocks everything in M3.
2. **Entity model limits.** Count and pricing at ~100 entities. One third-party review mentions a "primary + adjusting entity" multi-book limit, so confirm.
3. **API coverage** for: creating entities, custom dimension values, JEs with dimensions and custom fields, bills (create + read payment status), attachments, trial balance/balances by entity and dimension.
4. **Webhooks or events** (e.g. bill paid) vs polling.
5. **Embedding / SSO.** Any app marketplace, iframe/embedded-app support, or SSO/OIDC we can share so the FO layer launches from inside Campfire.
6. **MFA/SSO** for SEC-06.
7. **Bank feed coverage** for the client's banks; whether brokerage cash can feed directly.
8. **Partner arrangement.** Whether Campfire would co-market or white-label an FO vertical (this shapes "demo" vs "product").

## 9. Decisions needed

1. **Demo vs product.** Is this a sales demo for First Fam (Campfire replaces QuickBooks; Masttro and Arch stay as sources), or the start of an "Asseta on Campfire" product (FO layer replaces Masttro and Arch)? Recommendation: **build the data model as a product** (FO layer is the investment book of record) with **pluggable source adapters**, so the First Fam demo can show Masttro/Arch feeding in while the product can stand alone.
2. **Tech stack.** Proposed: TypeScript, Next.js (UI + API), Postgres (row-level security for portal scoping), Claude API for document/K-1 extraction, and a typed Campfire API client. Everything is runnable against synthetic data plus a Campfire sandbox.
3. **Demo data.** Synthetic "family" modeled on First Fam's structure but anonymized (no client names or figures in the demo data).
4. **Look & feel.** Match Campfire's visual style so the hand-off between the two apps is seamless.
