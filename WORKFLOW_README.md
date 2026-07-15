# AI Cloud Cost Detective — How Everything Works

A plain-English guide to the full system: what runs, in what order,
how costs are calculated, and what each resource type checks for.

---

## 1. Big Picture — One Diagram

```
         You / Azure DevOps Pipeline / Cron Job
                          │
                          ▼
              ┌─────────────────────┐
              │   Authentication    │
              │  Service Principal  │
              │  (Client ID + Key)  │
              └────────┬────────────┘
                       │  OAuth2 token
                       ▼
        ┌──────────────────────────────┐
        │   Azure Resource Manager     │
        │   (management.azure.com)     │
        └──────┬─────────────┬─────────┘
               │             │
        List all subs    List all RGs
               │             │
               ▼             ▼
        ┌─────────────────────────┐
        │   Resource Scanner      │
        │   (azure_scanner.py)    │
        │   - Lists all resources │
        │   - Enriches each one   │
        │     with deep API calls │
        └──────────┬──────────────┘
                   │
         ┌─────────┴──────────┐
         │                    │
         ▼                    ▼
  Azure Cost Mgmt       Azure Retail
  API (real spend)      Prices API
  (actual_cost_mtd)     (SKU prices)
         │                    │
         └─────────┬──────────┘
                   │
                   ▼
        ┌──────────────────────────────────────────┐
        │            Analysis (two layers)          │
        │                                            │
        │  ┌──────────────────┐  ┌────────────────┐ │
        │  │ Rules Engine     │  │  AI Analyzer   │ │
        │  │ (rules_engine.py)│  │ (ai_analyzer)  │ │
        │  │ deterministic,   │  │  GPT-4o adds   │ │
        │  │ real-price backed│  │  nuance/polish │ │
        │  └────────┬─────────┘  └───────┬────────┘ │
        │           └──────── merge ─────┘          │
        │        (dedupe on resource+category,      │
        │         rules-engine figures authoritative)│
        └──────────────────┬─────────────────────────┘
                   │
          ┌────────┴────────┐
          │                 │
          ▼                 ▼
    PostgreSQL DB      Excel Report
    (history)          (.xlsx file)
          │
          ▼
     Web App UI
  (Report + History)
```

---

## 2. Step-by-Step Execution

### Step 1 — Authentication
The app uses an Azure **Service Principal** (SP) with:
- `AZURE_TENANT_ID` — your Azure directory ID
- `AZURE_CLIENT_ID` — the SP's app registration ID
- `AZURE_CLIENT_SECRET` — the SP's password

It calls Microsoft Identity Platform to get a **Bearer token**:
```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
```
This token is used for all Azure API calls.

---

### Step 2 — Discover Subscriptions
Calls the ARM API to list every subscription the SP has access to:
```
GET https://management.azure.com/subscriptions?api-version=2022-12-01
```
Returns all **enabled** subscriptions. If the SP has Reader at the
Management Group level, it sees ALL subscriptions in the tenant.

---

### Step 3 — Discover Resource Groups
For each subscription, lists all resource groups:
```
GET /subscriptions/{sub}/resourcegroups?api-version=2021-04-01
```

---

### Step 4 — Scan Resources (Deep Enrichment)
For each resource group, lists all resources:
```
GET /subscriptions/{sub}/resourceGroups/{rg}/resources
```
Then for each resource, makes **additional API calls** to fetch
deep configuration details (power state, SKU, security settings, etc.)
See Section 4 below for exactly what each resource type fetches.

---

### Step 5 — Fetch Real Billing Data
Calls Azure Cost Management for the current month's actual spend:
```
POST /subscriptions/{sub}/providers/Microsoft.CostManagement/query
```
- Timeframe: month start → today
- Groups by: ResourceId + ResourceType
- Returns: actual spend in your billing currency

Currency is converted to USD using the Frankfurter FX API.

---

