# How the Recommendation Engine Works (Demo Guide)

A plain-English explanation of how our Azure Cost Optimization tool decides what
to recommend. Use this to walk someone through it in a demo.

---

## The one-liner

> "For every Azure resource we look at its real configuration and its real usage
> metrics, run a set of expert rules to find savings and risks, score each
> recommendation by **confidence, risk and priority**, and rank them so the
> highest-impact action shows first. The rules make the decisions; the AI only
> writes the explanation."

---

## The big idea: two layers

There are two layers, and this is the most important thing to explain:

| Layer | What it does | Why it matters |
|---|---|---|
| **1. Rules Engine (the brain)** | Hand-written expert rules decide *what* to recommend, using real data. Fully deterministic. | Same input → **same output every time**. No random AI guessing. |
| **2. AI Layer (the writer)** | GPT explains the recommendations in nice language and catches nuanced extras. | Readable output, but it **never decides** the numbers or actions. |

**Key demo point:** *"The AI explains recommendations, it does not invent them.
That's why our results are consistent — run it 10 times, you get the same
answer."*

---

## The end-to-end flow

```
 Azure  ──▶  1. SCAN        Find every resource in the subscription/RG
             2. ENRICH      Pull deep config + 7/30/90-day usage metrics
                            + tags + age + live prices + Azure Advisor
             3. RULES       30 expert rules generate candidate recommendations
             4. SCORE       Each gets confidence + risk + priority + savings
             5. RANK        Sort by impact, remove duplicates & overlaps
             6. EXPLAIN     AI writes the human-readable description
             7. OUTPUT      Excel + dashboard + database
```

---

## What signals we analyse (per resource)

We don't just look at the SKU. For each resource we combine:

- **Configuration** — tier, redundancy, size, security settings.
- **Current SKU** — what it's running on today.
- **Usage metrics** — CPU over **7, 30 and 90 days** (average *and* peak) from
  Azure Monitor, plus a utilisation **trend** (rising / stable / falling).
- **Live pricing** — the Azure Retail Prices API for real dollar figures.
- **Azure Advisor** — Microsoft's own recommendations, folded in.
- **Tags** — to detect production vs dev/test and cost-allocation gaps.
- **Resource age** — how long it has existed (e.g. stale snapshots).
- **Dependencies** — e.g. is a disk attached to a VM, is a public IP in use.

**Demo point:** *"A recommendation to downsize is only made when the measured
CPU actually justifies it — we never blindly say 'make everything smaller'."*

---

## Multiple recommendations per resource (ranked)

Instead of one recommendation per resource, we generate **several options** and
rank them. Example for a real VM:

| Priority | Recommendation | Confidence | Risk | Est. saving |
|---|---|---|---|---|
| P1 | Move to Burstable B4ms | 99% | Medium | $318/mo |
| P1 | Downsize to D4s_v5 | 99% | Medium | $280/mo |
| P1 | Enable auto-shutdown (dev/test) | 80% | Low | $168/mo |
| P1 | Purchase 1-yr Reserved Instance | 75% | Medium | $207/mo |
| P1 | Run as a Spot VM | 80% | Medium | $442/mo |

The client sees the **menu of choices** and picks the one that fits — we don't
force a single answer.

**Important (and a great demo point):** these are **alternatives**, so when we
add up total savings we count **only the best one per resource** — we never
double-count "downsize AND reserved instance AND spot". The number you see is
honest.

---

## Each recommendation is fully explained

Every recommendation carries:

- **Title** — short action ("Move to Burstable B4ms")
- **Category** — Idle / Overprovisioned / Underprovisioned / Cost Saving /
  Performance / Security
- **Description & Reason** — what and why
- **Current configuration → Recommended configuration**
- **Estimated monthly savings** (real $ from live pricing)
- **Confidence score** (how sure we are)
- **Risk level** (Low / Medium / High)
- **Priority** (P1–P4)
- **Documentation link** (Microsoft Learn)

---

## The scoring model (explain it simply)

Three deterministic scores decide the ranking:

### 1. Confidence — "how sure are we?"
- Highest when it's a **measured fact** (a disk with no VM attached = 95%).
- High when a downsize is backed by **real low CPU** over a long window.
- Lower when we have **no metric data** (we say so and hedge).
- A **longer metric window (90d)** and **more idle headroom** raise confidence.

### 2. Risk — "what happens if we're wrong?"
- **High**: deletes data/resource, or touches production.
- **Medium**: changes capacity/performance (right-sizing).
- **Low**: a reversible config toggle.
- We infer **production vs dev** from tags and naming, so prod changes are
  treated more cautiously.

### 3. Priority — "what should they do first?"
```
priority = savings  ×  confidence  ×  risk-weight
```
So a **big, safe, high-confidence** saving beats a small, risky guess. This is
what we sort on (bucketed into P1–P4).

