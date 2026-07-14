"""
ai_analyzer.py
--------------
Sends an Azure resource inventory to OpenAI and returns a structured
cost-optimization analysis.
"""

from __future__ import annotations

import calendar
import json
import os
from datetime import datetime, timezone
from typing import Any

import requests as _requests
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

from rules_engine import (
    detect_issues as _detect_deterministic_issues,
    _vm_prices,
)
from recommendation import canonical_category, group_by_resource

load_dotenv()


class AIAnalyzerError(Exception):
    """Raised when the AI analysis step fails."""


class OpenAIKeyMissingError(AIAnalyzerError):
    pass


# ---------------------------------------------------------------------------
# Azure Retail Prices — used to calculate exact SKU-change savings
# ---------------------------------------------------------------------------

_PRICE_API = "https://prices.azure.com/api/retail/prices"

# Cache prices per session to avoid hammering the API
_price_cache: dict[str, float | None] = {}


def _fetch_sku_hourly_price(service_name: str, sku_name: str, location: str) -> float | None:
    """Fetch the current pay-as-you-go hourly price for a given Azure SKU.
    Returns None on any failure. No auth required (public API)."""
    cache_key = f"{service_name}|{sku_name}|{location}"
    if cache_key in _price_cache:
        return _price_cache[cache_key]

    # Normalise location: "East US" → "eastus"
    arm_region = location.lower().replace(" ", "")

    def _query(sku_field: str) -> float | None:
        """Query the retail API by either skuName or armSkuName and return the
        cheapest non-Spot / non-Windows consumption price, or None."""
        try:
            filt = (
                f"serviceName eq '{service_name}' "
                f"and {sku_field} eq '{sku_name}' "
                f"and armRegionName eq '{arm_region}' "
                f"and priceType eq 'Consumption' "
                f"and unitOfMeasure eq '1 Hour'"
            )
            resp = _requests.get(
                _PRICE_API,
                params={"$filter": filt, "api-version": "2023-01-01-preview"},
                timeout=10,
            )
            if not resp.ok:
                return None
            candidates: list[float] = []
            for it in resp.json().get("Items", []):
                price = float(it.get("retailPrice") or 0)
                if price <= 0:
                    continue
                if "Windows" in (it.get("productName") or ""):
                    continue
                meter = (it.get("meterName") or "").lower()
                if "spot" in meter or "low priority" in meter:
                    continue
                candidates.append(price)
            return min(candidates) if candidates else None
        except Exception:
            return None

    # Prefer skuName; fall back to armSkuName (needed for newer SKU families).
    price = _query("skuName")
    if price is None:
        price = _query("armSkuName")

    _price_cache[cache_key] = price
    return price


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert Azure FinOps consultant and cloud architect.
Your ONLY output must be valid JSON — no markdown, no prose outside the JSON.

PRIME DIRECTIVE — READ THIS FIRST:
Your goal is cost optimisation and security risk detection — nothing else.
Analyse EVERY resource in the inventory. For each one, ask:
  1. Is money being wasted? (wrong tier, idle, orphaned, over-provisioned)
  2. Is there a security risk that could lead to a breach or data loss?
  3. Is there a misconfiguration that increases cost? (no lifecycle policy, HA on dev, etc.)
If YES to any of the above → add it to issues.
If NO to all three → do NOT add it. Leave clean resources out of the report.
Do NOT invent issues. Do NOT add tagging, governance, or Reserved Instance suggestions
just to fill the report. Only report REAL cost waste and REAL security risks.

Schema (follow exactly):
{
  "summary": "<1-3 sentence executive summary>",
  "total_estimated_monthly_savings_usd": <number or null>,
  "issues": [
    {
      "resource_name": "<name>",
      "resource_type": "<Azure resource type>",
      "severity": "high" | "medium" | "low",
      "category": "Over-provisioned" | "Unused / Idle" | "Wrong Pricing Tier" | "Redundancy Config" | "Misconfigured" | "Security Risk" | "Optimization Opportunity",
      "issue": "<what is wrong and why it costs money>",
      "current_monthly_cost_usd": <projected_full_month_usd or null>,
      "optimized_monthly_cost_usd": <cost after applying the fix or null>,
      "estimated_monthly_savings_usd": <current_monthly_cost_usd - optimized_monthly_cost_usd>,
      "savings_reasoning": "<one sentence: current SKU/tier costs X, recommended SKU/tier costs Y, saving = X-Y>",
      "fix_commands": ["<az cli command>"]
    }
  ],
  "general_recommendations": ["<rec>"]
}

Category definitions (pick the BEST match for each issue):
  Over-provisioned      — SKU/tier/size is larger than the workload needs
  Unused / Idle         — resource is stopped, orphaned, deallocated, or abandoned
  Wrong Pricing Tier    — correct service but wrong tier (e.g. Premium when Standard fits)
  Redundancy Config     — HA, geo-redundancy, zone-redundancy enabled unnecessarily
  Misconfigured         — wrong setting that wastes money (backup retention, autoscale off, etc.)
  Security Risk         — open NSG rules, public access, admin credentials exposed
  Optimization Opportunity — reserved instances, spot VMs, autoscale, right-sizing suggestion

Severity:
  high   — wasted spend ≥ 20% of projected cost OR security risk
  medium — moderate savings or best-practice gap
  low    — minor housekeeping / nice-to-have

CRITICAL SAVINGS RULES (never break these):
1. estimated_monthly_savings_usd MUST be < current_monthly_cost_usd.
2. Use projected_full_month_usd as current_monthly_cost_usd. Never use MTD directly.
3. For SKU downgrades: savings = projected_cost × (1 - target_sku_price / current_sku_price).
   Use your knowledge of Azure pricing. Be conservative — use real pricing ratios.
   Example: GP_Standard_D4s_v3 ($0.48/hr) → GP_Standard_D2s_v3 ($0.24/hr) → ratio 0.5 → savings = 50% of projected.
4. For orphaned/idle resources: savings = 100% of projected_full_month_usd.
5. For configuration changes (backup retention, storage tier): savings = realistic %, typically 10-30%.
6. If projected_full_month_usd is null for a resource, use Azure public list pricing.
7. total_estimated_monthly_savings_usd = SUM of all individual savings.

