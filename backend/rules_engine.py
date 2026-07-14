"""
rules_engine.py
---------------
Deterministic, rule-based cost & security detection.

Why this exists
---------------
Relying only on the AI (LLM) to find issues produces inconsistent results:
the same resources can yield different findings on different runs, and the
model sometimes silently skips resources. For a client-facing product we need
GUARANTEED, reproducible findings.

This engine runs a fixed set of hand-written rules directly against the
enriched resource data from azure_scanner. Every rule is deterministic:
same input → same output, every single time. The AI layer then adds
narrative polish and catches anything nuanced, but the baseline findings
here are always present.

Each rule returns an "issue" dict matching the schema used by ai_analyzer,
so the two sources can be merged seamlessly.
"""

from __future__ import annotations

import re
import requests as _requests
from typing import Any, Callable

import sku_advisor
from recommendation import (
    canonical_category,
    doc_url as _doc_url,
    infer_production,
    is_nonproduction,
    resource_age_days,
    priority_label,
    priority_score,
    rank_and_dedup,
    score_confidence,
    score_risk,
)

# ---------------------------------------------------------------------------
# Savings ratios by remediation type (conservative, defensible estimates)
# ---------------------------------------------------------------------------

_IDLE_DELETE_RATIO = 1.00      # resource can be deleted entirely
_TIER_DOWNGRADE_RATIO = 0.50   # e.g. GeneralPurpose → Burstable, Premium → Standard
_HA_DISABLE_RATIO = 0.50       # HA doubles cost, disabling halves it
_REDUNDANCY_RATIO = 0.45       # Geo → Local redundancy
_RIGHTSIZE_RATIO = 0.50        # halving a VM/DB size
_LIFECYCLE_RATIO = 0.30        # blob tiering to Cool/Archive
_MINOR_CONFIG_RATIO = 0.15     # small config tweaks


# Rough Azure list prices (USD/GB/month) for managed disks — used ONLY as a
# fallback when no live billing/retail data is available, so cost-saving
# estimates are never silently $0 for known paid resources.
_DISK_GB_RATE = {
    "premium": 0.15,       # Premium SSD (~$0.15/GB/mo)
    "standardssd": 0.075,  # Standard SSD
    "standard": 0.045,     # Standard HDD
    "ultra": 0.30,         # Ultra Disk (approx)
}


# ---------------------------------------------------------------------------
# Live VM pricing (public retail API, no auth required)
# ---------------------------------------------------------------------------
# We key on armSkuName (e.g. "Standard_D2ads_v7") which is the RELIABLE match
# for compute SKUs. The older skuName-based lookup fails for newer families.
_VM_PRICE_API = "https://prices.azure.com/api/retail/prices"
_vm_price_cache: dict[str, dict] = {}

# Reserved Instance / Savings Plan cut (1-year, conservative — real world 30-40%)
_RESERVED_RATIO = 0.37
# Moving a general-purpose/compute VM down to a Burstable B-series (typical)
_VM_RIGHTSIZE_RATIO = 0.40


def _vm_prices(vm_size: str | None, location: str | None) -> dict:
    """Return {'ondemand': $/mo, 'spot': $/mo} for a VM SKU via the public retail API.

    Uses armSkuName + armRegionName, filters out Windows license-included and
    Low Priority meters, and takes the cheapest (Linux) price in each class.
    Result is monthly assuming 24/7 (730 h). Returns {} on any failure.
    """
    if not vm_size or not location:
        return {}
    key = f"{vm_size}|{location}"
    if key in _vm_price_cache:
        return _vm_price_cache[key]
    result: dict = {}
    try:
        region = location.lower().replace(" ", "")
        filt = (
            f"armSkuName eq '{vm_size}' and armRegionName eq '{region}' "
            f"and priceType eq 'Consumption'"
        )
        resp = _requests.get(
            _VM_PRICE_API,
            params={"currencyCode": "USD", "$filter": filt},
            timeout=12,
        )
        if resp.ok:
            ondemand: list[float] = []
            spot: list[float] = []
            for it in resp.json().get("Items", []):
                price = float(it.get("retailPrice") or 0)
                if price <= 0:
                    continue
                if "Windows" in (it.get("productName") or ""):
                    continue  # skip Windows license-included pricing
                meter = (it.get("meterName") or "").lower()
                if "spot" in meter:
                    spot.append(price)
                elif "low priority" in meter:
                    continue
                else:
                    ondemand.append(price)
            if ondemand:
                result["ondemand"] = round(min(ondemand) * 730, 2)
            if spot:
                result["spot"] = round(min(spot) * 730, 2)
    except Exception:
        pass
    _vm_price_cache[key] = result
    return result


def _is_burstable(vm_size: str | None) -> bool:
    """B-series VMs are already the cheapest burstable tier."""
    return bool(vm_size) and vm_size.lower().startswith("standard_b")


# ---------------------------------------------------------------------------
# Evidence-based right-sizing: gate downsize/tier advice on real CPU usage
# ---------------------------------------------------------------------------
_CPU_LOW_AVG = 15.0   # average CPU below this over the window = under-used
_CPU_LOW_MAX = 40.0   # and peak below this = safe to downsize


def _cpu_underused(res: dict) -> bool | None:
    """True  = measured CPU is low → safe to downsize.
    False = busy enough to justify the current size → do NOT downsize.
    None  = no metric data available (brand-new resource, etc.)."""
    avg = res.get("cpu_avg_pct")
    mx = res.get("cpu_max_pct")
    if avg is None and mx is None:
        return None
    if avg is not None and avg < _CPU_LOW_AVG and (mx is None or mx < _CPU_LOW_MAX):
        return True
    return False


def _cpu_text(res: dict) -> str:
    """Human-readable CPU evidence for the reasoning field (empty if no data)."""
    avg = res.get("cpu_avg_pct")
    mx = res.get("cpu_max_pct")
    parts = []
    if avg is not None:
        parts.append(f"avg {avg:.0f}%")
    if mx is not None:
        parts.append(f"peak {mx:.0f}%")
    return f" Measured CPU {', '.join(parts)} over ~14 days." if parts else ""


# ---------------------------------------------------------------------------
# Generic live retail pricing for non-VM resource types
# ---------------------------------------------------------------------------
_retail_cache: dict[str, Any] = {}


def _region(res: dict) -> str | None:
    loc = (res.get("location") or "").lower().replace(" ", "")
    return loc or None


def _retail_items(filt: str) -> list[dict]:
    """Run an arbitrary $filter against the public retail price API."""
    try:
        resp = _requests.get(
            _VM_PRICE_API,
            params={"currencyCode": "USD", "$filter": filt},
            timeout=12,
        )
        if resp.ok:
            return resp.json().get("Items", [])
    except Exception:
        pass
    return []


# Managed-disk size → performance-tier number (first tier that fits the size).
# Same size breakpoints apply to Premium (P), Standard SSD (E) and Standard HDD (S).
_DISK_TIER_SIZES = [
    (4, 1), (8, 2), (16, 3), (32, 4), (64, 6), (128, 10), (256, 15),
    (512, 20), (1024, 30), (2048, 40), (4096, 50), (8192, 60),
    (16384, 70), (32767, 80),
]


def _disk_family_prefix(sku: str | None) -> str | None:
    s = (sku or "").lower()
    if "premium" in s:
        return "P"
    if "standardssd" in s:
        return "E"
    if s.startswith("standard"):
        return "S"
    return None  # UltraSSD is priced per-IOPS/throughput, not per-tier


def _disk_tier_num(size_gb: float) -> int:
    for cap, num in _DISK_TIER_SIZES:
        if size_gb <= cap:
            return num
    return 80


def _disk_price_monthly(size_gb: float | None, region: str | None, family_prefix: str) -> float | None:
    """Real fixed monthly price of a managed disk of the given size + family."""
    if not size_gb or not region:
        return None
    sku = f"{family_prefix}{_disk_tier_num(size_gb)} LRS"
    key = f"disk|{sku}|{region}"
    if key in _retail_cache:
        return _retail_cache[key]
    price = None
    for it in _retail_items(
        f"serviceName eq 'Storage' and armRegionName eq '{region}' "
        f"and skuName eq '{sku}' and priceType eq 'Consumption'"
    ):
        if "Managed Disks" not in (it.get("productName") or ""):
            continue
        if it.get("unitOfMeasure") != "1/Month":
            continue
        meter = it.get("meterName") or ""
        if not meter.endswith("Disk"):  # skip 'Disk Mount' / 'Disk Operations'
            continue
        price = round(float(it.get("retailPrice") or 0), 2)
        break
    _retail_cache[key] = price
    return price


def _public_ip_monthly(res: dict) -> float | None:
    """Real monthly price of a Public IP address (Standard/Basic, Static/Dynamic)."""
    region = _region(res)
    if not region:
        return None
    sku = res.get("sku_name") or "Standard"
    alloc = (res.get("allocation_method") or "Static")
    want = "Static" if "static" in alloc.lower() else "Dynamic"
    key = f"pip|{sku}|{want}|{region}"
    if key in _retail_cache:
        return _retail_cache[key]
    price = None
    for it in _retail_items(
        f"serviceName eq 'Virtual Network' and armRegionName eq '{region}' "
        f"and priceType eq 'Consumption' and productName eq 'IP Addresses'"
    ):
        if (it.get("skuName") or "") != sku:
            continue
        meter = it.get("meterName") or ""
        if want not in meter or "Public IP" not in meter:
            continue
        if it.get("unitOfMeasure") != "1 Hour":
            continue
        price = round(float(it.get("retailPrice") or 0) * 730, 2)
        break
    _retail_cache[key] = price
    return price


def _app_gateway_monthly(res: dict) -> float | None:
    """Real base monthly price of an Application Gateway v2 (fixed cost + 1 capacity unit)."""
    region = _region(res)
    if not region:
        return None
    tier = (res.get("agw_tier") or res.get("agw_sku_name") or "").lower()
    if "waf" in tier and "v2" in tier:
        product = "Application Gateway WAF v2"
    elif "standard" in tier and "v2" in tier:
        product = "Application Gateway Standard v2"
    elif "basic" in tier:
        product = "Application Gateway Basic v2"
    else:
        return None  # v1 SKUs → caller uses documented fallback
    key = f"agw|{product}|{region}"
    if key in _retail_cache:
        return _retail_cache[key]
    fixed = cu = None
    for it in _retail_items(
        f"serviceName eq 'Application Gateway' and armRegionName eq '{region}' "
        f"and priceType eq 'Consumption'"
    ):
        if (it.get("productName") or "") != product:
            continue
        meter = it.get("meterName") or ""
        if meter.endswith("Fixed Cost"):
            fixed = float(it.get("retailPrice") or 0)
        elif meter.endswith("Capacity Units"):
            cu = float(it.get("retailPrice") or 0)
    price = round((fixed + (cu or 0)) * 730, 2) if fixed is not None else None
    _retail_cache[key] = price
    return price