**Demo point:** *"We don't just rank by dollars — we rank by dollars we're
confident about and safe to apply."*

---

## The knowledge base — 30 rules across all major services

Each rule is a small "expert" for one Azure service. It looks at that service's
own configuration + usage and produces one or more recommendations. Here is the
full list with **what it checks** and an **example recommendation**.

### Compute

| # | Service | What the rule checks | Example recommendation |
|---|---------|----------------------|------------------------|
| 1 | **Virtual Machine** | power state, 7/30/90-day CPU, size, OS/license, tags | "Downsize D8s_v5 → D4s_v5 (save $280/mo)", "Buy 1-yr Reserved Instance", "Apply Azure Hybrid Benefit (Windows)", "Enable auto-shutdown (dev)", "Run as Spot" — *multiple ranked options* |
| 2 | **VM Scale Set** | instance count, CPU, Spot, prod/non-prod | "Scale in from 4 → 2 instances (CPU low)", "Use Spot for this non-prod scale set" |
| 3 | **AKS (Kubernetes)** | node pools, VM sizes, Spot, autoscaler | "Move user pool to Spot (save $338/mo)", "Enable cluster autoscaler on fixed pool" |
| 4 | **App Service Plan** | tier, CPU, worker count, sites | "Downsize P1v3 → Standard (CPU low)", "Delete plan hosting 0 apps", "Scale in workers" |
| 5 | **Web / Function App** | run state, HTTPS-only | "Stopped app still billing its plan — delete", "Enforce HTTPS-only (security)" |

### Databases

| # | Service | What the rule checks | Example recommendation |
|---|---------|----------------------|------------------------|
| 6 | **SQL Database** | tier, vCores, CPU, zone redundancy, paused | "Business Critical → General Purpose (CPU low, save ~50%)", "Halve 8 → 4 vCores", "Disable zone redundancy" |
| 7 | **PostgreSQL / MySQL** | compute tier, CPU, HA, backup retention, stopped | "GeneralPurpose → Burstable (CPU low)", "Disable HA in non-prod", "Reduce backup retention" |
| 8 | **Cosmos DB** | multi-region, backup type | "3 regions → single region for non-prod (save 66%)", "Continuous → Periodic backup" |
| 9 | **Redis Cache** | SKU, non-SSL port | "Premium → Standard for simple caching (~50% less)", "Disable non-SSL port (security)" |

### Storage

| # | Service | What the rule checks | Example recommendation |
|---|---------|----------------------|------------------------|
| 10 | **Storage Account** | tier, redundancy, used GB, lifecycle, public access, TLS, soft-delete | "Premium → Standard (save $X on 500 GB)", "GRS → LRS for non-critical", "Add lifecycle policy", "Buy reserved capacity (≥1 TB)", "Disable public blob access" |
| 11 | **Managed Disk** | attached?, SKU, size | "Delete unattached disk (100% waste)", "Premium SSD → Standard SSD (exact $ delta)" |
| 12 | **Snapshot** | age, size, incremental | "Delete 190-day-old snapshot — forgotten backup" |

### Network

| # | Service | What the rule checks | Example recommendation |
|---|---------|----------------------|------------------------|
| 13 | **Application Gateway** | empty backends, autoscale, WAF mode | "Delete idle App Gateway", "Enable autoscale", "WAF Detection → Prevention (security)" |
| 14 | **Load Balancer** | frontend/backend config, SKU | "Delete unused LB (no backend)", "Basic → Standard SKU" |
| 15 | **Public IP** | attached? | "Delete orphaned public IP (reservation fee waste)" |
| 16 | **NAT Gateway** | subnet association | "Delete NAT Gateway not attached to any subnet" |
| 17 | **VPN Gateway** | connection count | "Delete VPN Gateway with 0 connections (~$130-700/mo)" |
| 18 | **Azure Firewall** | IP config, rules, tier | "Delete idle firewall (~$900/mo!)", "Premium → Standard if IDPS/TLS unused" |
| 19 | **NSG** | risky inbound rules | "SSH/RDP open to internet — restrict source IP (security)" |
| 20 | **VNet** | peerings, subnets | "Isolated dev VNet with no peerings — review/remove" |
| 21 | **NIC** | attached?, direct public IP | "Delete orphaned NIC", "NIC has direct public IP (security)" |

### Platform / Data services