ZERO-COST RESOURCE TYPES — these are FREE in Azure, they have no direct hourly charge:
  - Microsoft.Network/virtualNetworks (VNets)
  - Microsoft.Network/virtualNetworks/subnets (Subnets)
  - Microsoft.Network/networkSecurityGroups (NSGs)
  - Microsoft.Network/routeTables (Route Tables)
  - Microsoft.Network/networkInterfaces (NICs, unless accelerated networking)
  - Microsoft.Resources/resourceGroups
  - Microsoft.Authorization/* (RBAC assignments)
For these types: set current_monthly_cost_usd = 0, optimized_monthly_cost_usd = 0,
estimated_monthly_savings_usd = 0, and explain the issue is a security/hygiene concern,
NOT a cost concern. Do NOT invent a dollar saving for them.
Exception: if an issue involves DELETING a free resource that has a PAID dependency
(e.g. orphaned VNet still holding a paid VPN Gateway or NAT Gateway),
then the saving = the cost of that paid dependency, not the VNet itself.

FIX COMMANDS — MANDATORY RULES:
1. fix_commands MUST NEVER be empty [] for any issue. Every single issue must have at least
   one actionable Azure CLI command.
2. Use the ACTUAL resource name from the JSON "name" field and the resource group from context.
   Do NOT use angle-bracket placeholders like <resource-name> or {rg}. Use the real values.
3. Commands must be complete and copy-paste ready.
4. Only generate fix commands for issues that actually exist in the scanned data.
   Do not fabricate issues or commands for resources not present in the JSON.
5. The az CLI command pattern to use per resource type (substitute real names):
   - VM resize:             az vm resize --resource-group RG --name VMNAME --size <recommended-size>
   - VM deallocate:         az vm deallocate --resource-group RG --name VMNAME
   - Disk delete:           az disk delete --resource-group RG --name DISKNAME --yes
   - Public IP delete:      az network public-ip delete --resource-group RG --name PIPNAME
   - PostgreSQL tier:       az postgres flexible-server update --resource-group RG --name SERVERNAME --tier Burstable --sku-name Standard_B2ms
   - PostgreSQL HA off:     az postgres flexible-server update --resource-group RG --name SERVERNAME --high-availability Disabled
   - PostgreSQL backup:     az postgres flexible-server update --resource-group RG --name SERVERNAME --backup-retention 7
   - MySQL tier:            az mysql flexible-server update --resource-group RG --name SERVERNAME --tier Burstable --sku-name Standard_B2ms
   - MySQL HA off:          az mysql flexible-server update --resource-group RG --name SERVERNAME --high-availability Disabled
   - MySQL backup:          az mysql flexible-server update --resource-group RG --name SERVERNAME --backup-retention 7
   - Redis downgrade:       az redis update --resource-group RG --name CACHENAME --sku Standard --vm-size c1
   - SQL zone redundancy:   az sql db update --resource-group RG --server SERVERNAME --name DBNAME --zone-redundant false
   - Web app HTTPS:         az webapp update --resource-group RG --name APPNAME --https-only true
   - Web app always-on:     az webapp config set --resource-group RG --name APPNAME --always-on true
   - App plan resize:       az appservice plan update --resource-group RG --name PLANNAME --sku B1
   - NSG rule restrict:     az network nsg rule update --resource-group RG --nsg-name NSGNAME --name RULENAME --source-address-prefixes YOUR_IP
   - NSG rule delete:       az network nsg rule delete --resource-group RG --nsg-name NSGNAME --name RULENAME
   - NIC delete:            az network nic delete --resource-group RG --name NICNAME
   - LB delete:             az network lb delete --resource-group RG --name LBNAME
   - NAT GW delete:         az network nat gateway delete --resource-group RG --name NATNAME
   - AGW autoscale:         az network application-gateway update --resource-group RG --name AGWNAME --min-capacity 1 --max-capacity 5
   - AGW WAF prevention:    az network application-gateway waf-config set --resource-group RG --gateway-name AGWNAME --enabled true --firewall-mode Prevention --rule-set-version 3.2
   - Storage public off:    az storage account update --resource-group RG --name SANAME --allow-blob-public-access false
   - Storage TLS:           az storage account update --resource-group RG --name SANAME --min-tls-version TLS1_2
   - Storage network deny:  az storage account update --resource-group RG --name SANAME --default-action Deny
   - KV purge protection:   az keyvault update --resource-group RG --name KVNAME --enable-purge-protection true
   - KV disable public:     az keyvault update --resource-group RG --name KVNAME --public-network-access Disabled
   - KV enable RBAC:        az keyvault update --resource-group RG --name KVNAME --enable-rbac-authorization true
   - AKS autoscale:         az aks nodepool update --resource-group RG --cluster-name AKSNAME --name POOLNAME --enable-cluster-autoscaler --min-count 1 --max-count 5
   - AKS tier upgrade:      az aks update --resource-group RG --name AKSNAME --tier standard
   - APIM scale down:       az apim update --resource-group RG --name APIMNAME --sku-capacity 1
"""


def _build_per_resource_checklist(resources: list[dict]) -> str:
    """
    Build an explicit per-resource checklist so the AI is forced to
    individually analyse every resource instead of skipping quiet ones.
    """
    _CHECKS: dict[str, list[str]] = {
        "microsoft.compute/virtualmachines": [
            "power_state — if not 'VM running' (deallocated/stopped): flag Unused/Idle (OS disk + public IP still billed)",
            "vm_size — if D8s_v3 or larger with no justification: flag Over-provisioned, suggest halving the SKU",
            "direct_public_ip_attached — if true: flag Security Risk (VM NIC has public IP directly, should be behind LB or Bastion)",
            "mgmt_ports_open_to_internet — if contains '22' or '3389' or '*': flag HIGH Security Risk (SSH/RDP open to internet)",
            "os_disk_type — if Premium_SSD for a dev/test VM: flag Wrong Pricing Tier (Standard_SSD is cheaper)",
            "auto_shutdown_enabled — if false on a dev VM: flag Optimization Opportunity (idle nights/weekends cost money)",
        ],
        "microsoft.compute/disks": [
            "disk_state=Unattached / is_orphaned=true — flag Unused/Idle (orphaned disk, delete immediately to stop billing)",
            "disk_sku=Premium_LRS for non-database, non-production workload — flag Wrong Pricing Tier (Standard_SSD is 50% cheaper)",
            "disk_size_gb > 512 for a small VM — flag Over-provisioned (oversized disk wastes money)",
            "disk has no owner VM (managed_by empty) — flag Unused/Idle",
        ],
        "microsoft.network/networkinterfaces": [
            "nic_is_attached — if false: flag Unused/Idle (orphaned NIC)",
            "nic_ip_forwarding — if true without justification: flag Security Risk",
            "nic_has_public_ip — if true: flag Security Risk (direct public IP on NIC)",
            "nic_nsg_attached — if false: flag Misconfigured (no NSG protecting this NIC)",
        ],
        "microsoft.network/publicipaddresses": [
            "is_attached — if false: flag Unused/Idle (paying for reserved IP with nothing using it)",
            "sku_name — if Basic: flag Misconfigured (no SLA, should be Standard)",
            "allocation_method — if Static and is_attached=false: flag Unused/Idle",
        ],
        "microsoft.network/networksecuritygroups": [
            "open_inbound_rules — if any: flag Security Risk (wildcard allow rule)",
            "risky_port_rules — if SSH(22) or RDP(3389) open to internet: flag HIGH Security Risk",
            "is_associated — if false: flag Unused/Idle (NSG not attached to anything)",
        ],
        "microsoft.network/virtualnetworks": [
            "vnet_is_isolated — if true (no peerings, 1 subnet): flag Unused/Idle (abandoned VNet)",
            "subnets without NSG: flag Misconfigured (missing security boundary)",
        ],
        "microsoft.storage/storageaccounts": [
            # Account-level
            "storage_sku Premium_LRS/ZRS for general workload — flag Wrong Pricing Tier (Standard_LRS is 3× cheaper)",
            "access_tier Hot with no lifecycle policy (has_lifecycle_policy=false) — flag Optimization Opportunity (blobs never auto-tiered to Cool/Archive, big cost)",
            "is_empty=true / blob_container_count=0 — flag Unused/Idle (storage account with no blob containers)",
            "https_only=false — flag Security Risk (HTTP traffic allowed)",
            # Security
            "allow_blob_public_access=true — flag HIGH Security Risk (blob data publicly readable by anyone)",
            "network_default_action=Allow — flag Security Risk (storage open to ALL networks, no IP/VNet restriction)",
            "min_tls_version=TLS1_0 or TLS1_1 — flag Security Risk (outdated TLS accepted, upgrade to TLS1_2)",
            "public_network_access=Enabled with no ip/vnet rules — flag Security Risk",
            # Blob service
            "blob_soft_delete_enabled=false — flag Misconfigured (no blob recovery, data loss risk on accidental delete)",
            "blob_versioning_enabled=false for critical data — flag Misconfigured (no version history)",
            # Queue service
            "queue_count > 0 — check: are queues actively used? If storage account is old and unchanged, queues may be idle",
            "queue_count > 0 and no lifecycle/monitoring — flag Optimization Opportunity (enable queue metrics to detect idle queues)",
            # File shares
            "file_share_count > 0 and storage_sku=Premium — Premium file shares charge per provisioned GB not used GB; check file_share_total_quota_gb vs actual usage",
            "file_share_large_quota=true (>1TB provisioned) — flag Over-provisioned (you pay for provisioned quota on Premium shares)",
            "file_share_count > 0 on Standard with no snapshots — flag Misconfigured (no file share backup/snapshot policy)",
            # Tables
            "table_count > 0 on Premium_LRS — flag Wrong Pricing Tier (tables get no benefit from Premium storage, use Standard)",
            # Immutability
            "immutable_storage=true and has_lifecycle_policy=false — flag Misconfigured (WORM blobs cannot be tiered; verify retention period not excessive)",
        ],
        "microsoft.web/sites": [
            "state=Stopped / is_running=false — flag Unused/Idle (paying for App Service Plan with stopped app)",
            "always_on=false on a production web app — flag Misconfigured (cold starts hurt users, costs are same)",
            "https_only=false — flag Security Risk (HTTP traffic not redirected to HTTPS)",
            "min_tls_version=TLS1_0 or TLS1_1 — flag Security Risk",
            "deployment_slot_count > 1 for a dev/test app — flag Over-provisioned (each slot = extra compute)",
            "is_consumption_plan=false for functionapp with low invocations — flag Wrong Pricing Tier (Consumption plan charges per execution, Dedicated plan charges 24/7)",
            "function_language using end-of-support runtime (Python 3.8, Node 14, .NET 5) — flag Security Risk",
            "health_check_path not configured on production — flag Misconfigured (no auto-restart on unhealthy instances)",
        ],
        "microsoft.web/serverfarms": [
            "asp_number_of_sites=0 — flag Unused/Idle (orphaned App Service Plan, delete it — you pay even with no apps)",
            "sku_tier=PremiumV2 or PremiumV3 for 1 small app — flag Over-provisioned (Basic or Standard tier sufficient)",
            "asp_workers > 1 with low traffic — flag Over-provisioned (reduce worker count)",
        ],
        "microsoft.dbforpostgresql/flexibleservers": [
            "compute_tier=GeneralPurpose for dev/test/small workload — flag Wrong Pricing Tier (Burstable is 60% cheaper)",
            "ha_mode=ZoneRedundant or SameZone on non-production — flag Redundancy Config (HA doubles compute cost)",
            "backup_retention_days > 14 for dev DB — flag Misconfigured (7 days is sufficient, extra days = extra storage cost)",
            "db_state=Stopped — flag Unused/Idle (server stopped but storage still billed)",
            "storage_gb much larger than data size — flag Over-provisioned (PostgreSQL bills for provisioned storage)",
            "vm_size has 8+ vCores for small DB — flag Over-provisioned (right-size to 2 or 4 vCores)",
        ],
        "microsoft.dbformysql/flexibleservers": [
            "compute_tier=GeneralPurpose for dev/test — flag Wrong Pricing Tier (Burstable is 60% cheaper)",
            "ha_mode=ZoneRedundant or SameZone on non-production — flag Redundancy Config (doubles cost)",
            "backup_retention_days > 14 for dev DB — flag Misconfigured (reduce to 7)",
            "vm_size has 8+ vCores — flag Over-provisioned",
        ],
        "microsoft.sql/servers/databases": [
            "sql_status=Paused — flag Unused/Idle (check if DB still needed)",
            "zone_redundant=true for non-critical workload — flag Redundancy Config (adds ~50% cost)",
            "sql_tier=BusinessCritical for dev/test — flag Wrong Pricing Tier (GeneralPurpose is 3× cheaper)",
            "backup_storage_redundancy=Geo for non-critical — flag Redundancy Config (Local is 60% cheaper)",
        ],
        "microsoft.cache/redis": [
            "redis_sku_name=Premium for simple key-value caching — flag Wrong Pricing Tier (Standard sufficient, ~50% cheaper)",
            "redis_capacity > 1 for low traffic — flag Over-provisioned (reduce cache size)",
            "non_ssl_port_enabled=true — flag Security Risk (unencrypted data in transit)",
        ],
        "microsoft.documentdb/databaseaccounts": [
            "cosmos_multi_region=true for non-global app — flag Redundancy Config (multi-region writes are very expensive)",
            "multiple regions with writes enabled — flag Redundancy Config (single-region read replica is much cheaper)",
            "serverless=false for unpredictable/low traffic — flag Optimization Opportunity (serverless billing is per RU consumed)",
        ],
        "microsoft.network/applicationgateways": [
            "agw_autoscale=false — flag Misconfigured (fixed capacity = paying for peak 24/7)",
            "agw_waf_mode=Detection — flag Security Risk (WAF not blocking attacks)",
            "agw_ssl_min_protocol TLS1_0/TLS1_1 — flag Security Risk",
            "agw_is_empty=true — flag Unused/Idle (no backends configured)",
        ],
        "microsoft.network/loadbalancers": [
            "lb_is_unused=true — flag Unused/Idle",
            "lb_sku Basic — flag Misconfigured (no SLA, upgrade to Standard)",
        ],
        "microsoft.network/natgateways": [
            "nat_is_orphaned=true — flag Unused/Idle (no subnets attached)",
            "nat_public_ip_count > 2 — flag Over-provisioned",
        ],
        "microsoft.network/virtualnetworkgateways": [
            "vng_is_unused=true — flag Unused/Idle (no connections, significant idle cost)",
            "vng_sku_name Basic — flag Misconfigured (no SLA)",
        ],
        "microsoft.recoveryservices/vaults": [
            "rsv_protected_items=0 — flag Unused/Idle (empty vault)",
            "rsv_redundancy GeoRedundant for non-critical — flag Redundancy Config (use LRS, 50% cheaper)",
        ],
        "microsoft.keyvault/vaults": [
            "kv_public_access=Enabled + kv_network_default_action=Allow — flag Security Risk",
            "kv_purge_protection=false — flag Security Risk (permanent deletion possible)",
            "kv_sku_name premium with no HSM keys — flag Wrong Pricing Tier (use standard)",
            "kv_expiring_certs not empty — flag Misconfigured (certs expiring soon)",
        ],
        "microsoft.containerservice/managedclusters": [
            "aks_sku_tier=Free in production — flag Misconfigured (no SLA, control plane can fail without compensation)",
            "node pool with no autoscale (min_count/max_count not set) — flag Optimization Opportunity (manual scaling wastes money on idle nodes)",
            "node pool vm_size D8s_v3 or larger — flag Over-provisioned (D2s_v3 or D4s_v3 sufficient for most workloads)",
            "total node count much higher than running pods — flag Over-provisioned",
            "kubernetes_version outdated — flag Security Risk (end of support)",
        ],
        "microsoft.apimanagement/service": [
            "apim_is_empty=true (apim_api_count=0) — flag Unused/Idle (APIM running with no APIs, costs $500-3000/mo)",
            "apim_sku_name=Premium with apim_additional_region_count=0 — flag Wrong Pricing Tier (Standard is sufficient for single-region, saves $2000+/mo)",
            "apim_sku_name=Developer in production — flag Misconfigured (no SLA)",
            "apim_sku_capacity > 1 for low traffic — flag Over-provisioned (scale down to 1 unit)",
        ],
        "microsoft.logic/workflows": [
            "logic_is_disabled=true — flag Unused/Idle (disabled workflow still incurs trigger costs)",
            "logic_trigger_type=Recurrence and logic_trigger_frequency=Minute — flag Optimization Opportunity (minute-level recurrence = 43,000+ executions/month, review if needed)",
            "logic_integration_account=true — flag Wrong Pricing Tier (Integration Account adds $300+/mo, verify it is used)",
            "logic_action_count > 20 — flag Optimization Opportunity (complex workflow, consider splitting or caching intermediate results)",
        ],
        "microsoft.containerregistry/registries": [
            "acr_sku=Premium for small team with low image pulls — flag Wrong Pricing Tier (Standard is 3× cheaper)",
            "acr_admin_enabled=true — flag Security Risk (shared credentials, use managed identity instead)",
            "acr_public_access=Enabled — flag Security Risk (registry accessible from all networks)",
        ],
        "microsoft.servicebus/namespaces": [
            "ns_sku_name=Premium for low-throughput messaging — flag Wrong Pricing Tier (Standard is sufficient, 10× cheaper)",
            "ns_capacity > 1 for low message volume — flag Over-provisioned (reduce messaging units)",
        ],
        "microsoft.eventhub/namespaces": [
            "ns_sku_name=Premium for low-throughput streaming — flag Wrong Pricing Tier (Standard sufficient)",
            "ns_capacity > 1 for low event volume — flag Over-provisioned (reduce throughput units)",
        ],
        "microsoft.network/virtualnetworkgateways": [
            "vng_is_unused=true (no connections) — flag Unused/Idle (VPN Gateway with no tunnels, costs $130-700+/mo)",
            "vng_sku_name=Basic — flag Misconfigured (Basic SKU has no SLA and no zone redundancy)",
        ],
        "microsoft.recoveryservices/vaults": [
            "rsv_protected_items=0 — flag Unused/Idle (empty RSV vault, delete it)",
            "rsv_redundancy=GeoRedundant for non-critical workloads — flag Redundancy Config (LocallyRedundant is ~50% cheaper)",
            "cross_region_restore=true for non-critical — flag Redundancy Config (adds cost, only needed for DR requirements)",
            "rsv_storage_used_gb very high relative to rsv_protected_items — flag Misconfigured (overly long retention policy, reduce days)",
        ],
    }

    lines = []
    for i, res in enumerate(resources, 1):
        rtype = res.get("type", "").lower()
        rname = res.get("name", "unknown")
        checks = _CHECKS.get(rtype, ["Check for idle/unused state, security misconfigurations, and over-provisioned SKU"])
        lines.append(f"{i}. {rname} ({res.get('type', rtype)})")
        for chk in checks:
            lines.append(f"   → {chk}")
        lines.append("")
    return "\n".join(lines)


def _build_user_prompt(
    resource_group: str,
    resources: list[dict[str, Any]],
    actual_costs: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Construct the prompt: attach billing data + pre-fetched SKU prices.
    Returns (prompt_string, enriched_resources) so callers can use projected costs."""
    now = datetime.now(timezone.utc)
    day_of_month = now.day
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    remaining_days = days_in_month - day_of_month

    # Build lookup: resource_id (lower) → cost entry
    cost_by_id: dict[str, dict] = {
        c["resource_id"].lower(): c for c in actual_costs if c.get("resource_id")
    }

    has_costs = any(c["cost_usd"] > 0 for c in actual_costs) if actual_costs else False

    enriched = []
    for res in resources:
        entry = dict(res)
        rid = entry.get("id", "").lower()
        cost_info = cost_by_id.get(rid)
        mtd = cost_info["cost_usd"] if cost_info else None
        entry["actual_cost_mtd_usd"] = mtd

        if mtd is not None and day_of_month > 0:
            daily_rate = mtd / day_of_month
            raw_projected = daily_rate * days_in_month
            sku_monthly = entry.get("current_sku_projected_monthly_usd")

            if day_of_month < 7:
                # Too few days — raw extrapolation is wildly inaccurate.
                # Use SKU list price as the cost basis (most reliable anchor).
                if sku_monthly and sku_monthly > 0:
                    projected = sku_monthly
                    entry["projection_capped"] = True
                    entry["projection_cap_reason"] = (
                        f"Only {day_of_month} days elapsed — using SKU list price "
                        f"${sku_monthly:.2f}/mo instead of raw projection "
                        f"${raw_projected:.2f}/mo (unreliable extrapolation)."
                    )
                else:
                    # No SKU price available, use raw but flag it
                    projected = raw_projected
                    entry["projection_capped"] = False
                    entry["projection_cap_reason"] = (
                        f"Only {day_of_month} days elapsed — projection may be inaccurate."
                    )
            elif sku_monthly and sku_monthly > 0 and raw_projected > sku_monthly * 1.2:
                # 7–14 days but still inflated beyond 20% of list price — cap it
                projected = sku_monthly * 1.1
                entry["projection_capped"] = True
                entry["projection_cap_reason"] = (
                    f"Raw projection ${raw_projected:.2f} > SKU list ${sku_monthly:.2f} × 1.2. "
                    f"Capped at ${projected:.2f}."
                )
            else:
                projected = raw_projected
                entry["projection_capped"] = False

            entry["projected_full_month_usd"] = round(projected, 2)
            entry["projection_confidence"] = (
                "low — using SKU list price (< 7 days of data)" if day_of_month < 7
                else "medium (7–14 days)" if day_of_month < 15
                else "high (≥ 15 days)"
            )
        else:
            entry["projected_full_month_usd"] = None
            entry["projection_confidence"] = None

        # Fetch current SKU hourly price from Azure Retail Prices API
        rtype = entry.get("type", "").lower()
        location = entry.get("location", "")
        sku = entry.get("sku") or {}
        sku_name = sku.get("name") or ""

        current_sku_price_per_hr: float | None = None
        if "virtualmachines" in rtype:
            # VMs must be priced by armSkuName (e.g. "Standard_D2ads_v7"); the
            # skuName filter fails for newer families. _vm_prices returns real
            # Linux on-demand + Spot monthly figures and is cached.
            vm_size = entry.get("vm_size") or sku_name
            vp = _vm_prices(vm_size, location)
            if vp.get("ondemand"):
                current_sku_price_per_hr = round(vp["ondemand"] / 730, 4)
                entry["vm_ondemand_monthly_usd"] = vp["ondemand"]
            if vp.get("spot"):
                # Give the AI a real cheaper alternative to reason about.
                entry["vm_spot_monthly_usd"] = vp["spot"]
        elif sku_name and location:
            if "flexibleservers" in rtype and "postgresql" in rtype:
                current_sku_price_per_hr = _fetch_sku_hourly_price("Azure Database for PostgreSQL", sku_name, location)
            elif "flexibleservers" in rtype and "mysql" in rtype:
                current_sku_price_per_hr = _fetch_sku_hourly_price("Azure Database for MySQL", sku_name, location)

        if current_sku_price_per_hr is not None:
            entry["current_sku_price_per_hr_usd"] = current_sku_price_per_hr
            entry["current_sku_projected_monthly_usd"] = round(current_sku_price_per_hr * 730, 2)

        enriched.append(entry)

    def _drop_nulls(obj: Any) -> Any:
        """Recursively remove None values and empty dicts/lists to reduce token count."""
        if isinstance(obj, dict):
            return {k: _drop_nulls(v) for k, v in obj.items()
                    if v is not None and v != {} and v != []}
        if isinstance(obj, list):
            return [_drop_nulls(i) for i in obj if i is not None]
        return obj

    enriched_json = json.dumps(_drop_nulls(enriched), indent=2)

    confidence_note = ""
    if day_of_month < 7:
        confidence_note = (
            f"\nWARNING: Only {day_of_month} days of data. Projections are LOW CONFIDENCE. "
            f"Where projection_capped=true, use current_sku_projected_monthly_usd as cost basis instead."
        )
    elif day_of_month < 15:
        confidence_note = f"\nNOTE: {day_of_month} days of data — medium confidence projections."

    if has_costs:
        billing_ctx = (
            f"Today: day {day_of_month} of {days_in_month} ({now.strftime('%B %Y')}, "
            f"{remaining_days} days remaining).{confidence_note}\n"
            f"Fields per resource:\n"
            f"  actual_cost_mtd_usd               = real spend so far (Azure Cost Management)\n"
            f"  projected_full_month_usd          = best estimate of full-month cost ← USE THIS\n"
            f"  projection_capped                 = true if early-month inflation was corrected\n"
            f"  projection_confidence             = low/medium/high based on days elapsed\n"
            f"  current_sku_price_per_hr_usd      = live Azure Retail hourly price\n"
            f"  current_sku_projected_monthly_usd = SKU list price × 730h (use when projection_capped=true)\n"
        )
    else:
        billing_ctx = (
            "No Cost Management data available. "
            "Use current_sku_projected_monthly_usd (SKU list × 730h) for cost basis, "
            "or Azure public list pricing from training knowledge."
        )

    per_resource_checks = _build_per_resource_checklist(resources)

    prompt = f"""\
Resource group  : {resource_group}
Total resources : {len(resources)}
Billing context :
{billing_ctx}

IMPORTANT — HOW TO CALCULATE SAVINGS (read carefully):
Step 1. Look up the current SKU in Azure pricing (use current_sku_price_per_hr_usd if provided).
Step 2. Look up the recommended/target SKU price.
Step 3. Compute reduction_ratio = (current_price - target_price) / current_price.
Step 4. Apply ratio to ACTUAL projected spend, NOT catalog price:
          current_monthly_cost_usd   = projected_full_month_usd   (from billing data)
          optimized_monthly_cost_usd = projected_full_month_usd × (1 - reduction_ratio)
          estimated_monthly_savings_usd = current - optimized
If projected_full_month_usd is null, use current_sku_projected_monthly_usd or list pricing.
savings_reasoning must state: "Projected $X/mo × Y% reduction = $Z saving".

Resource inventory (JSON):
{enriched_json}

Find ALL of the following issues (cover every resource type present):

COMPUTE
1. Over-provisioned VMs — large SKU with no justification → suggest right-sizing.
2. Deallocated VMs (power_state != "VM running") — still paying for OS disk + public IP.
3. Orphaned disks (is_orphaned = true or disk_state = "Unattached") — zero-use spend.
4. Unattached public IPs (is_attached = false) — paying for reserved IP with no use.
4b. VMs with direct_public_ip_attached = true — Security Risk (VM directly exposed to internet).
4c. VMs with mgmt_ports_open_to_internet containing "22", "3389", or "*" — HIGH Security Risk.

WEB / FUNCTIONS / APP SERVICE
5. Stopped web apps or function apps (state != "Running") — paying for idle plan.
6. App Service Plans with zero apps (asp_number_of_sites = 0) — orphaned plan.
7. Over-provisioned App Service Plans (Premium tier for light traffic) → downgrade tier.
8. App Service Plans with always_on=false on production apps (cold starts).
9. Function apps on Dedicated plan that should move to Consumption plan.

DATABASE / CACHE
10. PostgreSQL/MySQL — GeneralPurpose tier for dev/test → Burstable; excessive backup retention.
11. PostgreSQL/MySQL — HA enabled on non-production servers (doubles cost).
12. SQL Database — paused serverless DB (sql_status = "Paused").
13. SQL Database — zone_redundant=true for non-critical workloads (adds ~50% cost).
14. Redis Cache — Premium SKU for workloads that fit Standard or Basic.
15. Cosmos DB — multi-region writes (cosmos_multi_region=true) for non-global apps.

KUBERNETES / CONTAINERS
16. AKS — system node pool on expensive VM size → suggest Standard_D2s_v3 or similar.
17. AKS — zero autoscale (no min/max_count) on user pools.
18. AKS — aks_sku_tier = "Free" for production (no SLA).
19. Container Registry — Premium ACR for small teams → Standard.

NETWORKING
20. NSGs with open_inbound_rules not empty — wildcard allow rules (security + compliance risk).
    Also flag risky_port_rules where SSH(22) or RDP(3389) is open to Internet.
21. NICs: nic_is_attached=false (orphaned), nic_ip_forwarding=true (security), nic_has_public_ip=true.
22. Load balancers with lb_is_unused=true — no frontend or backend configured.
23. Basic SKU load balancer (lb_sku="Basic") — no SLA, should be Standard.
24. Application Gateway — agw_autoscale=false (fixed capacity), agw_waf_mode="Detection" (not blocking),
    agw_ssl_min_protocol outdated, agw_is_empty=true (no backends).
25. VPN Gateway with vng_is_unused=true — no connections, significant idle cost.
26. NAT Gateway with nat_is_orphaned=true — not associated with any subnet.
27. VNet with vnet_is_isolated=true — no peerings, likely abandoned.

STORAGE ACCOUNTS
28. Premium LRS for workloads that fit Standard GRS — Wrong Pricing Tier.
29. is_empty=true (blob_container_count=0) — storage account with no containers, likely unused.
30. allow_blob_public_access=true OR network_default_action="Allow" — Security Risk.
31. min_tls_version="TLS1_0" or "TLS1_1" — Security Risk, outdated TLS.
32. has_lifecycle_policy=false AND access_tier="Hot" — no auto-tiering to Cool/Archive.
33. blob_soft_delete_enabled=false — data-loss risk.
34. file_share_large_quota=true (>1TB provisioned on Premium) — paying for unused quota.
35. table_count>0 on Premium SKU — tables get no benefit from Premium storage.
36. queue_count>0 on an old unchanged account — possibly idle queue service.

BACKUP / RECOVERY SERVICES VAULT
37. RSV with rsv_protected_items=0 — empty vault, delete it.
38. RSV with rsv_redundancy="GeoRedundant" for non-critical — LocallyRedundant is ~50% cheaper.
39. RSV with very high rsv_storage_used_gb vs rsv_protected_items — overly aggressive retention.

SERVICE BUS / EVENT HUBS
40. Premium tier for low-throughput workloads → Standard.

KEY VAULT
41. kv_public_access="Enabled" AND kv_network_default_action="Allow" — Security Risk.
42. kv_purge_protection=false — permanent deletion possible immediately.
43. kv_sku_name="premium" with kv_key_count=0 — paying 5× for no HSM keys.
44. kv_expiring_certs not empty — certificates expiring within 60 days.
45. kv_wide_permissions=true — access policy grants "all" or "purge" (too broad).

API MANAGEMENT
46. apim_is_empty=true — APIM with no APIs deployed (costs $500-3000/mo).
47. apim_sku_name="Premium" with no multi-region — Standard is sufficient.
48. apim_sku_capacity > 1 for low traffic — scale down units.

LOGIC APPS
49. logic_is_disabled=true — disabled workflow, consider deleting.
50. logic_trigger_frequency="Minute" — minute-level recurrence = 43,000+ runs/month.
51. logic_integration_account=true — Integration Account adds $300+/mo, verify usage.

GENERAL
52. Idle/abandoned resources — created_time > 90 days ago, changed_time > 60 days.

MANDATORY PROCESS — follow this exactly, do not skip steps:
1. Go through EVERY resource in the JSON list one by one (all {len(resources)} of them).
2. For each resource, check it against ALL detection rules above.
3. If a resource has ANY issue, add it to the issues array.
4. Only omit a resource from issues if it is 100% correctly configured with zero concerns.
5. Do NOT report a resource as an issue just to acknowledge it is fine. If a resource is
   already optimally configured (e.g. a Burstable DB with sensible retention), DO NOT create
   an issue for it — leave it out entirely. Never write "which is optimal" as an issue.
6. Do NOT stop early. Do NOT summarise groups of resources together.
7. fix_commands MUST NEVER be empty. Use the ACTUAL resource name and resource group.

FINAL VERIFICATION — confirm you checked each of these {len(resources)} resources:
{per_resource_checks}
"""
    return prompt, enriched


# ---------------------------------------------------------------------------
# OpenAI call
# ---------------------------------------------------------------------------

def _rebase_savings_on_actual_cost(
    issues: list[dict],
    enriched_resources: list[dict],
) -> None:
    """
    The AI calculates savings ratios from Azure catalog prices, which may differ
    from what you actually pay (discounts, commitments, partial month, etc.).

    This function:
    1. Builds a name→projected_full_month_usd lookup from the enriched resources.
    2. For each issue, extracts the AI's price-reduction RATIO
       (ratio = 1 - optimized_catalog / current_catalog).
    3. Rebases savings onto the resource's actual projected cost:
         actual_current   = projected_full_month_usd
         actual_optimized = actual_current * (1 - ratio)
         actual_savings   = actual_current - actual_optimized
    4. Guarantees savings < actual_current (hard cap at 95%).
    """
    # Build lookup: resource name (lower) → projected_full_month_usd
    projected_by_name: dict[str, float] = {}
    for r in enriched_resources:
        name = r.get("name", "").lower()
        proj = r.get("projected_full_month_usd")
        if proj is not None and proj > 0:
            projected_by_name[name] = proj

    for issue in issues:
        rname = issue.get("resource_name", "").lower()
        projected = projected_by_name.get(rname)
        if projected is None or projected <= 0:
            continue  # No real billing data → leave AI's estimate as-is

        ai_current = issue.get("current_monthly_cost_usd")
        ai_optimized = issue.get("optimized_monthly_cost_usd")

        # Derive the reduction ratio the AI used from catalog prices
        if (
            ai_current is not None
            and ai_optimized is not None
            and ai_current > 0
            and ai_optimized >= 0
        ):
            ratio = 1.0 - (ai_optimized / ai_current)
            ratio = max(0.0, min(ratio, 0.95))   # clamp to [0, 95%]
        elif issue.get("estimated_monthly_savings_usd") and ai_current and ai_current > 0:
            # AI gave savings but not the split — derive ratio from savings/current
            ratio = min(issue["estimated_monthly_savings_usd"] / ai_current, 0.95)
        else:
            # AI had no cost data at all — don't touch
            continue

        # Rebase onto actual projected spend
        actual_current   = projected
        actual_optimized = round(actual_current * (1.0 - ratio), 2)
        actual_savings   = round(actual_current - actual_optimized, 2)

        old_reasoning = issue.get("savings_reasoning") or ""
        issue["current_monthly_cost_usd"]    = actual_current
        issue["optimized_monthly_cost_usd"]  = actual_optimized
        issue["estimated_monthly_savings_usd"] = actual_savings
        issue["savings_reasoning"] = (
            f"Actual projected spend ${actual_current:.2f}/mo × "
            f"{ratio*100:.0f}% reduction (from SKU pricing ratio) = "
            f"${actual_savings:.2f}/mo saving. "
            + old_reasoning
        )


def analyze_resources(
    resource_group: str,
    resources: list[dict[str, Any]],
    actual_costs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Send the resource inventory to OpenAI gpt-4o and return the parsed
    cost-analysis JSON.

    Parameters
    ----------
    resource_group : str
        Name of the Azure resource group being analysed.
    resources : list[dict]
        The list of normalised resource objects from azure_scanner.

    Returns
    -------
    dict
        Parsed analysis matching the schema defined in _SYSTEM_PROMPT.

    Raises
    ------
    OpenAIKeyMissingError
        If OPENAI_API_KEY is not set.
    AIAnalyzerError
        For any other OpenAI or parsing failure.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIKeyMissingError(
            "OPENAI_API_KEY environment variable is not set. "
            "Add it to a .env file or export it before starting the server."
        )

    # Model is configurable — set OPENAI_MODEL env var to switch without redeploying.
    # Recommended options (as of mid-2026):
    #   gpt-4o          — default, fast, good quality
    #   gpt-4.1         — better reasoning, 1M token context (handles large RG scans)
    #   gpt-4.1-mini    — cheaper, slightly less thorough
    #   o3-mini         — strong reasoning, slower
    model = os.getenv("OPENAI_MODEL", "gpt-4o").strip()

    client = OpenAI(api_key=api_key)

    costs = actual_costs or []

    # Sort resources by name before sending to GPT so the input is always in the
    # same order regardless of what order Azure's API returned them.
    # Consistent input → consistent AI output across runs.
    sorted_resources = sorted(resources, key=lambda r: (r.get("type", ""), r.get("name", "")))

    user_prompt, enriched_resources = _build_user_prompt(resource_group, sorted_resources, costs)

    # Estimate prompt size and warn if close to gpt-4o's 128K limit
    prompt_tokens_approx = (len(_SYSTEM_PROMPT) + len(user_prompt)) // 4
    if prompt_tokens_approx > 100_000 and model == "gpt-4o":
        model = "gpt-4.1"   # auto-upgrade to 1M context model for large scans

    # o-series models (o1, o3, o4) don't support temperature/seed/response_format
    is_o_series = model.startswith("o") and not model.startswith("gpt")

    call_kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    if not is_o_series:
        call_kwargs["temperature"] = 0       # deterministic — no randomness
        call_kwargs["top_p"] = 1             # combined with temp=0, maximises consistency
        call_kwargs["seed"] = 42             # same seed = same internal state across runs
        call_kwargs["response_format"] = {"type": "json_object"}

    try:
        response = client.chat.completions.create(**call_kwargs)
    except OpenAIError as exc:
        raise AIAnalyzerError(f"OpenAI API call failed: {exc}") from exc

    raw_content = response.choices[0].message.content or ""

    try:
        analysis = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise AIAnalyzerError(
            f"OpenAI returned non-JSON content: {raw_content[:300]}"
        ) from exc

    # Ensure expected top-level keys exist so callers can rely on the schema
    analysis.setdefault("summary", "No summary provided.")
    analysis.setdefault("total_estimated_monthly_savings_usd", None)
    analysis.setdefault("issues", [])
    analysis.setdefault("general_recommendations", [])

    issues = analysis.get("issues", [])
    n_resources = len(resources)

    # ── Deterministic rules engine ───────────────────────────────────────────
    # Run the hand-written rule engine over the SAME enriched resource data the
    # AI saw. These findings are guaranteed and reproducible. Merge them with the
    # AI's findings, de-duplicating on (resource_name, category) so we don't show
    # the same problem twice. Rule-engine findings are authoritative for cost math.
    deterministic = _detect_deterministic_issues(enriched_resources)

    def _key(i: dict) -> tuple[str, str]:
        # Canonicalise the category so an AI "Over-provisioned" dedups against a
        # deterministic "Overprovisioned" for the same resource.
        return (
            (i.get("resource_name") or "").lower(),
            canonical_category(i.get("category")).lower(),
        )

    det_keys = {_key(d) for d in deterministic}
    # Keep AI issues that the rules engine did NOT already cover, and normalise
    # their category to the canonical set so downstream logic is consistent.
    ai_only = []
    for i in issues:
        if _key(i) in det_keys:
            continue
        i["category"] = canonical_category(i.get("category"))
        ai_only.append(i)
    # Deterministic findings first (reliable), then AI's extra nuanced findings
    issues = deterministic + ai_only
    analysis["issues"] = issues

    # Sanity check: if the combined result found 0-1 issues for 5+ resources, warn.
    if n_resources >= 5 and len(issues) <= 1:
        analysis["summary"] = (
            analysis["summary"]
            + " ⚠️ Warning: analysis may be incomplete — "
            "fewer issues than expected were found. Try re-running the analysis."
        )

    _FREE_TYPES = {
        "microsoft.network/virtualnetworks",
        "microsoft.network/networksecuritygroups",
        "microsoft.network/routetables",
        "microsoft.network/networkinterfaces",
        "microsoft.resources/resourcegroups",
    }

    for issue in issues:
        rtype = issue.get("resource_type", "").lower()

        # Zero out savings for inherently free resource types
        if any(rtype.startswith(ft) for ft in _FREE_TYPES):
            issue["current_monthly_cost_usd"] = 0
            issue["optimized_monthly_cost_usd"] = 0
            issue["estimated_monthly_savings_usd"] = 0
            if not issue.get("savings_reasoning"):
                issue["savings_reasoning"] = (
                    "This resource type has no direct Azure billing charge. "
                    "Issue is a security or hygiene concern."
                )

        # Safety cap: savings cannot exceed projected cost for any issue
        current = issue.get("current_monthly_cost_usd")
        savings = issue.get("estimated_monthly_savings_usd")
        if current is not None and savings is not None and current > 0 and savings > current:
            issue["estimated_monthly_savings_usd"] = round(current * 0.9, 2)
            issue["savings_reasoning"] = (
                (issue.get("savings_reasoning") or "")
                + " [savings capped at 90% of current cost]"
            )

    # Rebase AI's savings onto actual projected spend (fixes catalog-price inflation)
    _rebase_savings_on_actual_cost(issues, enriched_resources)

    # Fill in missing savings for issues where AI left estimated_monthly_savings_usd = null.
    # Use conservative category-based ratios (keyed by the canonical category)
    # applied to current_monthly_cost_usd.
    _CATEGORY_RATIOS: dict[str, float] = {
        "overprovisioned":   0.40,  # right-sizing
        "idle":              1.00,  # resource can be deleted
        "cost saving":       0.30,  # tier / commitment change
        "underprovisioned":  0.00,  # perf fix, not a cost saving
        "performance":       0.20,  # config fix
        "security":          0.00,  # no direct savings
    }

    for issue in issues:
        if issue.get("estimated_monthly_savings_usd") is not None:
            continue  # Already set — don't touch

        current = issue.get("current_monthly_cost_usd")
        category = canonical_category(issue.get("category")).lower()
        ratio = _CATEGORY_RATIOS.get(category, 0.25)

        if current and current > 0:
            savings = round(current * ratio, 2)
            issue["estimated_monthly_savings_usd"] = savings
            issue["optimized_monthly_cost_usd"] = round(current * (1 - ratio), 2)
            issue["savings_reasoning"] = (
                issue.get("savings_reasoning") or
                f"Estimated {int(ratio*100)}% reduction based on issue category "
                f"'{issue.get('category', 'unknown')}' applied to ${current:.2f}/mo current cost."
            )
        else:
            # No cost data at all — mark as negligible
            issue["estimated_monthly_savings_usd"] = 0
            issue["savings_reasoning"] = (
                issue.get("savings_reasoning") or
                "No billing data available for this resource — savings not quantifiable."
            )

    # ── Noise filter ─────────────────────────────────────────────────────────
    # Drop "issues" that are not actually actionable. A pure COST-optimisation
    # finding with $0 estimated savings is just noise (e.g. a correctly-configured
    # resource reported as "already optimal"). Risk/hygiene/governance findings
    # are kept even at $0 because they reduce RISK, not cost.
    _RISK_CATEGORIES = {"security", "performance", "idle"}
    cleaned = []
    for i in issues:
        cat = canonical_category(i.get("category")).lower()
        savings = i.get("estimated_monthly_savings_usd") or 0
        if cat in _RISK_CATEGORIES or i.get("_keep_at_zero"):
            cleaned.append(i)          # always keep risk / governance findings
        elif savings > 0:
            cleaned.append(i)          # keep cost findings that actually save money
        # else: cost-category finding with $0 savings → drop as noise
    issues = cleaned

    # ── Rank the merged list by risk-adjusted priority ────────────────────────
    issues.sort(
        key=lambda x: (x.get("_priority_score", 0), x.get("estimated_monthly_savings_usd") or 0),
        reverse=True,
    )

    # ── Total savings — de-duplicate mutually-exclusive alternatives ──────────
    # A resource can carry several alternative recommendations in one exclusive
    # group (e.g. VM: downsize vs B-series vs Reserved Instance). You apply ONE,
    # so the total counts only the best option per (resource, group). The others
    # are flagged is_alternative so the UI can present them as choices.
    group_best: dict[tuple, float] = {}
    for i in issues:
        grp = i.get("_exclusive_group")
        if not grp:
            continue
        key = ((i.get("resource_name") or "").lower(), grp)
        group_best[key] = max(group_best.get(key, 0.0), i.get("estimated_monthly_savings_usd") or 0)

    seen_group_primary: set[tuple] = set()
    total = 0.0
    for i in issues:
        sav = i.get("estimated_monthly_savings_usd") or 0
        grp = i.get("_exclusive_group")
        if not grp:
            total += sav
            continue
        key = ((i.get("resource_name") or "").lower(), grp)
        if sav == group_best[key] and key not in seen_group_primary:
            seen_group_primary.add(key)
            i["is_alternative"] = False
            total += sav                # count only the best option once
        else:
            i["is_alternative"] = True  # alternative choice — not summed

    analysis["issues"] = issues
    analysis["total_estimated_monthly_savings_usd"] = round(total, 2) if total else 0

    # ── Additive per-resource view (highest-impact first) ─────────────────────
    analysis["resource_recommendations"] = group_by_resource(issues)
    analysis["recommendation_engine"] = {
        "version": 2,
        "deterministic_count": len(deterministic),
        "ai_supplemented_count": len(ai_only),
        "resources_analyzed": n_resources,
    }

    # Strip internal bookkeeping fields before returning to the client
    for i in issues:
        for f in ("_source", "_priority_score", "_exclusive_group", "_keep_at_zero"):
            i.pop(f, None)

    return analysis