def _vpn_gateway_monthly(res: dict) -> float | None:
    """Real monthly price of a VPN Gateway by SKU (VpnGw1..5 / Basic / AZ variants)."""
    if (res.get("vng_gateway_type") or "").lower() == "expressroute":
        return None  # ExpressRoute gateways are priced differently
    region = _region(res)
    sku = res.get("vng_sku_name")
    if not region or not sku:
        return None
    key = f"vng|{sku}|{region}"
    if key in _retail_cache:
        return _retail_cache[key]
    price = None
    for it in _retail_items(
        f"serviceName eq 'VPN Gateway' and armRegionName eq '{region}' "
        f"and skuName eq '{sku}' and priceType eq 'Consumption'"
    ):
        # The gateway-hour meter's name matches the SKU name (e.g. 'VpnGw1').
        if (it.get("meterName") or "") == sku and it.get("unitOfMeasure") == "1 Hour":
            price = round(float(it.get("retailPrice") or 0) * 730, 2)
            break
    _retail_cache[key] = price
    return price


def _app_service_plan_monthly(res: dict) -> float | None:
    """Real monthly price of an App Service Plan = per-instance hourly × workers × 730.

    The retail catalogue lists the v2/v3 SKUs with a space ("P1v3" → "P1 v3")
    and splits Linux vs Windows into separate products, so we normalise both.
    Free/Shared tiers are effectively no-cost.
    """
    region = _region(res)
    raw = res.get("asp_sku_name")
    if not region or not raw:
        return None
    tier = (res.get("asp_sku_tier") or "").lower()
    if tier in ("free", "shared") or raw.upper() in ("F1", "D1"):
        return 0.0
    sku = re.sub(r"(v[23])$", r" \1", raw)  # "P1v3" -> "P1 v3"
    workers = res.get("asp_workers") or 1
    is_linux = (res.get("asp_os") or "").lower() == "linux"
    key = f"asp|{sku}|{is_linux}|{region}"
    if key in _retail_cache:
        base = _retail_cache[key]
    else:
        base = None
        for it in _retail_items(
            f"serviceName eq 'Azure App Service' and armRegionName eq '{region}' "
            f"and priceType eq 'Consumption' and skuName eq '{sku}'"
        ):
            if it.get("unitOfMeasure") != "1 Hour":
                continue
            prod_linux = "Linux" in (it.get("productName") or "")
            if prod_linux != is_linux:
                continue
            base = float(it.get("retailPrice") or 0)
            break
        _retail_cache[key] = base
    if base is None:
        return None
    return round(base * (workers or 1) * 730, 2)


# Container Registry: fixed registry-unit fee per tier (USD/month, ~30.44 days).
# Verified against the retail API (Basic $0.1666/day, Standard $0.6666/day,
# Premium $1.6666/day). Storage/build minutes are extra and usage-based.
_ACR_MONTHLY_FALLBACK: dict[str, float] = {"basic": 5.07, "standard": 20.29, "premium": 50.73}


def _acr_monthly(res: dict) -> float | None:
    """Real monthly registry-unit fee for a Container Registry by tier."""
    sku = (res.get("acr_sku") or "").lower()
    if sku not in _ACR_MONTHLY_FALLBACK:
        return None
    region = _region(res)
    if region:
        key = f"acr|{sku}|{region}"
        if key in _retail_cache:
            live = _retail_cache[key]
        else:
            live = None
            for it in _retail_items(
                f"serviceName eq 'Container Registry' and armRegionName eq '{region}' "
                f"and skuName eq '{sku.capitalize()}' and priceType eq 'Consumption'"
            ):
                if (it.get("meterName") or "").endswith("Registry Unit") and it.get("unitOfMeasure") == "1/Day":
                    live = round(float(it.get("retailPrice") or 0) * 30.44, 2)
                    break
            _retail_cache[key] = live
        if live:
            return live
    return _ACR_MONTHLY_FALLBACK[sku]


# Service Bus / Event Hubs Premium: fixed per-unit hourly fee (USD/month, ×730).
# Verified: Service Bus Premium Messaging Unit $0.9275/hr; Event Hubs Premium
# Processing Unit $1.027/hr. Basic/Standard tiers are cheap usage-based (no floor).
_NS_PREMIUM_UNIT_FALLBACK = {"servicebus": 677.08, "eventhub": 749.71}


def _namespace_monthly(res: dict) -> float | None:
    """Real monthly fixed fee for a Premium Service Bus / Event Hubs namespace."""
    if (res.get("ns_sku_name") or "").lower() != "premium":
        return None  # Basic/Standard have no meaningful fixed floor
    is_eh = "eventhub" in (res.get("type") or "").lower()
    cap = res.get("ns_capacity") or 1
    per_unit = None
    region = _region(res)
    if region:
        svc = "Event Hubs" if is_eh else "Service Bus"
        needle = "Premium Processing Unit" if is_eh else "Premium Messaging Unit"
        key = f"ns|{svc}|{region}"
        if key in _retail_cache:
            per_unit = _retail_cache[key]
        else:
            for it in _retail_items(
                f"serviceName eq '{svc}' and armRegionName eq '{region}' and priceType eq 'Consumption'"
            ):
                if (it.get("meterName") or "") == needle and (it.get("unitOfMeasure") or "").endswith("Hour"):
                    per_unit = round(float(it.get("retailPrice") or 0) * 730, 2)
                    break
            _retail_cache[key] = per_unit
    if per_unit is None:
        per_unit = _NS_PREMIUM_UNIT_FALLBACK["eventhub" if is_eh else "servicebus"]
    return round(per_unit * (cap or 1), 2)


def _aks_node_monthly(res: dict) -> float | None:
    """Estimate an AKS cluster's node-compute cost by pricing each node pool's
    VMs (Spot rate for Spot pools, on-demand otherwise) via the retail API."""
    pools = res.get("aks_node_pools") or []
    loc = res.get("location")
    total = 0.0
    for p in pools:
        vm, cnt = p.get("vm_size"), p.get("count") or 0
        if not vm or not cnt:
            continue
        prices = _vm_prices(vm, loc)
        rate = prices.get("spot") if p.get("spot") else prices.get("ondemand")
        if rate:
            total += rate * cnt
    return round(total, 2) if total > 0 else None


def _vmss_monthly(res: dict) -> float | None:
    """VM Scale Set compute cost = per-instance VM price × capacity."""
    size, cap = res.get("vmss_vm_size"), res.get("vmss_capacity") or 0
    if not size or not cap:
        return None
    prices = _vm_prices(size, res.get("location"))
    rate = prices.get("spot") if res.get("vmss_spot") else prices.get("ondemand")
    return round(rate * cap, 2) if rate else None


# Managed-disk snapshot per-GB/month rates (US East list prices) by redundancy.
_SNAPSHOT_GB_RATE = {"lrs": 0.05, "zrs": 0.0627}


def _snapshot_monthly(res: dict) -> float | None:
    gb = res.get("snapshot_size_gb")
    if not gb:
        return None
    red = "zrs" if "zrs" in (res.get("snapshot_sku") or "").lower() else "lrs"
    # Incremental snapshots bill only changed data; we don't know the delta, so
    # price a conservative 30% of full size to avoid over-stating savings.
    factor = 0.3 if res.get("snapshot_incremental") else 1.0
    return round(gb * _SNAPSHOT_GB_RATE[red] * factor, 2)


# Azure Firewall fixed deployment fee (USD/month, 24/7) by tier — the dominant
# cost; data-processing is extra. Verified order-of-magnitude vs list prices.
_FIREWALL_MONTHLY = {"basic": 287.0, "standard": 912.5, "premium": 1277.5}


def _firewall_monthly(res: dict) -> float | None:
    tier = (res.get("fw_tier") or "standard").lower()
    return _FIREWALL_MONTHLY.get(tier, _FIREWALL_MONTHLY["standard"])


# Recovery Services Vault: representative Azure VM protected-instance fee/month
# and per-GB backup storage rates by redundancy (US East list prices).
_RSV_INSTANCE_FEE = 10.0
_RSV_GB_RATE = {
    "locallyredundant": 0.0224,
    "zoneredundant": 0.028,
    "georedundant": 0.0448,
}

# Key Vault standard transaction price (~$0.03 per 10,000 operations).
_KV_OP_RATE = 0.03


def _rsv_monthly(res: dict) -> float | None:
    """Estimate an RSV's monthly cost = protected-instance fees + backup storage."""
    items = res.get("rsv_protected_items")
    if not items:
        return None  # empty vault has effectively no cost
    gb = res.get("rsv_storage_used_gb") or 0
    red = (res.get("rsv_redundancy") or "GeoRedundant").lower().replace(" ", "")
    rate = _RSV_GB_RATE.get(red, _RSV_GB_RATE["georedundant"])
    return round(items * _RSV_INSTANCE_FEE + gb * rate, 2)


# Storage: per-GB/month data rates (US East list prices) by tier + redundancy.
# Storage is USAGE-based, so cost = stored GB (from Azure Monitor) × rate.
_STORAGE_GB_RATE: dict[str, dict[str, float]] = {
    "hot":     {"lrs": 0.0184, "zrs": 0.0230, "grs": 0.0368, "ragrs": 0.0460},
    "cool":    {"lrs": 0.0100, "zrs": 0.0125, "grs": 0.0200, "ragrs": 0.0250},
    "premium": {"lrs": 0.1500, "zrs": 0.1875, "grs": 0.1500, "ragrs": 0.1500},
}


def _storage_redundancy(sku: str) -> str:
    s = (sku or "").lower()
    if "ragrs" in s or "ra_grs" in s:
        return "ragrs"
    if "grs" in s:
        return "grs"
    if "zrs" in s:
        return "zrs"
    return "lrs"


def _storage_rate(res: dict, *, redundancy: str | None = None) -> float:
    """Per-GB/month rate for this account's tier + (optionally overridden) redundancy."""
    sku = (res.get("storage_sku") or "").lower()
    red = redundancy or _storage_redundancy(sku)
    if "premium" in sku:
        table = _STORAGE_GB_RATE["premium"]
    else:
        tier = (res.get("access_tier") or "Hot").lower()
        table = _STORAGE_GB_RATE.get(tier, _STORAGE_GB_RATE["hot"])
    return table.get(red, table["lrs"])