| # | Service | What the rule checks | Example recommendation |
|---|---------|----------------------|------------------------|
| 22 | **Key Vault** | network access, purge protection, SKU, expiring certs | "Premium vault with no HSM keys → Standard", "Restrict network access (security)", "Cert expiring in 60 days" |
| 23 | **Recovery Services Vault** | protected items, redundancy | "Empty vault — delete", "Geo → Local redundancy on backup storage" |
| 24 | **Log Analytics** | retention days, ingestion cap | "Reduce 365-day retention to 90", "Set a daily ingestion cap (cost guardrail)" |
| 25 | **Container Registry** | SKU, admin user | "Premium ACR → Standard (save ~$30/mo)", "Disable admin user (security)" |
| 26 | **Service Bus** | Premium tier, units | "Premium namespace → Standard if isolation not needed (save $677/mo)" |
| 27 | **Event Hubs** | Premium tier, units | "Premium namespace → Standard (save ~$750/mo per unit)" |
| 28 | **API Management** | empty APIs, Premium tier | "Delete idle APIM (~$500-3000/mo)", "Premium without multi-region → Standard" |
| 29 | **Logic App** | disabled, integration account | "Delete disabled workflow", "Unused Integration Account (~$300/mo)" |

### Cross-cutting (applies to every billable resource)

| # | Rule | What it checks | Example recommendation |
|---|------|----------------|------------------------|
| 30 | **Tag Governance** | missing cost-allocation tags | "Add tags: owner, costcenter — untagged spend can't be attributed" |

> **Note on counting:** the engine registers **30 resource-type rules** in its
> dispatch table. PostgreSQL and MySQL share one code function
> (`_rule_postgres_mysql`), and Service Bus and Event Hubs share one
> (`_rule_namespace`), because the logic is identical per family — that's why the
> table above groups PostgreSQL/MySQL into one row but the dispatch table still
> counts them (and Service Bus + Event Hubs) as separate registered types.
> Every rule can emit *several* recommendations for a single resource.

---

## Things we catch that Azure Advisor often misses

- Idle **development** resources (via tags/naming)
- **Unattached** managed disks & **orphaned** public IPs
- **Oversized** App Service plans & SQL databases (metric-gated)
- **Stale snapshots** (old, forgotten backups)
- **Premium storage/disk** used for low-performance workloads
- **Inefficient backup retention** & **Log Analytics retention**
- **Missing auto-shutdown** on dev VMs
- **Reserved Instance / Savings Plan** opportunities
- **Idle Azure Firewall / NAT Gateway / Load Balancer** (big fixed costs)
- **Inconsistent tags** breaking cost allocation

---

## Dynamic SKU right-sizing (no hardcoded lists)

When we suggest a smaller VM, we **don't** use a fixed lookup table. Instead we:
1. Read the vCPU count from the SKU name (Azure convention: the "8" in `D8s_v5`),
2. Compute the target size from measured CPU (keep peak under a safe ~60%),
3. Generate candidate sizes + a Burstable option,
4. Price each against the **live retail API**, and
5. Rank by real cost difference.

**Demo point:** *"Because pricing is live and sizing is derived from the naming
convention, it keeps working as Azure adds new VM families — nothing to
maintain."*

---

## Why the results are trustworthy

- **Deterministic** — same scan → same recommendations (critical for a daily
  pipeline and for client trust).
- **Evidence-based** — right-sizing needs real metrics; busy resources are left
  alone (no false positives).
- **Real pricing** — dollar figures come from Azure's live retail catalogue.
- **Honest totals** — alternative options are never double-counted.
- **Noise-filtered** — a correctly configured resource is **not** reported as a
  problem; a $0-saving "optimisation" is dropped (security/idle/governance items
  are always kept because they reduce risk).

---

## Quick Q&A for the demo

**Q: Is this just ChatGPT guessing?**
No. Expert rules make every decision deterministically; the AI only writes the
explanation. Results are reproducible.

**Q: How do you know a VM can be downsized safely?**
We read 7/30/90-day CPU (average and peak). We only recommend downsizing when
usage genuinely allows it, and we tell you the confidence.

**Q: Where do the savings numbers come from?**
The live Azure Retail Prices API — real prices for the resource's region and
SKU, not estimates.

**Q: Won't multiple options inflate the savings?**
No. Alternatives share an "exclusive group"; the total counts only the single
best option per resource.

**Q: What if there's no metric data (brand-new resource)?**
We still recommend, but at **lower confidence**, and we clearly say "verify
before applying".

**Q: Can it scale to a whole tenant?**
Yes — it runs per subscription and per resource group in the daily pipeline, and
the logic is stateless and cached, so it scales horizontally.

---

## Where to look in the code (if asked)

| Concern | File |
|---|---|
| Scoring: confidence / risk / priority / ranking | `backend/recommendation.py` |
| The 30 expert rules + real pricing | `backend/rules_engine.py` |
| Dynamic VM SKU right-sizing | `backend/sku_advisor.py` |
| Scanning + 7/30/90-day metrics + Advisor | `backend/azure_scanner.py` |
| AI explanation + merge + honest totals | `backend/ai_analyzer.py` |