### Step 6 — Project Full-Month Cost
Since we only have part of the month's data:
```
projected = (mtd_cost ÷ day_of_month) × days_in_month
```
Example: Spent $15 by day 11 of 31 → projected = $42.27/month

**Early-month guard:** If less than 7 days have elapsed,
the projection uses Azure Retail Prices (SKU list price) instead
of raw extrapolation (which would be wildly inaccurate).

---

### Step 7 — Analysis (Rules Engine + GPT-4o)
Analysis runs in **two layers** that are then merged:

**Layer A — Deterministic Rules Engine (`rules_engine.py`)**
Hand-written rules run over the same enriched data. Same input → same output,
every run (this is what makes the daily report *consistent*). Each rule produces
a finding with a **real, price-backed** saving:
- **VMs** — running VMs get a right-size / Reserved Instance / Spot recommendation,
  priced from the live Azure Retail API (by `armSkuName`, which works for new SKUs).
- **Disks** — exact Premium→Standard-SSD saving from real per-tier prices.
- **Public IPs, App Gateway, VPN Gateway, RSV/Backup** — real region-specific prices.
- **NAT Gateway, Load Balancer, APIM, Redis** — documented list-price fallbacks
  (the retail API doesn't expose clean meters for these).

**Layer B — AI Analyzer (GPT-4o)**
Sends the full enriched inventory + billing data to GPT-4o, which:
- Checks every resource against specific issue patterns
- Adds nuanced findings the fixed rules don't cover
- Returns strict JSON with issues, severity, fix commands

**Merge:** the two lists are combined and de-duplicated on
`(resource_name, category)`. Rules-engine findings are authoritative for the
cost math; the AI fills in anything extra.

**Post-processing safety checks:**
1. Savings rebased onto actual projected spend (not catalog prices)
2. Savings capped at 90% of projected cost (can't save more than you spend)
3. Free resource types (VNets, NSGs, NICs) get $0 savings
4. Missing savings filled with category-based conservative ratios
5. Noise filter drops pure cost findings with $0 savings (avoids flagging
   already-optimal resources)

---

### Step 8 — Save Results
- Full analysis JSON saved to **PostgreSQL** (viewable in History)
- **Excel file** generated with 4 sheets
- Results appear in **Web App** Report page instantly

---

### Step 9 — Daily Automation
Two options running in parallel:
1. **Azure DevOps Pipeline** — scheduled cron `0 6 * * *` (06:00 UTC)
2. **Linux cron job on VM** — same schedule as backup

---

## 3. Cost Calculation — Exactly How Numbers Are Made

```
Real spend this month (Azure Cost Mgmt)
         ÷ days elapsed
         × days in month
         = Projected full-month cost  ← "Current Cost" in report

Projected cost × SKU reduction ratio  ← from Azure Retail Prices API
         = Optimized cost

Current cost - Optimized cost = Estimated monthly savings
```

**Example:**
```
VM Standard_D4s_v3, day 11, spent $14 MTD
→ Projected = (14÷11)×31 = $39.45/mo

Azure Retail: D4s_v3 = $0.192/hr, D2s_v3 = $0.096/hr
→ Ratio = 1 - (0.096÷0.192) = 50% reduction

Savings = $39.45 × 50% = $19.73/mo
```

### When there is no billing data (new or small environments)
Azure Cost Management only reports spend after a resource has run for a while,
so a brand-new resource can show `$0` month-to-date. To avoid reporting `$0`
savings in that case, the rules engine falls back to **live Azure Retail
prices** (or documented list prices) so every paid resource still has a real
cost basis. Representative figures used:

| Resource | Real price basis |
|---|---|
| VM (e.g. Standard_D2ads_v7) | ~$83/mo on-demand · ~$16/mo Spot (live, by `armSkuName`) |
| Managed disk (Premium P4 32 GB) | $5.28/mo → Standard SSD E4 $2.40/mo (live, per size-tier) |
| Public IP (Standard static) | $3.65/mo (live) |
| Application Gateway (Standard_v2) | ~$152/mo · WAF_v2 ~$273/mo (live, fixed + 1 capacity unit) |
| VPN Gateway (VpnGw1) | ~$139/mo (live, by SKU) |
| RSV / Backup | $10/protected instance + storage/GB by redundancy (computed) |
| NAT Gateway / Load Balancer / APIM / Redis | documented list-price fallback |

**VM savings levers** (whichever fits the workload):
- Right-size a non-burstable VM to a Burstable B-series (~40%)
- 1-year Reserved Instance / Savings Plan for always-on VMs (~37%)
- Spot instance for interruptible/dev workloads (up to ~80%)

---

## 4. Resource-by-Resource: What Gets Checked

---

### VIRTUAL MACHINES (microsoft.compute/virtualmachines)

**What we fetch:**
- Power state (running / deallocated / stopped)
- All NIC attachments → public IP detection
- NSG rules on each NIC → open SSH/RDP port detection
- VM size and SKU

**What AI looks for:**
| Check | Why it matters |
|---|---|
| Large SKU with low utilisation | Over-provisioned — paying for unused CPU/RAM |
| Power state = deallocated | Still paying for OS disk + reserved public IP |
| direct_public_ip_attached = true | Security risk — VM exposed directly to internet |
| SSH/RDP open to 0.0.0.0/0 | High security risk — remote access from anywhere |
| No auto-shutdown tag | Dev VMs running 24/7 unnecessarily |

**Real pricing (rules engine):** a *running* VM is priced live from the Azure
Retail API by `armSkuName`, and gets one consolidated recommendation —
right-size to Burstable (~40%), Reserved Instance/Savings Plan (~37%), or Spot
(up to ~80%) — with the exact dollar saving. A *deallocated* VM is only charged
for its disk, so it is never priced at the full on-demand rate.

**Common fix commands:**
```bash
# Resize VM
az vm resize --resource-group {rg} --name {vm} --size Standard_B2s

# Deallocate idle VM
az vm deallocate --resource-group {rg} --name {vm}
```

---

### DISKS (microsoft.compute/disks)

**What we fetch:**
- Disk state (Attached / Unattached / Reserved)
- managedBy (which VM owns it)
- Disk size in GB
- Disk SKU (Premium_LRS / Standard_LRS / UltraSSD_LRS)

**What AI looks for:**
| Check | Why it matters |
|---|---|
| is_orphaned = true (no managedBy) | Orphaned disk — paying for storage with no VM |
| disk_state = Unattached | Unattached — candidate for deletion |
| Premium_LRS for non-critical data | Premium SSD costs 3× Standard HDD |
| Large disk with small VM | Disk is oversized for the workload |

**Real pricing (rules engine):** the disk size is mapped to its exact
performance tier (P/E/S series) and the real fixed monthly price is fetched
live. The Premium→Standard-SSD downgrade uses the **exact** price difference
(e.g. Premium P4 32 GB $5.28/mo → Standard SSD E4 $2.40/mo).

---

### NETWORK INTERFACES / NICs (microsoft.network/networkinterfaces)

**What we fetch:**
- Private IP allocation method (Static / Dynamic)
- Attached public IP address
- Associated VM (virtualMachine reference)
- Associated NSG
- Accelerated Networking enabled/disabled
- IP forwarding enabled (security concern)
- DNS settings

**What AI looks for:**
| Check | Why it matters |
|---|---|
| NIC not attached to any VM | Orphaned NIC — unused resource |
| ip_forwarding_enabled = true | Security risk unless intentional router |
| Static private IP with no justification | Management overhead |
| No associated NSG | Missing network security boundary |

---

### VIRTUAL NETWORKS / VNets (microsoft.network/virtualnetworks)

**What we fetch:**
- Address space (CIDR blocks)
- Subnet count and each subnet's details
- Peering count
- DNS server configuration
- Each subnet: NSG attached, route table, delegations, service endpoints

**What AI looks for:**
| Check | Why it matters |
|---|---|
| vnet_is_isolated = true (no peerings, 1 subnet) | Abandoned dev VNet |
| Subnets with no NSG | Missing security boundary |
| /8 or /16 address space for small workload | Over-allocated IP space |
| No custom DNS server | Using Azure default, may cause resolution issues |

---

### NETWORK SECURITY GROUPS / NSGs (microsoft.network/networksecuritygroups)

**What we fetch:**
- All inbound security rules
- Associated subnets and NICs
- Rules with wildcard source (0.0.0.0/0 or *)
- Rules allowing SSH (22), RDP (3389), or all ports (*)

**What AI looks for:**
| Check | Why it matters |
|---|---|
| Any rule: source=Any, port=22 or 3389 | SSH/RDP open to entire internet |
| Any rule: source=Any, port=* | ALL ports open to internet |
| NSG not associated with any subnet/NIC | Orphaned NSG, wasted management |
| Allow rule overrides Deny rule | Rule ordering issue |

---

### STORAGE ACCOUNTS (microsoft.storage/storageaccounts)

**What we fetch:**
- SKU (Standard_LRS / Premium_LRS / Standard_GRS etc.)
- Access tier (Hot / Cool)
- Kind (StorageV2 / BlobStorage / FileStorage)
- HTTPS only enforcement
- Public network access setting
- Blob public access allowed
- Minimum TLS version (TLS1_0 / TLS1_1 / TLS1_2)
- Network ACLs (default action, IP rules count, VNet rules count)
- Blob soft delete enabled + retention days
- Blob versioning enabled
- Blob container count (is_empty if 0)
- Lifecycle management policy existence
- **Queue Service:** queue count, logging enabled
- **File Shares:** share count, quota, enabled protocols (SMB/NFS)
- **Table Service:** table count, logging enabled

**What AI looks for:**
| Check | Why it matters |
|---|---|
| is_empty = true (0 containers) | Likely unused — pay for account fee |
| access_tier = Hot with no recent access | Should be Cool or Archive |
| has_lifecycle_policy = false | Blobs never auto-tiered to cheaper tiers |
| allow_blob_public_access = true | Data publicly readable — Security Risk |
| network_default_action = Allow | No network restriction — open to all |
| min_tls_version = TLS1_0 or TLS1_1 | Outdated TLS — Security Risk |
| blob_soft_delete_enabled = false | No data recovery protection |
| Premium_LRS for general workload | Premium costs 3× Standard |
| Queue with no messages for 60+ days | Possibly unused service |

---

### FUNCTION APPS (microsoft.web/sites — kind: functionapp)

**What we fetch:**
- App state (Running / Stopped)
- Hosting plan (Consumption / Premium / Dedicated)
- Runtime stack + version (Python, Node, .NET, Java)
- Always On setting
- Number of active functions
- Last modified time
- HTTPS only enforcement
- App Service Plan linked
- Daily memory time quota (Consumption plan limit)
- Scale limit

**What AI looks for:**
| Check | Why it matters |
|---|---|
| State = Stopped | Paying for Dedicated/Premium plan with no use |
| Dedicated plan with low invocations | Should use Consumption (pay-per-execution) |
| Premium plan for low traffic | Premium EP1 costs $170/mo vs Consumption's pennies |
| Old runtime (Python 3.8, Node 14) | End of support, security risk |
| No function deployed (empty app) | Paying for plan with nothing on it |

---

### WEB APPS (microsoft.web/sites — kind: app)

**What we fetch:**
- App state (Running / Stopped)
- App Service Plan name and SKU
- Always On setting
- HTTPS only
- Minimum TLS version
- Custom domain count
- Deployment slots count
- Health check path configured

**What AI looks for:**
| Check | Why it matters |
|---|---|
| State = Stopped | Paying for plan with idle app |
| Premium plan for dev/test app | Should use Basic or Free |
| Always On = false on production | Cold starts hurt users |
| No HTTPS only | Insecure — traffic can go over HTTP |
| Multiple idle deployment slots | Paying for extra compute |

---

### APP SERVICE PLANS (microsoft.web/serverfarms)

**What we fetch:**
- SKU name (F1, B1, S1, P1v2, P2v3, etc.)
- SKU tier (Free, Basic, Standard, Premium, Isolated)
- Number of workers provisioned
- Number of sites on the plan
- OS type (Windows / Linux)
- Is dev/test plan

**What AI looks for:**
| Check | Why it matters |
|---|---|
| asp_number_of_sites = 0 | Orphaned plan — paying with nothing on it |
| Premium tier with 1 small app | Massive over-provision |
| asp_workers > actual traffic needs | Too many instances |
| PremiumV3 for dev/test | Should be Basic ($13/mo vs $150/mo) |

---

### LOGIC APPS (microsoft.logic/workflows)

**What we fetch:**
- State (Enabled / Disabled)
- Trigger type (Recurrence, HTTP, ServiceBus, etc.)
- Action count (complexity of the workflow)
- Run frequency (calls per day estimate)
- Created and modified time
- SKU/integration account linked
- Diagnostic settings enabled

**What AI looks for:**
| Check | Why it matters |
|---|---|
| State = Disabled | Paying for triggers with no executions |
| No runs for 60+ days | Likely abandoned workflow |
| Recurrence trigger at high frequency | Unnecessary execution cost |
| Integration Account attached but unused | Premium ISE cost for nothing |

---

### APPLICATION GATEWAY (microsoft.network/applicationgateways)

**What we fetch:**
- SKU name (Standard_v2 / WAF_v2)
- SKU tier
- Capacity (fixed instance count)
- Autoscale min/max configured
- WAF enabled and mode (Detection / Prevention)
- WAF rule set version
- Backend pool count and health
- Frontend IP count (public/private)
- SSL policy name and minimum protocol version
- Request routing rule count
- Number of unhealthy backend instances

**What AI looks for:**
| Check | Why it matters |
|---|---|
| agw_autoscale = false (fixed capacity) | Paying for peak capacity 24/7 |
| WAF in Detection mode | WAF is logging but not blocking threats |
| Outdated SSL policy | Allows weak cipher suites |
| No backend pools | App Gateway deployed with nothing behind it |
| Standard v1 SKU (deprecated) | Should migrate to v2 for autoscale + support |
| WAF_v2 with no custom rules | Paying WAF premium with only defaults |

**Real pricing (rules engine):** priced live by tier — Standard_v2 ≈ $152/mo,
WAF_v2 ≈ $273/mo (fixed cost + one baseline capacity unit). v1 SKUs fall back to
a documented list price.

---

### NAT GATEWAY (microsoft.network/natgateways)

**What we fetch:**
- Idle timeout in minutes
- Public IP count attached
- Number of associated subnets
- Public IP prefix attached (if any)

**What AI looks for:**
| Check | Why it matters |
|---|---|
| nat_is_orphaned = true (0 subnets) | NAT Gateway running with nothing routing through it |
| nat_public_ip_count > 2 for small workload | Each public IP costs $3.65/mo |
| Long idle timeout (>30 min) | Holds SNAT ports longer than needed |

**Pricing (rules engine):** documented list-price fallback ≈ $32.85/mo
(the retail API does not expose a clean NAT Gateway meter).

---

### LOAD BALANCERS (microsoft.network/loadbalancers)

**What we fetch:**
- SKU (Basic / Standard)
- Frontend IP configuration count
- Backend address pool count
- Load balancing rule count
- Probe count
- Inbound NAT rule count
- Is unused (no frontend OR no backend)

**What AI looks for:**
| Check | Why it matters |
|---|---|
| lb_is_unused = true | Load Balancer with no traffic — wasted cost |
| Basic SKU | No SLA, no zone redundancy, should be Standard |
| Frontend with no load balancing rules | Misconfigured — traffic not being distributed |
| Single backend instance | No HA benefit, LB overhead with no value |

**Pricing (rules engine):** documented list-price fallback ≈ $18.25/mo for a
Standard LB (the retail API does not expose a clean Load Balancer meter).

---

### RECOVERY SERVICES VAULT / RSV (microsoft.recoveryservices/vaults)

**What we fetch:**
- SKU (Standard / RS0)
- Storage redundancy (LocallyRedundant / GeoRedundant / ZoneRedundant)
- Cross-region restore enabled
- Protected item count (VMs, SQL DBs, Files backed up)
- Storage used in GB
- Backup policy count

**What AI looks for:**
| Check | Why it matters |
|---|---|
| GeoRedundant with cross_region_restore for non-critical workloads | LRS is ~50% cheaper |
| rsv_protected_items = 0 | Empty vault — nothing backed up, delete it |
| Very high storage vs item count | Excessive retention policy — reduce retention days |
| ZoneRedundant for non-prod | Unnecessary cost for dev/test environments |

**Real pricing (rules engine):** cost = protected-instance fees (~$10/instance/mo)
+ backup storage per GB by redundancy. The GRS→LRS saving is computed on the
**storage portion only** (per-instance fees don't change with redundancy).

---

### KEY VAULT (microsoft.keyvault/vaults)

**What we fetch:**
- SKU (Standard / Premium)
- Soft delete retention days
- Purge protection enabled
- Public network access
- Access policy count
- RBAC authorization enabled
- Key count (certificates, secrets, keys stored)
- Key expiry policies

**What AI looks for:**
| Check | Why it matters |
|---|---|
| Public network access = Enabled with no IP restrictions | All IPs can attempt access |
| acl_access_policy_count > 20 | Too many access policies — RBAC model is better |
| Soft delete retention < 7 days | Risk of accidental permanent deletion |
| Purge protection = Disabled | Keys can be permanently deleted immediately |
| Premium SKU with no HSM-backed keys | Premium costs 5× Standard, only needed for HSM |
| No key expiry policies | Keys never rotate — security risk |

---

### AZURE KUBERNETES SERVICE / AKS (microsoft.containerservice/managedclusters)

**What we fetch:**
- Kubernetes version
- All node pools (name, VM size, count, min/max autoscale, mode)
- Total node count
- SKU tier (Free / Standard / Premium)
- RBAC enabled
- Network plugin (kubenet / azure / cilium)
- Add-ons (monitoring, ingress, etc.)

**What AI looks for:**
| Check | Why it matters |
|---|---|
| System pool on expensive VM size | Use Standard_D2s_v3 or D4s_v3 (not D8s+) |
| No autoscale on user pools | Manual scaling wastes money on idle nodes |
| SKU tier = Free for production | No SLA — control plane failures not compensated |
| Outdated Kubernetes version | Security and support risk |
| Oversized node pool for actual pod count | Scale down — you pay per node VM |

---

### POSTGRESQL / MYSQL FLEXIBLE SERVERS

**What we fetch:**
- Compute tier (Burstable / GeneralPurpose / MemoryOptimized)
- VM size (e.g. Standard_D4ds_v4)
- Storage GB
- Backup retention days
- HA mode (Disabled / SameZone / ZoneRedundant)
- Database state (Ready / Stopped)

**What AI looks for:**
| Check | Why it matters |
|---|---|
| GeneralPurpose tier for dev/test | Burstable is 60% cheaper for low workloads |
| HA mode enabled on non-production | HA doubles the compute cost |
| Backup retention > 14 days on dev DB | Excessive retention storage cost |
| State = Stopped | Server paused — still paying for storage |
| 8+ vCores for small database | Right-size to 2 or 4 vCores |

---

### AZURE SQL DATABASE (microsoft.sql/servers/databases)

**What we fetch:**
- SKU (GP_S_Gen5_2, BC_Gen5_4, etc.)
- Tier (GeneralPurpose / BusinessCritical / Hyperscale)
- Max size in GB
- Status (Online / Paused)
- Zone redundant
- Backup storage redundancy (Local / Zone / Geo)

**What AI looks for:**
| Check | Why it matters |
|---|---|
| Status = Paused (serverless) | Autopause is working — but check if needed |
| Zone redundant for non-critical DB | Adds 50% cost for non-production |
| BusinessCritical tier for dev/test | Should be GeneralPurpose |
| Geo-redundant backup for non-prod | Local redundant is 60% cheaper |

---

### REDIS CACHE (microsoft.cache/redis)

**What we fetch:**
- SKU (Basic / Standard / Premium)
- Capacity (size tier 0–6)
- SSL port
- Non-SSL port enabled (security risk)
- Geo-replication linked

**What AI looks for:**
| Check | Why it matters |
|---|---|
| Premium SKU for simple caching | Standard is 50% cheaper with same features |
| Non-SSL port enabled | Data in transit not encrypted |
| Capacity > 1 for small workload | Oversized cache |
| Geo-replication for non-global apps | Adds cost unnecessarily |

---

### API MANAGEMENT (microsoft.apimanagement/service)

**What we fetch:**
- SKU (Developer / Basic / Standard / Premium / Consumption)
- Unit count (scaling units)
- Virtual network type (None / External / Internal)
- Created date
- Gateway URL
- Developer portal enabled

**What AI looks for:**
| Check | Why it matters |
|---|---|
| Premium SKU for internal/dev use | Developer SKU is 80% cheaper |
| Multiple units for low traffic | Scale down to 1 unit |
| Developer SKU in production | No SLA — not suitable for production |
| No custom APIs deployed | APIM running with no APIs = wasted cost |

---

## 5. Excel Report Structure

Each analysis generates a `.xlsx` file with 4 sheets:

| Sheet | Contents |
|---|---|
| **Summary** | Executive overview, total resources, issue counts by severity, estimated savings, MTD spend |
| **Issues** | Every detected issue — resource, severity, category, description, current cost, optimized cost, savings, fix commands |
| **Cost Breakdown** | Month-to-date actual spend per resource sorted highest to lowest |
| **Recommendations** | General improvement recommendations |

---

## 6. Pipeline Execution Modes

| Mode | How to trigger | Output |
|---|---|---|
| **Web UI** | Dashboard → Run Analysis | Real-time progress + Report page |
| **Manual pipeline** | ADO → Run pipeline | Excel artifacts in ADO |
| **Daily scheduled** | Auto at 06:00 UTC | Excel artifacts + DB saved |
| **Cron job (VM)** | Auto at 06:00 UTC | `/home/azureuser/reports/*.xlsx` |
| **CLI** | `python pipeline.py` | Console summary + Excel files |

---

## 7. Secrets and Configuration

| Variable | Purpose |
|---|---|
| `AZURE_TENANT_ID` | Your Azure Active Directory tenant |
| `AZURE_CLIENT_ID` | Service principal app ID |
| `AZURE_CLIENT_SECRET` | Service principal password |
| `AZURE_SUBSCRIPTION_ID` | Default/fallback subscription |
| `OPENAI_API_KEY` | GPT-4o analysis |
| `DATABASE_URL` | PostgreSQL connection string |
| `JWT_SECRET` | Web app authentication |
| `OPENAI_MODEL` | Optional: override AI model (default: gpt-4o) |

---

## 8. For All Subscriptions — SP Permission Required

The SP must have these roles at **Management Group** or per-subscription level:

| Role | Purpose |
|---|---|
| Reader | List subscriptions, RGs, resources |
| Cost Management Reader | Access Azure Cost Management billing data |

```bash
# Grant at Management Group level (covers all subscriptions)
az role assignment create \
  --assignee {AZURE_CLIENT_ID} \
  --role "Reader" \
  --scope /providers/Microsoft.Management/managementGroups/{mg-id}

az role assignment create \
  --assignee {AZURE_CLIENT_ID} \
  --role "Cost Management Reader" \
  --scope /providers/Microsoft.Management/managementGroups/{mg-id}
```