def _storage_monthly(res: dict) -> float | None:
    """Real usage-based monthly data cost of a storage account (from stored GB)."""
    gb = res.get("used_capacity_gb")
    if not gb or gb <= 0:
        return None
    return round(gb * _storage_rate(res), 2)


# Documented approximate list prices (USD/month, 24/7) for resources that are
# billed at a fixed hourly rate but which may have no live billing data in a
# small/new environment. Used ONLY as a fallback so idle/tier findings never
# show $0 savings. Figures are conservative pay-as-you-go US East list prices.
_FIXED_MONTHLY_FALLBACK: dict[str, float] = {
    "microsoft.network/natgateways": 32.85,            # ~$0.045/hr gateway
    "microsoft.network/loadbalancers": 18.25,          # Standard LB base ~$0.025/hr
    "microsoft.network/applicationgateways": 180.0,    # Standard_v2 base + ~1 CU
    "microsoft.network/virtualnetworkgateways": 140.0, # VpnGw1 ~$0.19/hr
    "microsoft.apimanagement/service": 700.0,          # Developer/Basic-ish floor
    "microsoft.cache/redis": 55.0,                     # C1 Standard ballpark
}


def _estimate_disk_monthly(res: dict) -> float | None:
    """Estimate a managed disk's monthly cost from size + SKU when billing data is absent."""
    size = res.get("disk_size_gb")
    sku = (res.get("disk_sku") or "").lower()
    if not size:
        return None
    rate = None
    for key, r in _DISK_GB_RATE.items():
        if key in sku:
            rate = r
            break
    if rate is None:
        rate = _DISK_GB_RATE["standardssd"]
    return round(size * rate, 2)


def _cost(res: dict) -> float | None:
    """Best-available monthly cost figure for a resource.
    Order: live billing → live retail SKU price → list-price estimate."""
    val = (
        res.get("projected_full_month_usd")
        or res.get("current_sku_projected_monthly_usd")
        or res.get("actual_cost_mtd_usd")
    )
    if val:
        return val
    # Fallback estimates for known paid resource types (live retail price first,
    # then documented list-price fallback), so savings are never silently $0.
    rtype = (res.get("type") or "").lower()
    if rtype == "microsoft.compute/disks":
        prefix = _disk_family_prefix(res.get("disk_sku"))
        if prefix:
            live = _disk_price_monthly(res.get("disk_size_gb"), _region(res), prefix)
            if live:
                return live
        return _estimate_disk_monthly(res)
    if rtype == "microsoft.compute/virtualmachines":
        # Only a RUNNING VM incurs full compute charges; a deallocated VM only
        # pays for its disk, so we must not price it at the on-demand rate here.
        power = (res.get("power_state") or "").lower()
        if res.get("is_running") is True or "running" in power:
            return _vm_prices(res.get("vm_size"), res.get("location")).get("ondemand")
        return None
    if rtype == "microsoft.network/publicipaddresses":
        return _public_ip_monthly(res)
    if rtype == "microsoft.network/applicationgateways":
        return _app_gateway_monthly(res) or _FIXED_MONTHLY_FALLBACK[rtype]
    if rtype == "microsoft.network/virtualnetworkgateways":
        return _vpn_gateway_monthly(res) or _FIXED_MONTHLY_FALLBACK[rtype]
    if rtype == "microsoft.recoveryservices/vaults":
        return _rsv_monthly(res)
    if rtype == "microsoft.storage/storageaccounts":
        return _storage_monthly(res)
    if rtype == "microsoft.web/serverfarms":
        return _app_service_plan_monthly(res)
    if rtype == "microsoft.containerregistry/registries":
        return _acr_monthly(res)
    if rtype in ("microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces"):
        return _namespace_monthly(res)
    if rtype == "microsoft.containerservice/managedclusters":
        return _aks_node_monthly(res)
    if rtype == "microsoft.compute/virtualmachinescalesets":
        return _vmss_monthly(res)
    if rtype == "microsoft.compute/snapshots":
        return _snapshot_monthly(res)
    if rtype == "microsoft.network/azurefirewalls":
        return _firewall_monthly(res)
    if rtype in _FIXED_MONTHLY_FALLBACK:
        return _FIXED_MONTHLY_FALLBACK[rtype]
    return None


def _issue(
    res: dict,
    *,
    severity: str | None = None,
    category: str,
    issue: str,
    fix_commands: list[str],
    savings_ratio: float,
    is_security_only: bool = False,
    reasoning: str | None = None,
    # ── rich recommendation metadata (all optional, sensible defaults) ────────
    title: str | None = None,
    current_config: str | None = None,
    recommended_config: str | None = None,
    confidence: float | None = None,
    risk: str | None = None,
    destructive: bool = False,
    reversible: bool = True,
    performance_impact: bool = False,
    evidence: str = "heuristic",
    doc: str | None = None,
    exclusive_group: str | None = None,
    keep_at_zero: bool = False,
    current_cost_override: float | None = None,
) -> dict:
    """Build a normalized recommendation dict.

    Backward compatible: still emits every legacy field the Excel exporter / DB
    / frontend consume (resource_name, severity, category, issue,
    *_monthly_cost_usd, savings_reasoning, fix_commands). Adds rich, scored
    fields (title, description, reason, current/recommended config, confidence,
    risk, priority, doc_url) on top.
    """
    rg = res.get("resource_group", "")
    name = res.get("name", "")
    rtype = res.get("type", "")
    current = current_cost_override if current_cost_override is not None else _cost(res)

    if is_security_only or current is None or current <= 0:
        current_cost = current if current else 0
        optimized = current_cost
        savings = 0.0
    else:
        current_cost = round(current, 2)
        optimized = round(current * (1 - savings_ratio), 2)
        savings = round(current_cost - optimized, 2)

    if reasoning:
        pass
    elif savings > 0:
        reasoning = (
            f"Projected ${current_cost:.2f}/mo × {int(savings_ratio*100)}% reduction "
            f"= ${savings:.2f}/mo saving."
        )
    else:
        reasoning = "Security/hygiene issue — no direct cost saving, but reduces risk."

    canon = canonical_category(category)

    # ── Confidence (deterministic) ────────────────────────────────────────────
    if confidence is None:
        ev = "security" if is_security_only else evidence
        wd = (90 if res.get("cpu_avg_90d") is not None else
              30 if res.get("cpu_avg_30d") is not None else
              7 if res.get("cpu_avg_7d") is not None else None)
        headroom = None
        avg = res.get("cpu_avg_pct")
        if avg is not None and ev == "metric_backed":
            headroom = max(0.0, min(1.0, (100 - avg) / 100))
        confidence = score_confidence(ev, window_days=wd, util_headroom=headroom)

    # ── Risk (deterministic) ──────────────────────────────────────────────────
    if risk is None:
        risk = score_risk(
            reversible=reversible,
            destructive=destructive,
            is_production=infer_production(res),
            performance_impact=performance_impact,
        )

    pscore = priority_score(savings, confidence, risk)
    plabel = priority_label(pscore, is_security_only)
    if severity is None:
        severity = {"P1": "high", "P2": "medium", "P3": "low", "P4": "low"}[plabel]

    return {
        # ── legacy fields (unchanged contract) ──────────────────────────────
        "resource_name": name,
        "resource_type": rtype,
        "severity": severity,
        "category": canon,
        "issue": issue,
        "current_monthly_cost_usd": current_cost,
        "optimized_monthly_cost_usd": optimized,
        "estimated_monthly_savings_usd": savings,
        "savings_reasoning": reasoning,
        "fix_commands": [c.replace("{rg}", rg).replace("{name}", name) for c in fix_commands],
        # ── rich recommendation fields (additive) ───────────────────────────
        "title": title or issue[:80],
        "description": issue,
        "reason": reasoning,
        "current_configuration": current_config,
        "recommended_configuration": recommended_config,
        "confidence_score": confidence,
        "confidence_pct": int(round(confidence * 100)),
        "risk_level": risk,
        "priority": plabel,
        "documentation_url": _doc_url(doc),
        # ── internal bookkeeping (stripped before returning to client) ──────
        "_source": "rules_engine",
        "_priority_score": pscore,
        "_exclusive_group": exclusive_group,
        "_keep_at_zero": is_security_only or keep_at_zero,  # keep even at $0
    }


# ---------------------------------------------------------------------------
# Per-type rule functions. Each returns a list of issues (possibly empty).
# ---------------------------------------------------------------------------

def _vm_cost_candidates(res: dict) -> list[dict]:
    """Emit MULTIPLE ranked cost recommendations for one running VM.

    All compute levers share the exclusive group 'vm-compute' so the report can
    show several ranked options while the SAVINGS TOTAL only counts the single
    best one (you apply one strategy, not all at once). Options considered:
      • dynamic downsize to a specific cheaper SKU (evidence: real CPU),
      • move to a Burstable B-series,
      • purchase a Reserved Instance / Savings Plan (always-on),
      • enable auto-shutdown (non-production only),
      • move to Spot (non-production / interruptible only).
    """
    out: list[dict] = []
    size = res.get("vm_size")
    loc = res.get("location")
    prices = _vm_prices(size, loc)
    ondemand = prices.get("ondemand")
    spot = prices.get("spot")
    cpu_txt = _cpu_text(res)
    underused = _cpu_underused(res)          # True / False / None
    nonprod = is_nonproduction(res)
    grp = "vm-compute"
    base = f"~${ondemand:.0f}/mo on-demand (24/7)" if ondemand else "an ongoing hourly charge"

    # 1) Dynamic right-sizing to a specific cheaper SKU (evidence-based).
    if underused is not False:
        alts = sku_advisor.cheaper_alternatives(
            size, loc, res.get("cpu_avg_pct"), res.get("cpu_max_pct")
        )
        for alt in alts[:2]:  # top 2 concrete SKUs (best saving first)
            ratio = min(max(alt["saving_pct"] / 100.0, 0.01), 0.95)
            is_b = alt["kind"] == "burstable"
            short_size = alt["size"].replace("Standard_", "")
            out.append(_issue(
                res,
                category="Overprovisioned",
                title=(f"Move to Burstable {short_size}") if is_b
                      else f"Downsize to {short_size}",
                issue=(f"VM '{size}' can move to {alt['size']} "
                       f"({alt['vcpu']} vCPU) — ~${alt['saving_usd']:.0f}/mo ({alt['saving_pct']:.0f}%) cheaper."),
                fix_commands=[f"az vm resize --resource-group {{rg}} --name {{name}} --size {alt['size']}"],
                savings_ratio=ratio,
                current_config=f"{size} (on-demand {base})",
                recommended_config=f"{alt['size']} (~${alt['monthly_usd']:.0f}/mo)",
                evidence="metric_backed" if underused is True else "metric_missing",
                performance_impact=True,
                reversible=True,
                doc="vm_bseries" if is_b else "vm_downsize",
                exclusive_group=grp,
                reasoning=(
                    f"{size} costs {base}.{cpu_txt} {alt['size']} keeps peak CPU under a safe ceiling "
                    f"and lists at ~${alt['monthly_usd']:.0f}/mo → save ~${alt['saving_usd']:.0f}/mo "
                    f"({alt['saving_pct']:.0f}%)."
                    + ("" if underused is True else " Utilisation data limited — verify before resizing.")
                ),
            ))

    # 2) Reserved Instance / Savings Plan — always valid for an always-on VM.
    if ondemand:
        out.append(_issue(
            res,
            category="Cost Saving",
            title="Purchase a 1-year Reserved Instance / Savings Plan",
            issue=(f"VM '{size}' runs 24/7 on pay-as-you-go — a 1-year Reserved Instance or "
                   f"Compute Savings Plan locks in a large discount with no config change."),
            fix_commands=[
                "# Azure Portal -> Reservations -> Add -> Virtual Machine -> size {name} ({size}), 1-year term.",
                "# Or evaluate a Compute Savings Plan for flexibility across sizes.",
            ],
            savings_ratio=_RESERVED_RATIO,
            current_config=f"{size}, pay-as-you-go ({base})",
            recommended_config=f"{size}, 1-yr Reserved Instance / Savings Plan",
            evidence="heuristic",
            reversible=False,
            doc="vm_reserved",
            exclusive_group=grp,
            reasoning=(
                f"{size} costs {base}.{cpu_txt} A 1-yr commitment cuts ~{int(_RESERVED_RATIO*100)}% "
                f"for steady-state, always-on compute."
            ),
        ))

    # 3) Spot — only for clearly non-production / interruptible workloads.
    if spot and ondemand and spot < ondemand and nonprod:
        ratio = min(1 - spot / ondemand, 0.9)
        out.append(_issue(
            res,
            category="Cost Saving",
            title="Run as a Spot VM",
            issue=(f"Non-production VM '{size}' can run on Spot capacity for interruptible/dev "
                   f"workloads at a fraction of on-demand cost."),
            fix_commands=["# Recreate the VM with --priority Spot --eviction-policy Deallocate"],
            savings_ratio=ratio,
            current_config=f"{size} on-demand (~${ondemand:.0f}/mo)",
            recommended_config=f"{size} Spot (~${spot:.0f}/mo)",
            evidence="governance",
            reversible=False,
            performance_impact=True,
            doc="vm_spot",
            exclusive_group=grp,
            reasoning=f"On-demand ~${ondemand:.0f}/mo vs Spot ~${spot:.0f}/mo — non-prod tag/name detected.",
        ))

    # 4) Auto-shutdown — additive for non-production VMs (own group so it can
    #    stack with a right-size, since stopping nights/weekends is orthogonal).
    if nonprod:
        out.append(_issue(
            res,
            category="Cost Saving",
            title="Enable auto-shutdown (dev/test)",
            issue=("Non-production VM has no auto-shutdown — stopping it outside working hours "
                   "(e.g. nights/weekends) can cut compute cost by ~65%."),
            fix_commands=["az vm auto-shutdown --resource-group {rg} --name {name} --time 1900"],
            savings_ratio=0.30,   # conservative vs a full ~65% if it truly runs 45h/168h
            current_config="Running 24/7 (168 h/week)",
            recommended_config="Auto-shutdown outside business hours (~45 h/week)",
            evidence="governance",
            reversible=True,
            doc="vm_autoshutdown",
            exclusive_group="vm-schedule",
        ))
    return out


def _rule_vm(res: dict) -> list[dict]:
    out = []
    running = res.get("is_running")
    power = res.get("power_state") or ""
    is_running = running is True or "running" in power.lower()
    is_stopped = running is False or "deallocat" in power.lower() or "stopped" in power.lower()

    if is_stopped:
        out.append(_issue(
            res, severity="high", category="Unused / Idle",
            issue=f"VM is '{power or 'not running'}' — you still pay for the OS disk and any reserved public IP.",
            fix_commands=["az vm delete --resource-group {rg} --name {name} --yes"],
            savings_ratio=0.70,
        ))
    elif is_running and res.get("vm_size"):
        out.extend(_vm_cost_candidates(res))

    if res.get("mgmt_ports_open_to_internet"):
        ports = ", ".join(res["mgmt_ports_open_to_internet"])
        out.append(_issue(
            res, severity="high", category="Security Risk",
            issue=f"Management port(s) {ports} open to the internet (SSH/RDP). High risk of brute-force attack.",
            fix_commands=["az network nsg rule update --resource-group {rg} --nsg-name <nsg> --name <rule> --source-address-prefixes <your-office-ip>"],
            savings_ratio=0, is_security_only=True,
        ))
    elif res.get("direct_public_ip_attached"):
        out.append(_issue(
            res, severity="medium", category="Security Risk",
            issue="Public IP attached directly to the VM NIC instead of behind a Load Balancer or Bastion.",
            fix_commands=["az network public-ip update --resource-group {rg} --name <pip> --remove ipConfiguration"],
            savings_ratio=0, is_security_only=True,
        ))
    return out


def _rule_disk(res: dict) -> list[dict]:
    out = []
    if res.get("is_orphaned") or res.get("disk_state") == "Unattached":
        out.append(_issue(
            res, severity="high", category="Unused / Idle",
            issue="Managed disk is unattached / orphaned (no VM owns it) — pure wasted spend.",
            fix_commands=["az disk delete --resource-group {rg} --name {name} --yes"],
            savings_ratio=_IDLE_DELETE_RATIO,
        ))
    if (res.get("disk_sku") or "").startswith("Premium"):
        # Compute the EXACT saving from real Premium vs Standard SSD list prices
        # for this disk's size, instead of a flat ratio.
        size = res.get("disk_size_gb")
        region = _region(res)
        premium = _disk_price_monthly(size, region, "P")
        standard = _disk_price_monthly(size, region, "E")
        if premium and standard and standard < premium:
            ratio = 1 - (standard / premium)
            reasoning = (
                f"Premium disk ({size} GB) lists at ${premium:.2f}/mo; the equivalent "
                f"Standard SSD is ${standard:.2f}/mo → save ${premium - standard:.2f}/mo."
            )
        else:
            ratio = _TIER_DOWNGRADE_RATIO
            reasoning = None
        out.append(_issue(
            res, severity="medium", category="Wrong Pricing Tier",
            issue="Premium SSD disk — if this is not a latency-critical/production workload, Standard SSD is cheaper for the same capacity.",
            fix_commands=["az disk update --resource-group {rg} --name {name} --sku StandardSSD_LRS"],
            savings_ratio=ratio,
            reasoning=reasoning,
        ))
    return out


def _rule_public_ip(res: dict) -> list[dict]:
    if res.get("is_attached") is False:
        return [_issue(
            res, severity="medium", category="Unused / Idle",
            issue="Public IP is not attached to anything — you pay a reservation fee for an unused address.",
            fix_commands=["az network public-ip delete --resource-group {rg} --name {name}"],
            savings_ratio=_IDLE_DELETE_RATIO,
        )]
    return []


def _rule_nic(res: dict) -> list[dict]:
    out = []
    if res.get("nic_is_attached") is False:
        out.append(_issue(
            res, severity="low", category="Unused / Idle",
            issue="Network interface is not attached to any VM — orphaned resource.",
            fix_commands=["az network nic delete --resource-group {rg} --name {name}"],
            savings_ratio=_IDLE_DELETE_RATIO,
        ))
    if res.get("nic_has_public_ip"):
        out.append(_issue(
            res, severity="medium", category="Security Risk",
            issue="NIC has a public IP attached directly — should route through a Load Balancer or Bastion.",
            fix_commands=["# Review and detach the public IP from this NIC"],
            savings_ratio=0, is_security_only=True,
        ))
    return out


def _rule_nsg(res: dict) -> list[dict]:
    out = []
    risky = res.get("risky_port_rules") or []
    if risky:
        ports = ", ".join(sorted({r.get("port", "?") for r in risky}))
        out.append(_issue(
            res, severity="high", category="Security Risk",
            issue=f"NSG has inbound rule(s) allowing management port(s) {ports} from the internet (Any/*).",
            fix_commands=[f"az network nsg rule update --resource-group {{rg}} --nsg-name {{name}} --name {r.get('rule')} --source-address-prefixes <your-ip>" for r in risky],
            savings_ratio=0, is_security_only=True,
        ))
    elif res.get("open_inbound_rules"):
        rules = ", ".join(res["open_inbound_rules"])
        out.append(_issue(
            res, severity="high", category="Security Risk",
            issue=f"NSG has wildcard allow rule(s) open to all ports from the internet: {rules}.",
            fix_commands=[f"az network nsg rule delete --resource-group {{rg}} --nsg-name {{name}} --name {r}" for r in res["open_inbound_rules"]],
            savings_ratio=0, is_security_only=True,
        ))
    return out


def _rule_storage(res: dict) -> list[dict]:
    out = []
    if res.get("is_empty"):
        out.append(_issue(
            res, severity="low", category="Unused / Idle",
            issue="Storage account has zero blob containers — likely unused.",
            fix_commands=["az storage account delete --resource-group {rg} --name {name} --yes"],
            savings_ratio=_IDLE_DELETE_RATIO,
        ))
    if res.get("allow_blob_public_access") is True:
        out.append(_issue(
            res, severity="high", category="Security Risk",
            issue="Blob public access is enabled — blobs may be readable by anyone on the internet.",
            fix_commands=["az storage account update --resource-group {rg} --name {name} --allow-blob-public-access false"],
            savings_ratio=0, is_security_only=True,
        ))
    if res.get("network_default_action") == "Allow":
        out.append(_issue(
            res, severity="medium", category="Security Risk",
            issue="Storage network default action is 'Allow' — account is reachable from all networks with no IP/VNet restriction.",
            fix_commands=["az storage account update --resource-group {rg} --name {name} --default-action Deny"],
            savings_ratio=0, is_security_only=True,
        ))
    if res.get("min_tls_version") in ("TLS1_0", "TLS1_1"):
        out.append(_issue(
            res, severity="medium", category="Security Risk",
            issue=f"Storage accepts outdated {res.get('min_tls_version')} — upgrade to TLS1_2.",
            fix_commands=["az storage account update --resource-group {rg} --name {name} --min-tls-version TLS1_2"],
            savings_ratio=0, is_security_only=True,
        ))
    if res.get("has_lifecycle_policy") is False and res.get("access_tier") == "Hot":
        out.append(_issue(
            res, severity="medium", category="Optimization Opportunity",
            issue="No blob lifecycle policy and tier is Hot — old blobs never move to cheaper Cool/Archive tiers.",
            fix_commands=["# Add a lifecycle management rule in the Azure Portal or via 'az storage account management-policy create'"],
            savings_ratio=_LIFECYCLE_RATIO,
        ))
    if (res.get("storage_sku") or "").startswith("Premium"):
        # Exact saving from real per-GB rates: Premium vs Standard Hot LRS.
        gb = res.get("used_capacity_gb")
        prem_rate = _storage_rate(res)
        std_rate = _STORAGE_GB_RATE["hot"]["lrs"]
        reasoning = None
        ratio = _TIER_DOWNGRADE_RATIO
        if prem_rate > 0 and std_rate < prem_rate:
            ratio = 1 - (std_rate / prem_rate)
            if gb:
                reasoning = (
                    f"{gb:.0f} GB on Premium (${gb * prem_rate:.2f}/mo) vs Standard Hot LRS "
                    f"(${gb * std_rate:.2f}/mo) → save ${gb * (prem_rate - std_rate):.2f}/mo on data at rest."
                )
        out.append(_issue(
            res, severity="medium", category="Wrong Pricing Tier",
            issue="Premium storage SKU — if this is not a high-IOPS workload, Standard is significantly cheaper per GB.",
            fix_commands=["# Migrate data to a Standard_LRS/GRS storage account (SKU cannot be downgraded in place)"],
            savings_ratio=ratio,
            reasoning=reasoning,
        ))

    # Geo-redundant storage → Local-redundant for non-critical data.
    red = _storage_redundancy(res.get("storage_sku") or "")
    if red in ("grs", "ragrs") and "premium" not in (res.get("storage_sku") or "").lower():
        gb = res.get("used_capacity_gb")
        total = _cost(res)
        reasoning = None
        ratio = _REDUNDANCY_RATIO
        if gb and total and total > 0:
            geo_rate = _storage_rate(res, redundancy=red)
            lrs_rate = _storage_rate(res, redundancy="lrs")
            saving = round(gb * (geo_rate - lrs_rate), 2)
            if saving > 0:
                ratio = min(saving / total, 0.9)
                reasoning = (
                    f"{gb:.0f} GB geo-redundant (${gb * geo_rate:.2f}/mo) vs locally-redundant "
                    f"(${gb * lrs_rate:.2f}/mo) → save ${saving:.2f}/mo for non-critical data."
                )
        out.append(_issue(
            res, severity="low", category="Redundancy Config",
            issue="Storage uses geo-redundant storage (GRS/RA-GRS) — Locally-redundant (LRS) is cheaper for non-critical data.",
            fix_commands=["az storage account update --resource-group {rg} --name {name} --sku Standard_LRS"],
            savings_ratio=ratio,
            reasoning=reasoning,
        ))
    if res.get("blob_soft_delete_enabled") is False:
        out.append(_issue(
            res, severity="low", category="Misconfigured",
            issue="Blob soft-delete is disabled — accidental deletions are unrecoverable (data-loss risk).",
            fix_commands=["az storage blob service-properties delete-policy update --account-name {name} --enable true --days-retained 7"],
            savings_ratio=0, is_security_only=True,
        ))
    if res.get("file_share_large_quota"):
        out.append(_issue(
            res, severity="medium", category="Over-provisioned",
            issue="File share provisioned quota exceeds 1 TB — on Premium shares you pay for provisioned GB, not used GB.",
            fix_commands=["# Reduce the file share quota to match actual usage"],
            savings_ratio=_MINOR_CONFIG_RATIO,
        ))
    if res.get("table_count") and (res.get("storage_sku") or "").startswith("Premium"):
        out.append(_issue(
            res, severity="low", category="Wrong Pricing Tier",
            issue="Table storage on a Premium account — tables gain no benefit from Premium; move to Standard.",
            fix_commands=["# Migrate tables to a Standard storage account"],
            savings_ratio=_TIER_DOWNGRADE_RATIO,
        ))
    return out


def _rule_postgres_mysql(res: dict) -> list[dict]:
    out = []
    tier = (res.get("compute_tier") or "").lower()
    is_mysql = "mysql" in res.get("type", "").lower()
    svc = "mysql" if is_mysql else "postgres"
    if tier in ("generalpurpose", "memoryoptimized"):
        # Evidence-based: only advise Burstable when measured CPU is low or
        # unknown. A busy DB genuinely needs GeneralPurpose, so we skip it.
        underused = _cpu_underused(res)
        if underused is not False:
            cpu_txt = _cpu_text(res)
            hedge = "" if underused is True else " (Utilisation data unavailable — verify before changing tier.)"
            out.append(_issue(
                res, severity="medium" if underused else "low", category="Wrong Pricing Tier",
                issue=f"Database on {res.get('compute_tier')} tier — for dev/test or light workloads, Burstable is ~50-60% cheaper.",
                fix_commands=[f"az {svc} flexible-server update --resource-group {{rg}} --name {{name}} --tier Burstable --sku-name Standard_B2ms"],
                savings_ratio=_TIER_DOWNGRADE_RATIO,
                reasoning=(
                    f"{res.get('compute_tier')} tier.{cpu_txt} Burstable is ~50-60% cheaper "
                    f"for low-usage databases.{hedge}"
                ),
            ))
    ha = (res.get("ha_mode") or "").lower()
    if ha and ha not in ("disabled", "none", ""):
        out.append(_issue(
            res, severity="medium", category="Redundancy Config",
            issue=f"High Availability ({res.get('ha_mode')}) is enabled — this doubles compute cost. Disable for non-production.",
            fix_commands=[f"az {svc} flexible-server update --resource-group {{rg}} --name {{name}} --high-availability Disabled"],
            savings_ratio=_HA_DISABLE_RATIO,
        ))
    retention = res.get("backup_retention_days")
    if isinstance(retention, int) and retention > 14:
        out.append(_issue(
            res, severity="low", category="Misconfigured",
            issue=f"Backup retention is {retention} days — reduce to 7 for dev/test to cut backup storage cost.",
            fix_commands=[f"az {svc} flexible-server update --resource-group {{rg}} --name {{name}} --backup-retention 7"],
            savings_ratio=_MINOR_CONFIG_RATIO,
        ))
    if (res.get("db_state") or "").lower() == "stopped":
        out.append(_issue(
            res, severity="low", category="Unused / Idle",
            issue="Database server is stopped but storage is still billed. Delete if no longer needed.",
            fix_commands=[f"az {svc} flexible-server delete --resource-group {{rg}} --name {{name}} --yes"],
            savings_ratio=0.30,
        ))
    return out


def _rule_sql_db(res: dict) -> list[dict]:
    out = []
    tier = (res.get("sql_tier") or "")
    sku = res.get("sql_sku_name") or ""

    if (res.get("sql_status") or "").lower() == "paused":
        out.append(_issue(
            res, severity="low", category="Unused / Idle",
            issue="Serverless SQL database is paused — verify it is still needed, otherwise delete.",
            fix_commands=["# Review; delete with: az sql db delete --resource-group {rg} --server <server> --name {name} --yes"],
            savings_ratio=0.30,
        ))

    # Evidence-based right-sizing. We emit AT MOST ONE cost lever so savings
    # are never double-counted:
    #   • Business Critical  → downgrade the whole tier to General Purpose (big lever).
    #   • Other tiers        → if over-provisioned on vCores, halve them.
    is_bc = "businesscritical" in tier.lower()
    underused = _cpu_underused(res)
    if is_bc and underused is not False:
        cpu_txt = _cpu_text(res)
        hedge = "" if underused is True else " (Utilisation data unavailable — verify before changing tier.)"
        out.append(_issue(
            res, severity="medium" if underused else "low", category="Wrong Pricing Tier",
            issue="SQL DB on Business Critical tier — for workloads that don't need local-SSD/read-replica performance, General Purpose is ~50% cheaper.",
            fix_commands=["az sql db update --resource-group {rg} --server <server> --name {name} --edition GeneralPurpose"],
            savings_ratio=_TIER_DOWNGRADE_RATIO,
            reasoning=(
                f"Business Critical tier.{cpu_txt} General Purpose delivers the same vCores at "
                f"~50% lower compute cost for non-latency-critical workloads.{hedge}"
            ),
        ))
    elif not is_bc:
        # Parse the vCore count from the SKU ("GP_Gen5_8" → 8) and, if it is
        # generous and CPU is genuinely low, suggest halving it.
        m = re.search(r"_(\d+)$", sku)
        vcores = int(m.group(1)) if m else None
        if vcores and vcores >= 4 and underused is True:
            out.append(_issue(
                res, severity="medium", category="Over-provisioned",
                issue=f"SQL DB is provisioned with {vcores} vCores but CPU usage is low — halve the vCores.",
                fix_commands=[f"az sql db update --resource-group {{rg}} --server <server> --name {{name}} --capacity {max(vcores // 2, 1)}"],
                savings_ratio=_RIGHTSIZE_RATIO,
                reasoning=(
                    f"{vcores} vCores provisioned.{_cpu_text(res)} Halving vCores roughly halves compute cost."
                ),
            ))

    if res.get("sql_zone_redundant") is True:
        out.append(_issue(
            res, severity="medium", category="Redundancy Config",
            issue="Zone redundancy enabled — adds ~50% cost. Disable for non-critical workloads.",
            fix_commands=["az sql db update --resource-group {rg} --server <server> --name {name} --zone-redundant false"],
            savings_ratio=_REDUNDANCY_RATIO,
        ))
    return out


def _rule_redis(res: dict) -> list[dict]:
    out = []
    if (res.get("redis_sku_name") or "") == "Premium":
        out.append(_issue(
            res, severity="medium", category="Wrong Pricing Tier",
            issue="Redis Premium SKU — for simple caching, Standard offers the same core features at ~50% less.",
            fix_commands=["az redis update --resource-group {rg} --name {name} --sku Standard --vm-size c1"],
            savings_ratio=_TIER_DOWNGRADE_RATIO,
        ))
    if res.get("redis_non_ssl_enabled"):
        out.append(_issue(
            res, severity="medium", category="Security Risk",
            issue="Non-SSL port is enabled on Redis — data in transit is unencrypted.",
            fix_commands=["az redis update --resource-group {rg} --name {name} --set enableNonSslPort=false"],
            savings_ratio=0, is_security_only=True,
        ))
    return out


def _rule_load_balancer(res: dict) -> list[dict]:
    out = []
    if res.get("lb_is_unused"):
        out.append(_issue(
            res, severity="medium", category="Unused / Idle",
            issue="Load Balancer has no frontend or no backend configured — it is not doing anything.",
            fix_commands=["az network lb delete --resource-group {rg} --name {name}"],
            savings_ratio=_IDLE_DELETE_RATIO,
        ))
    if (res.get("lb_sku") or "") == "Basic":
        out.append(_issue(
            res, severity="low", category="Misconfigured",
            issue="Basic SKU Load Balancer — no SLA and no zone redundancy. Standard is recommended.",
            fix_commands=["# Migrate to a Standard SKU Load Balancer"],
            savings_ratio=0, is_security_only=True,
        ))
    return out


def _rule_app_gateway(res: dict) -> list[dict]:
    out = []
    if res.get("agw_is_empty"):
        out.append(_issue(
            res, severity="high", category="Unused / Idle",
            issue="Application Gateway has no backend pools or routing rules — idle but billed hourly.",
            fix_commands=["az network application-gateway delete --resource-group {rg} --name {name}"],
            savings_ratio=_IDLE_DELETE_RATIO,
        ))
    if res.get("agw_autoscale") is False:
        out.append(_issue(
            res, severity="medium", category="Misconfigured",
            issue="Fixed capacity (no autoscale) — you pay for peak capacity 24/7. Enable autoscale.",
            fix_commands=["az network application-gateway update --resource-group {rg} --name {name} --min-capacity 1 --max-capacity 5"],
            savings_ratio=_MINOR_CONFIG_RATIO,
        ))
    if (res.get("agw_waf_mode") or "") == "Detection":
        out.append(_issue(
            res, severity="medium", category="Security Risk",
            issue="WAF is in Detection mode — it logs attacks but does not block them.",
            fix_commands=["az network application-gateway waf-config set --resource-group {rg} --gateway-name {name} --enabled true --firewall-mode Prevention --rule-set-version 3.2"],
            savings_ratio=0, is_security_only=True,
        ))
    return out


def _rule_nat_gateway(res: dict) -> list[dict]:
    if res.get("nat_is_orphaned"):
        return [_issue(
            res, severity="medium", category="Unused / Idle",
            issue="NAT Gateway is not associated with any subnet — idle but billed.",
            fix_commands=["az network nat gateway delete --resource-group {rg} --name {name}"],
            savings_ratio=_IDLE_DELETE_RATIO,
        )]
    return []


def _rule_vpn_gateway(res: dict) -> list[dict]:
    if res.get("vng_is_unused"):
        return [_issue(
            res, severity="high", category="Unused / Idle",
            issue="VPN Gateway has no connections — significant idle cost ($130-700+/mo).",
            fix_commands=["az network vnet-gateway delete --resource-group {rg} --name {name}"],
            savings_ratio=_IDLE_DELETE_RATIO,
        )]
    return []


def _rule_vnet(res: dict) -> list[dict]:
    if res.get("vnet_is_isolated"):
        return [_issue(
            res, severity="low", category="Unused / Idle",
            issue="VNet has no peerings and a single subnet — likely an abandoned dev VNet.",
            fix_commands=["# Review whether this VNet is still needed"],
            savings_ratio=0, is_security_only=True,
        )]
    return []


def _rule_keyvault(res: dict) -> list[dict]:
    out = []
    if res.get("kv_public_access") == "Enabled" and res.get("kv_network_default_action") == "Allow":
        out.append(_issue(
            res, severity="medium", category="Security Risk",
            issue="Key Vault is reachable from any IP with no network restriction.",
            fix_commands=["az keyvault update --resource-group {rg} --name {name} --public-network-access Disabled"],
            savings_ratio=0, is_security_only=True,
        ))
    if res.get("kv_purge_protection") is False:
        out.append(_issue(
            res, severity="low", category="Security Risk",
            issue="Purge protection is disabled — secrets/keys can be permanently deleted immediately.",
            fix_commands=["az keyvault update --resource-group {rg} --name {name} --enable-purge-protection true"],
            savings_ratio=0, is_security_only=True,
        ))
    if (res.get("kv_sku_name") or "").lower() == "premium" and res.get("kv_key_count") == 0:
        out.append(_issue(
            res, severity="low", category="Wrong Pricing Tier",
            issue="Premium Key Vault with no HSM-backed keys — Standard is sufficient and cheaper.",
            fix_commands=["az keyvault update --resource-group {rg} --name {name} --sku standard"],
            savings_ratio=_TIER_DOWNGRADE_RATIO,
        ))
    if res.get("kv_expiring_certs"):
        certs = ", ".join(res["kv_expiring_certs"])
        out.append(_issue(
            res, severity="medium", category="Misconfigured",
            issue=f"Certificate(s) expiring within 60 days: {certs}. Renew to avoid outage.",
            fix_commands=["# Renew the expiring certificate(s) in Key Vault"],
            savings_ratio=0, is_security_only=True,
        ))

    # High transaction volume — Key Vault is billed per 10k operations. Chatty
    # apps that fetch the same secret on every request drive avoidable cost;
    # caching secrets in memory (with a TTL) collapses most of these hits.
    hits = res.get("kv_api_hits_30d")
    if isinstance(hits, (int, float)) and hits >= 3_000_000:
        op_cost = round(hits / 10_000 * _KV_OP_RATE, 2)   # standard transaction rate
        if op_cost >= 5:                                   # only worth flagging above ~$5/mo
            reasoning = (
                f"~{int(hits):,} Key Vault operations in 30 days ≈ ${op_cost:.2f}/mo in transaction "
                f"charges. Caching frequently-read secrets in-app (short TTL) can cut most of these "
                f"calls — a ~50% reduction saves ~${op_cost * 0.5:.2f}/mo."
            )
            out.append(_issue(
                res, severity="low", category="Cost Saving",
                title="Cache secrets to cut Key Vault operations",
                issue=f"Very high transaction volume (~{int(hits):,} ops/30d ≈ ${op_cost:.2f}/mo). Cache frequently-read secrets in-app instead of fetching per request.",
                fix_commands=["# Add in-memory secret caching (e.g. 5-10 min TTL) in the app; avoid per-request Key Vault reads"],
                savings_ratio=0.5,
                current_config=f"~{int(hits):,} operations/30d",
                recommended_config="Cached reads (fewer direct Key Vault calls)",
                evidence="metric_backed", reversible=True,
                current_cost_override=op_cost, reasoning=reasoning,
            ))
    return out


def _rule_apim(res: dict) -> list[dict]:
    out = []
    if res.get("apim_is_empty"):
        out.append(_issue(
            res, severity="high", category="Unused / Idle",
            issue="API Management has no APIs deployed — an expensive idle service ($500-3000/mo).",
            fix_commands=["# Delete or downgrade this APIM instance if unused"],
            savings_ratio=_IDLE_DELETE_RATIO,
        ))
    if (res.get("apim_sku_name") or "") == "Premium" and not res.get("apim_multi_region"):
        out.append(_issue(
            res, severity="medium", category="Wrong Pricing Tier",
            issue="Premium APIM without multi-region — Standard is sufficient and far cheaper.",
            fix_commands=["# Change APIM SKU to Standard"],
            savings_ratio=_TIER_DOWNGRADE_RATIO,
        ))
    return out


def _rule_logic_app(res: dict) -> list[dict]:
    out = []
    if res.get("logic_is_disabled"):
        out.append(_issue(
            res, severity="low", category="Unused / Idle",
            issue="Logic App workflow is disabled — consider deleting if no longer needed.",
            fix_commands=["az logic workflow delete --resource-group {rg} --name {name} --yes"],
            savings_ratio=0.30,
        ))
    if res.get("logic_integration_account"):
        out.append(_issue(
            res, severity="low", category="Wrong Pricing Tier",
            issue="An Integration Account is linked (adds $300+/mo) — verify it is actually used.",
            fix_commands=["# Review Integration Account usage"],
            savings_ratio=_MINOR_CONFIG_RATIO,
        ))
    return out


def _rule_rsv(res: dict) -> list[dict]:
    out = []
    if res.get("rsv_protected_items") == 0:
        out.append(_issue(
            res, severity="medium", category="Unused / Idle",
            issue="Recovery Services Vault has zero protected items — empty vault, delete it.",
            fix_commands=["az backup vault delete --resource-group {rg} --name {name} --yes"],
            savings_ratio=_IDLE_DELETE_RATIO,
        ))
    if (res.get("rsv_redundancy") or "") == "GeoRedundant":
        # Only the backup-storage portion changes with redundancy; the
        # per-instance fee stays the same. Compute the storage-only saving.
        gb = res.get("rsv_storage_used_gb") or 0
        total = _cost(res)
        reasoning = None
        ratio = _REDUNDANCY_RATIO
        if gb and total and total > 0:
            storage_saving = round(gb * (_RSV_GB_RATE["georedundant"] - _RSV_GB_RATE["locallyredundant"]), 2)
            if storage_saving > 0:
                ratio = min(storage_saving / total, 0.9)
                reasoning = (
                    f"GRS backup storage ({gb:.0f} GB) costs ${gb * _RSV_GB_RATE['georedundant']:.2f}/mo; "
                    f"LRS is ${gb * _RSV_GB_RATE['locallyredundant']:.2f}/mo → save ${storage_saving:.2f}/mo "
                    f"(protected-instance fees are unchanged)."
                )
        out.append(_issue(
            res, severity="low", category="Redundancy Config",
            issue="Vault uses GeoRedundant storage — LocallyRedundant is ~50% cheaper on the storage portion for non-critical backups.",
            fix_commands=["az backup vault backup-properties set --resource-group {rg} --name {name} --backup-storage-redundancy LocallyRedundant"],
            savings_ratio=ratio,
            reasoning=reasoning,
            doc="rsv_redundancy",
        ))

    # Long DAILY retention keeps many recovery points and is the biggest driver
    # of backup-storage cost. Reducing it (using weekly/monthly for older points)
    # cuts stored data for non-compliance workloads.
    daily = res.get("rsv_max_daily_retention_days")
    if isinstance(daily, int) and daily > 30:
        gb = res.get("rsv_storage_used_gb") or 0
        total = _cost(res)
        reduce_frac = 1 - 30 / daily          # share of daily RPs we could drop
        reasoning = None
        ratio = _MINOR_CONFIG_RATIO
        if gb and total and total > 0:
            red = (res.get("rsv_redundancy") or "GeoRedundant").lower().replace(" ", "")
            rate = _RSV_GB_RATE.get(red, _RSV_GB_RATE["georedundant"])
            # Conservative: assume ~50% of stored GB is daily recovery points.
            saving = round(gb * rate * reduce_frac * 0.5, 2)
            if saving > 0:
                ratio = min(saving / total, 0.6)
                reasoning = (
                    f"Daily retention is {daily} days on ~{gb:.0f} GB of backup storage. "
                    f"Trimming daily recovery points toward 30 days (keeping weekly/monthly for "
                    f"older points) could save ~${saving:.2f}/mo of backup storage."
                )
        out.append(_issue(
            res, severity="low", category="Cost Saving",
            title="Reduce backup daily-retention",
            issue=f"Backup policy keeps {daily} days of DAILY recovery points — for non-compliance workloads, 30 days (plus weekly/monthly long-term) is usually enough and cuts backup-storage cost.",
            fix_commands=["# Edit the backup policy: reduce daily retention to 30 days, keep weekly/monthly/yearly as required"],
            savings_ratio=ratio,
            current_config=f"{daily}-day daily retention",
            recommended_config="30-day daily retention + weekly/monthly long-term",
            evidence="heuristic", reversible=True, keep_at_zero=True,
            doc="rsv_redundancy", reasoning=reasoning,
        ))
    return out


def _rule_app_service_plan(res: dict) -> list[dict]:
    out = []
    sites = res.get("asp_number_of_sites")
    tier = (res.get("asp_sku_tier") or "").lower()
    sku = res.get("asp_sku_name") or ""
    workers = res.get("asp_workers") or 1

    if sites == 0:
        out.append(_issue(
            res, severity="medium", category="Unused / Idle",
            issue="App Service Plan hosts zero apps — an orphaned plan billed with nothing running on it.",
            fix_commands=["az appservice plan delete --resource-group {rg} --name {name} --yes"],
            savings_ratio=_IDLE_DELETE_RATIO,
        ))
        return out  # no point right-sizing a plan we recommend deleting

    # Evidence-based right-sizing: only advise a smaller tier when measured CPU
    # is low (or unknown). A busy plan genuinely needs its current size.
    if tier in ("premium", "premiumv2", "premiumv3", "standard"):
        underused = _cpu_underused(res)
        if underused is not False:
            cpu_txt = _cpu_text(res)
            hedge = "" if underused is True else " (Utilisation data unavailable — verify before resizing.)"
            if tier == "standard":
                target, target_sku = "Basic (dev/test) or a smaller Standard instance", "B1"
            else:
                target, target_sku = "Standard, or a smaller Premium instance", "P1v3"
            out.append(_issue(
                res, severity="medium" if underused else "low", category="Over-provisioned",
                issue=f"App Service Plan '{sku}' ({res.get('asp_sku_tier')}) is over-sized for its load — move to {target}.",
                fix_commands=[
                    f"az appservice plan update --resource-group {{rg}} --name {{name}} --sku {target_sku}",
                    "# Pick the SKU matching your real CPU/RAM need.",
                ],
                savings_ratio=_TIER_DOWNGRADE_RATIO,
                reasoning=(
                    f"{res.get('asp_sku_tier')} tier on '{sku}'.{cpu_txt} Right-sizing to a smaller "
                    f"tier/SKU typically saves ~{int(_TIER_DOWNGRADE_RATIO*100)}%.{hedge}"
                ),
            ))
        elif workers > 1:
            # Busy plan, but multiple workers on low average CPU → scale in.
            out.append(_issue(
                res, severity="low", category="Over-provisioned",
                issue=f"App Service Plan runs {workers} instances — if load allows, scaling in reduces cost proportionally.",
                fix_commands=["az appservice plan update --resource-group {rg} --name {name} --number-of-workers 1"],
                savings_ratio=_MINOR_CONFIG_RATIO,
            ))
    return out


def _rule_web_app(res: dict) -> list[dict]:
    out = []
    if res.get("is_running") is False or (res.get("state") and res.get("state") != "Running"):
        out.append(_issue(
            res, severity="medium", category="Unused / Idle",
            issue="Web/Function app is stopped — you are still paying for the underlying App Service Plan.",
            fix_commands=["# Restart if needed, or delete the app and its plan if abandoned"],
            savings_ratio=0.30,
        ))
    if res.get("https_only") is False:
        out.append(_issue(
            res, severity="low", category="Security Risk",
            issue="HTTPS-only is not enforced — traffic can be sent over plain HTTP.",
            fix_commands=["az webapp update --resource-group {rg} --name {name} --https-only true"],
            savings_ratio=0, is_security_only=True,
        ))
    return out


def _rule_cosmos(res: dict) -> list[dict]:
    out = []
    locs = res.get("cosmos_locations") or []
    if res.get("cosmos_multi_region") and len(locs) > 1:
        n = len(locs)
        ratio = min((n - 1) / n, 0.7)
        out.append(_issue(
            res, severity="medium", category="Redundancy Config",
            issue=f"Cosmos DB is replicated across {n} regions — each extra region multiplies the provisioned throughput (RU/s) cost. For non-production, use a single region.",
            fix_commands=[
                "# Portal → Replicate data globally → remove extra regions, or:",
                "az cosmosdb update --resource-group {rg} --name {name} --locations regionName=<primary> failoverPriority=0 isZoneRedundant=False",
            ],
            savings_ratio=ratio,
            reasoning=(
                f"{n} regions provisioned; consolidating to a single region removes "
                f"~{int(ratio*100)}% of the duplicated RU/s cost for non-critical workloads."
            ),
        ))
    if (res.get("cosmos_backup_type") or "") == "Continuous":
        out.append(_issue(
            res, severity="low", category="Misconfigured",
            issue="Cosmos DB uses Continuous (point-in-time) backup — if 30-day PITR is not required, Periodic backup is cheaper.",
            fix_commands=["# Switch the account to Periodic backup if PITR is not needed"],
            savings_ratio=_MINOR_CONFIG_RATIO,
        ))
    return out


def _rule_aks(res: dict) -> list[dict]:
    out = []
    pools = res.get("aks_node_pools") or []
    loc = res.get("location")
    cluster_cost = _cost(res) or 0

    # 1) Spot for USER node pools currently on-demand (interruptible workloads).
    for p in pools:
        if p.get("mode") == "User" and not p.get("spot"):
            vm, cnt = p.get("vm_size"), p.get("count") or 0
            prices = _vm_prices(vm, loc) if vm else {}
            od, sp = prices.get("ondemand"), prices.get("spot")
            if vm and cnt and od and sp and sp < od:
                delta = round((od - sp) * cnt, 2)
                ratio = min(delta / cluster_cost, 0.9) if cluster_cost > 0 else _RIGHTSIZE_RATIO
                out.append(_issue(
                    res, severity="medium", category="Optimization Opportunity",
                    issue=f"AKS user node pool '{p.get('name')}' ({cnt}× {vm}) runs on-demand — Spot nodes suit interruptible/batch workloads at a fraction of the cost.",
                    fix_commands=[
                        f"az aks nodepool add --cluster-name {{name}} --resource-group {{rg}} --name spotpool --priority Spot --eviction-policy Delete --node-count {cnt} --node-vm-size {vm}",
                    ],
                    savings_ratio=ratio,
                    reasoning=(
                        f"{cnt}× {vm} on-demand ≈ ${od*cnt:.0f}/mo vs Spot ≈ ${sp*cnt:.0f}/mo → "
                        f"save ~${delta:.0f}/mo on this pool (interruptible workloads only)."
                    ),
                ))

    # 2) Fixed-size pools (no cluster autoscaler) pay for peak capacity 24/7.
    no_autoscale = [p.get("name") for p in pools if p.get("min_count") is None]
    if no_autoscale:
        out.append(_issue(
            res, severity="low", category="Misconfigured",
            issue=f"Node pool(s) {', '.join(no_autoscale)} have a fixed node count (no cluster autoscaler) — you pay for peak capacity 24/7.",
            fix_commands=["az aks nodepool update --cluster-name {name} --resource-group {rg} --name <pool> --enable-cluster-autoscaler --min-count 1 --max-count 5"],
            savings_ratio=_MINOR_CONFIG_RATIO,
        ))
    return out


def _rule_acr(res: dict) -> list[dict]:
    out = []
    sku = (res.get("acr_sku") or "").lower()
    if sku == "premium":
        prem, std = _ACR_MONTHLY_FALLBACK["premium"], _ACR_MONTHLY_FALLBACK["standard"]
        ratio = round(1 - std / prem, 2)  # ~0.60
        out.append(_issue(
            res, severity="low", category="Wrong Pricing Tier",
            issue="Premium Container Registry — if you don't use geo-replication, private link, or >500 GB storage, Standard is sufficient and cheaper.",
            fix_commands=["az acr update --resource-group {rg} --name {name} --sku Standard"],
            savings_ratio=ratio,
            reasoning=(
                f"Premium registry fee ~${prem:.0f}/mo vs Standard ~${std:.0f}/mo → "
                f"save ~${prem - std:.0f}/mo if Premium-only features aren't used."
            ),
        ))
    if res.get("acr_admin_enabled") is True:
        out.append(_issue(
            res, severity="medium", category="Security Risk",
            issue="ACR admin user is enabled — a shared username/password bypasses Azure AD/RBAC. Disable it and use token or AAD auth.",
            fix_commands=["az acr update --resource-group {rg} --name {name} --admin-enabled false"],
            savings_ratio=0, is_security_only=True,
        ))
    return out


def _rule_namespace(res: dict) -> list[dict]:
    """Service Bus / Event Hubs namespaces."""
    out = []
    kind = "Event Hubs" if "eventhub" in (res.get("type") or "").lower() else "Service Bus"
    if (res.get("ns_sku_name") or "").lower() == "premium":
        out.append(_issue(
            res, severity="medium", category="Wrong Pricing Tier",
            issue=f"{kind} Premium namespace has a fixed hourly fee per unit — if you don't need VNet isolation, predictable latency or high dedicated throughput, Standard is usage-based and far cheaper.",
            fix_commands=[f"# Recreate the namespace on the Standard tier if Premium isolation/throughput isn't required"],
            savings_ratio=_TIER_DOWNGRADE_RATIO,
        ))
    return out


def _rule_snapshot(res: dict) -> list[dict]:
    out = []
    age = resource_age_days(res)
    gb = res.get("snapshot_size_gb")
    if age is not None and age > 90:
        out.append(_issue(
            res, category="Unused / Idle",
            title="Delete stale snapshot",
            issue=f"Managed-disk snapshot is {age} days old — long-lived snapshots are often forgotten backups that keep billing.",
            fix_commands=["az snapshot delete --resource-group {rg} --name {name}"],
            savings_ratio=_IDLE_DELETE_RATIO,
            current_config=f"{gb or '?'} GB snapshot, age {age}d",
            recommended_config="Deleted (or moved to a retention policy)",
            evidence="governance", destructive=True, reversible=False,
            doc="snapshot_stale",
            reasoning=(
                f"Snapshot has existed {age} days. If it isn't part of an active backup policy, "
                f"deleting it removes ongoing snapshot storage charges."
            ),
        ))
    return out


def _rule_log_analytics(res: dict) -> list[dict]:
    out = []
    retention = res.get("la_retention_days")
    quota = res.get("la_daily_quota_gb")
    if isinstance(retention, int) and retention > 90:
        # Beyond the free 31 days, retained data is billed per GB/month. If we
        # know the daily quota we can estimate the interactive-retention cost.
        savings_ratio = _MINOR_CONFIG_RATIO
        reasoning = None
        if quota:
            billable = quota * 30.4 * (retention - 31) / 30.0 * 0.10
            reasoning = (
                f"Retention {retention}d on ~{quota} GB/day. Reducing to 90 days trims paid "
                f"interactive retention (~${billable:.0f}/mo of long-tail storage)."
            )
        out.append(_issue(
            res, category="Cost Saving",
            title="Reduce Log Analytics retention",
            issue=f"Log Analytics retention is {retention} days — beyond 31 days you pay per-GB/month for retained data. Reduce or archive if long retention isn't required.",
            fix_commands=["az monitor log-analytics workspace update --resource-group {rg} --workspace-name {name} --retention-time 90"],
            savings_ratio=savings_ratio,
            current_config=f"{retention}-day retention",
            recommended_config="90-day interactive retention (archive older logs)",
            evidence="heuristic", reversible=True,
            doc="loganalytics_retention", reasoning=reasoning,
            keep_at_zero=True,   # real cost lever but ingestion volume unknown → keep
        ))
    if not quota:
        out.append(_issue(
            res, category="Misconfigured",
            title="Set a daily ingestion cap",
            issue="Log Analytics has no daily ingestion cap — a misbehaving source can cause runaway ingestion charges. Set a daily quota as a cost guardrail.",
            fix_commands=["az monitor log-analytics workspace update --resource-group {rg} --workspace-name {name} --quota <GB-per-day>"],
            savings_ratio=0, is_security_only=True,
            evidence="governance", doc="loganalytics_retention",
        ))
    return out


def _rule_firewall(res: dict) -> list[dict]:
    out = []
    tier = res.get("fw_tier") or "Standard"
    if res.get("fw_is_idle"):
        out.append(_issue(
            res, category="Unused / Idle",
            title="Delete idle Azure Firewall",
            issue="Azure Firewall has no IP configuration or no rules — it is deployed but not protecting traffic, at a large fixed hourly cost.",
            fix_commands=["az network firewall delete --resource-group {rg} --name {name}"],
            savings_ratio=_IDLE_DELETE_RATIO,
            current_config=f"{tier} tier, no active rules/IP config",
            recommended_config="Deleted (or attached + rules configured)",
            evidence="measured_fact", destructive=True, reversible=False,
            doc="firewall_idle",
        ))
    elif tier.lower() == "premium":
        out.append(_issue(
            res, category="Wrong Pricing Tier",
            title="Downgrade Firewall Premium → Standard",
            issue="Azure Firewall Premium — if you don't use IDPS, TLS inspection or URL filtering, Standard covers most needs at a lower hourly rate.",
            fix_commands=["# Recreate the firewall on the Standard tier if Premium features aren't used"],
            savings_ratio=round(1 - _FIREWALL_MONTHLY["standard"] / _FIREWALL_MONTHLY["premium"], 2),
            current_config="Premium tier",
            recommended_config="Standard tier",
            evidence="heuristic", reversible=False, doc="firewall_idle",
        ))
    return out


def _rule_vmss(res: dict) -> list[dict]:
    out = []
    size = res.get("vmss_vm_size")
    cap = res.get("vmss_capacity") or 0
    if not size or not cap:
        return out
    nonprod = is_nonproduction(res)
    underused = _cpu_underused(res)

    # Right-size instance count when CPU is genuinely low.
    if underused is True and cap > 1:
        target = max(1, cap // 2)
        out.append(_issue(
            res, category="Overprovisioned",
            title=f"Scale in from {cap} to {target} instances",
            issue=f"VM Scale Set runs {cap} instances but CPU is low — reduce the instance count (or lower the autoscale floor).",
            fix_commands=[f"az vmss scale --resource-group {{rg}} --name {{name}} --new-capacity {target}"],
            savings_ratio=round(1 - target / cap, 2),
            current_config=f"{cap}× {size}",
            recommended_config=f"{target}× {size}",
            evidence="metric_backed", performance_impact=True,
            doc="vm_downsize", exclusive_group="vmss-compute",
            reasoning=f"{cap}× {size}.{_cpu_text(res)} Halving capacity roughly halves compute cost.",
        ))

    # Spot for non-production scale sets.
    if nonprod and not res.get("vmss_spot"):
        prices = _vm_prices(size, res.get("location"))
        od, sp = prices.get("ondemand"), prices.get("spot")
        if od and sp and sp < od:
            out.append(_issue(
                res, category="Cost Saving",
                title="Use Spot instances for the scale set",
                issue="Non-production scale set runs on-demand — Spot priority suits stateless/interruptible workloads at a fraction of the cost.",
                fix_commands=["# Recreate the VMSS with --priority Spot --eviction-policy Delete"],
                savings_ratio=min(1 - sp / od, 0.9),
                current_config=f"{cap}× {size} on-demand",
                recommended_config=f"{cap}× {size} Spot",
                evidence="governance", reversible=False, performance_impact=True,
                doc="vm_spot", exclusive_group="vmss-compute",
            ))
    return out


# Tag keys used for cost allocation / governance. Missing any of these makes a
# billable resource hard to attribute to an owner/environment/cost-centre.
_REQUIRED_TAGS = ("environment", "owner", "costcenter")
# Only bother flagging tags on resource types that actually cost money.
_TAGGABLE_BILLABLE_TYPES = {
    "microsoft.compute/virtualmachines",
    "microsoft.compute/virtualmachinescalesets",
    "microsoft.compute/disks",
    "microsoft.storage/storageaccounts",
    "microsoft.sql/servers/databases",
    "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.dbformysql/flexibleservers",
    "microsoft.web/serverfarms",
    "microsoft.containerservice/managedclusters",
    "microsoft.cache/redis",
    "microsoft.documentdb/databaseaccounts",
}


def _rule_tags(res: dict) -> list[dict]:
    """Cross-cutting governance rule: billable resources missing cost-allocation
    tags. Emitted for every taggable type in addition to its own rules."""
    rtype = (res.get("type") or "").lower()
    if rtype not in _TAGGABLE_BILLABLE_TYPES:
        return []
    tags = {k.lower() for k in (res.get("tags") or {}).keys()}
    missing = [t for t in _REQUIRED_TAGS if t not in tags]
    if not missing:
        return []
    return [_issue(
        res, category="Tagging / Governance",
        title="Add cost-allocation tags",
        issue=f"Missing governance tag(s): {', '.join(missing)}. Untagged spend can't be attributed to an owner/team/environment for showback or budgeting.",
        fix_commands=[
            "az resource tag --ids <resource-id> --tags "
            + " ".join(f"{t}=<value>" for t in missing),
        ],
        savings_ratio=0, is_security_only=True,   # governance → keep at $0, no direct saving
        current_config=f"tags: {', '.join(sorted(tags)) or 'none'}",
        recommended_config=f"add: {', '.join(missing)}",
        evidence="governance", reversible=True, doc="tags_governance",
    )]


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_RULES: dict[str, Callable[[dict], list[dict]]] = {
    "microsoft.compute/virtualmachines": _rule_vm,
    "microsoft.compute/disks": _rule_disk,
    "microsoft.network/publicipaddresses": _rule_public_ip,
    "microsoft.network/networkinterfaces": _rule_nic,
    "microsoft.network/networksecuritygroups": _rule_nsg,
    "microsoft.network/virtualnetworks": _rule_vnet,
    "microsoft.network/loadbalancers": _rule_load_balancer,
    "microsoft.network/applicationgateways": _rule_app_gateway,
    "microsoft.network/natgateways": _rule_nat_gateway,
    "microsoft.network/virtualnetworkgateways": _rule_vpn_gateway,
    "microsoft.storage/storageaccounts": _rule_storage,
    "microsoft.dbforpostgresql/flexibleservers": _rule_postgres_mysql,
    "microsoft.dbformysql/flexibleservers": _rule_postgres_mysql,
    "microsoft.sql/servers/databases": _rule_sql_db,
    "microsoft.cache/redis": _rule_redis,
    "microsoft.keyvault/vaults": _rule_keyvault,
    "microsoft.apimanagement/service": _rule_apim,
    "microsoft.logic/workflows": _rule_logic_app,
    "microsoft.recoveryservices/vaults": _rule_rsv,
    "microsoft.web/serverfarms": _rule_app_service_plan,
    "microsoft.web/sites": _rule_web_app,
    "microsoft.documentdb/databaseaccounts": _rule_cosmos,
    "microsoft.containerservice/managedclusters": _rule_aks,
    "microsoft.containerregistry/registries": _rule_acr,
    "microsoft.servicebus/namespaces": _rule_namespace,
    "microsoft.eventhub/namespaces": _rule_namespace,
    "microsoft.compute/snapshots": _rule_snapshot,
    "microsoft.operationalinsights/workspaces": _rule_log_analytics,
    "microsoft.network/azurefirewalls": _rule_firewall,
    "microsoft.compute/virtualmachinescalesets": _rule_vmss,
}


def detect_issues(enriched_resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Run all deterministic rules over the enriched resource list.
    Returns a list of issue dicts (same schema as ai_analyzer).

    This is guaranteed reproducible: same input always yields the same output.
    """
    findings: list[dict] = []
    for res in enriched_resources:
        rtype = (res.get("type") or "").lower()
        rule_fn = _RULES.get(rtype)
        if rule_fn:
            try:
                findings.extend(rule_fn(res))
            except Exception:
                # A single bad resource must never break the whole engine
                continue
        # Cross-cutting governance rule runs for EVERY resource, in addition to
        # its own type rule.
        try:
            findings.extend(_rule_tags(res))
        except Exception:
            pass
    # Rank by risk-adjusted priority and drop exact duplicates so the highest
    # impact recommendation for each resource surfaces first.
    return rank_and_dedup(findings)
