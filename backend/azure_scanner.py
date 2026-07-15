"""
azure_scanner.py
----------------
Dual-mode Azure scanner:
  • If AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID / AZURE_SUBSCRIPTION_ID
    are all set → uses Azure REST API directly via `requests` (works on any server,
    no SDK namespace issues).
  • Otherwise → falls back to the local `az` CLI (works on dev machines with az login).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AzureCLIError(Exception):
    pass

class AzureCLINotInstalledError(AzureCLIError):
    pass

class AzureNotLoggedInError(AzureCLIError):
    pass

class AzureResourceGroupNotFoundError(AzureCLIError):
    pass


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

def _use_rest() -> bool:
    return bool(
        os.getenv("AZURE_CLIENT_ID", "").strip()
        and os.getenv("AZURE_CLIENT_SECRET", "").strip()
        and os.getenv("AZURE_TENANT_ID", "").strip()
        and os.getenv("AZURE_SUBSCRIPTION_ID", "").strip()
    )


# ---------------------------------------------------------------------------
# REST API path (Render / any server — no SDK needed)
# ---------------------------------------------------------------------------

_ARM_BASE = "https://management.azure.com"
_ARM_SCOPE = "https://management.azure.com/.default"


def _get_access_token() -> str:
    """Obtain a Bearer token via client credentials flow."""
    tenant = os.environ["AZURE_TENANT_ID"]
    client_id = os.environ["AZURE_CLIENT_ID"]
    client_secret = os.environ["AZURE_CLIENT_SECRET"]

    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    resp = requests.post(url, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": _ARM_SCOPE,
    }, timeout=15)

    if resp.status_code != 200:
        body = resp.json()
        err = body.get("error_description", resp.text)
        if "AADSTS" in err:
            raise AzureNotLoggedInError(f"Azure authentication failed: {err}")
        raise AzureCLIError(f"Token request failed: {err}")

    return resp.json()["access_token"]


def _arm_get(path: str, token: str, api_version: str, params: dict | None = None) -> Any:
    url = f"{_ARM_BASE}{path}"
    p = {"api-version": api_version, **(params or {})}
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=p, timeout=20)
    if resp.status_code == 401:
        raise AzureNotLoggedInError("Invalid or expired token.")
    if resp.status_code == 404:
        raise AzureResourceGroupNotFoundError(f"Not found: {path}")
    if not resp.ok:
        raise AzureCLIError(f"ARM GET {path} failed {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _arm_get_all(path: str, token: str, api_version: str) -> list[dict]:
    """Fetch all pages from a paginated ARM list endpoint."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{_ARM_BASE}{path}"
    params = {"api-version": api_version}
    items: list[dict] = []

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code == 401:
            raise AzureNotLoggedInError("Invalid or expired token.")
        if resp.status_code == 404:
            raise AzureResourceGroupNotFoundError(f"Not found: {path}")
        if not resp.ok:
            raise AzureCLIError(f"ARM GET {path} failed {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        items.extend(data.get("value", []))
        # nextLink is a full URL — don't append api-version again
        url = data.get("nextLink")
        params = {}   # nextLink already contains all query params

    return items


def _metric_latest_average(rid: str, token: str, metric: str) -> float | None:
    """Fetch the most recent daily-average value of an Azure Monitor metric.

    Used for usage-based resources (e.g. storage UsedCapacity) where the real
    cost depends on consumption rather than a fixed SKU price. Returns None on
    any failure so a metric hiccup never breaks the scan.
    """
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=2)
        data = _arm_get(
            f"{rid}/providers/microsoft.insights/metrics",
            token,
            "2023-10-01",
            params={
                "metricnames": metric,
                "aggregation": "Average",
                "interval": "P1D",
                "timespan": f"{start:%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}",
            },
        )
        values = data.get("value", [])
        if not values:
            return None
        series = values[0].get("timeseries", [])
        if not series:
            return None
        latest = None
        for point in series[0].get("data", []):
            if point.get("average") is not None:
                latest = point["average"]   # keep last non-null (most recent day)
        return latest
    except Exception:
        return None


def _metric_stats(rid: str, token: str, metric: str, days: int = 14) -> dict | None:
    """Return {'avg': x, 'max': y} for an Azure Monitor metric over the last `days`.

    Used to make right-sizing recommendations EVIDENCE-BASED: e.g. only suggest
    downsizing a VM when its measured CPU is genuinely low. Returns None if the
    metric is unavailable (e.g. brand-new resource) so callers can fall back.
    """
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        data = _arm_get(
            f"{rid}/providers/microsoft.insights/metrics",
            token,
            "2023-10-01",
            params={
                "metricnames": metric,
                "aggregation": "Average,Maximum",
                "interval": "P1D",
                "timespan": f"{start:%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}",
            },
        )
        values = data.get("value", [])
        if not values:
            return None
        series = values[0].get("timeseries", [])
        if not series:
            return None
        points = series[0].get("data", [])
        avgs = [p["average"] for p in points if p.get("average") is not None]
        maxs = [p["maximum"] for p in points if p.get("maximum") is not None]
        if not avgs and not maxs:
            return None
        return {
            "avg": round(sum(avgs) / len(avgs), 1) if avgs else None,
            "max": round(max(maxs), 1) if maxs else None,
        }
    except Exception:
        return None


# Metric name to read CPU utilisation, keyed by resource type. Used to gate
# right-size / tier-downgrade recommendations on real usage.
# NOTE: every type here MUST have a rule that actually consumes cpu_avg_pct /
# cpu_max_pct (rules_engine), otherwise we pay for a metrics call we never use.
# Redis is intentionally absent: its Premium→Standard decision is feature-based
# (persistence / clustering / geo-replication), not CPU-based.
_CPU_METRIC_BY_TYPE: dict[str, str] = {
    "microsoft.compute/virtualmachines": "Percentage CPU",
    "microsoft.compute/virtualmachinescalesets": "Percentage CPU",
    "microsoft.dbforpostgresql/flexibleservers": "cpu_percent",
    "microsoft.dbformysql/flexibleservers": "cpu_percent",
    "microsoft.sql/servers/databases": "cpu_percent",
    "microsoft.web/serverfarms": "CpuPercentage",
}


def _metric_windows(rid: str, token: str, metric: str) -> dict | None:
    """Fetch ONE 90-day daily series and derive 7/30/90-day avg+max windows and
    a utilisation trend. A single API call powers all three windows, which the
    recommendation engine uses to raise confidence on longer, stable evidence.
    """
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=90)
        data = _arm_get(
            f"{rid}/providers/microsoft.insights/metrics",
            token,
            "2023-10-01",
            params={
                "metricnames": metric,
                "aggregation": "Average,Maximum",
                "interval": "P1D",
                "timespan": f"{start:%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}",
            },
        )
        values = data.get("value", [])
        series = (values[0].get("timeseries", []) if values else [])
        points = series[0].get("data", []) if series else []
        # oldest→newest daily points, each may have average/maximum
        avgs = [p.get("average") for p in points]
        maxs = [p.get("maximum") for p in points]

        def _win(vals: list, agg) -> float | None:
            clean = [v for v in vals if v is not None]
            return round(agg(clean), 1) if clean else None

        out: dict = {}
        for days in (7, 30, 90):
            a = _win(avgs[-days:], lambda x: sum(x) / len(x))
            m = _win(maxs[-days:], max)
            if a is not None:
                out[f"cpu_avg_{days}d"] = a
            if m is not None:
                out[f"cpu_max_{days}d"] = m
        if not out:
            return None
        # Trend: last-7d avg vs the previous 7d (positive = rising utilisation).
        recent = [v for v in avgs[-7:] if v is not None]
        prior = [v for v in avgs[-14:-7] if v is not None]
        if recent and prior:
            r, p = sum(recent) / len(recent), sum(prior) / len(prior)
            out["cpu_trend"] = ("rising" if r > p * 1.15 else
                                "falling" if r < p * 0.85 else "stable")
        return out
    except Exception:
        return None


def _enrich_cpu(rid: str, token: str, rtype: str, extra: dict) -> None:
    """Attach CPU utilisation windows (7/30/90d) for compute/DB resources.

    cpu_avg_pct / cpu_max_pct remain as the primary gating signal (30-day window
    if available, else the widest we have) so existing rules keep working, while
    the per-window fields feed confidence scoring in the recommendation engine.
    """
    metric = _CPU_METRIC_BY_TYPE.get(rtype)
    if not metric:
        return
    windows = _metric_windows(rid, token, metric)
    if not windows:
        return
    extra.update(windows)
    # Primary signal for rule gating: prefer the 30-day window, fall back.
    extra["cpu_avg_pct"] = (windows.get("cpu_avg_30d")
                            or windows.get("cpu_avg_7d")
                            or windows.get("cpu_avg_90d"))
    extra["cpu_max_pct"] = (windows.get("cpu_max_30d")
                            or windows.get("cpu_max_7d")
                            or windows.get("cpu_max_90d"))


_fx_cache: dict[str, float] = {}  # e.g. {"INR": 0.01163}

def _to_usd(amount: float, currency: str) -> float:
    """Convert any currency amount to USD. Falls back to 1:1 if conversion fails."""
    currency = currency.upper().strip()
    if currency in ("USD", ""):
        return amount
    if currency in _fx_cache:
        return round(amount * _fx_cache[currency], 6)
    try:
        resp = requests.get(
            f"https://api.frankfurter.app/latest?from={currency}&to=USD",
            timeout=8,
        )
        if resp.ok:
            rate = resp.json()["rates"]["USD"]
            _fx_cache[currency] = rate
            return round(amount * rate, 6)
    except Exception:
        pass
    # Hard-coded fallback rates (approximate) in case the FX API is down
    _FALLBACK: dict[str, float] = {
        "INR": 0.01163, "EUR": 1.08, "GBP": 1.27, "AUD": 0.65,
        "CAD": 0.73, "JPY": 0.0067, "SGD": 0.74, "AED": 0.272,
        "BRL": 0.178, "MXN": 0.052, "KRW": 0.00072,
    }
    rate = _FALLBACK.get(currency, 1.0)
    _fx_cache[currency] = rate
    return round(amount * rate, 6)


def _arm_post(path: str, token: str, api_version: str, body: dict) -> Any:
    url = f"{_ARM_BASE}{path}"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"api-version": api_version},
        json=body,
        timeout=20,
    )
    if not resp.ok:
        raise AzureCLIError(f"ARM POST {path} failed {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _rest_list_subscriptions() -> list[dict]:
    """Return all subscriptions visible to the service principal."""
    token = _get_access_token()
    subs = _arm_get_all("/subscriptions", token, "2022-12-01")
    return [
        {
            "subscription_id": s.get("subscriptionId", ""),
            "display_name": s.get("displayName", ""),
            "state": s.get("state", ""),
            "tenant_id": s.get("tenantId", ""),
        }
        for s in subs
        if s.get("state", "").lower() == "enabled"
    ]


def _rest_list_resource_groups(subscription_id: str | None = None) -> list[dict]:
    token = _get_access_token()
    sub = subscription_id or os.environ["AZURE_SUBSCRIPTION_ID"]
    all_rgs = _arm_get_all(f"/subscriptions/{sub}/resourcegroups", token, "2021-04-01")
    return [
        {
            "name": rg.get("name", ""),
            "location": rg.get("location", ""),
            "tags": rg.get("tags") or {},
            "provisioning_state": rg.get("properties", {}).get("provisioningState", ""),
            "subscription_id": sub,
        }
        for rg in all_rgs
    ]


def _enrich_resource(res: dict, token: str) -> dict:
    """Fetch deeper properties for specific resource types.
    Failures are silently ignored so the overall scan never aborts."""
    rtype = res.get("type", "").lower()
    rid = res.get("id", "")
    extra: dict = {}
    try:
        if rtype == "microsoft.compute/virtualmachines":
            iv = _arm_get(f"{rid}/instanceView", token, "2023-03-01")
            statuses = iv.get("statuses", [])
            power = next(
                (s.get("displayStatus") for s in statuses
                 if s.get("code", "").startswith("PowerState/")),
                None,
            )
            extra["power_state"] = power           # "VM running" | "VM deallocated" | etc.
            extra["is_running"] = power == "VM running" if power else None

            # Check if any NIC on this VM has a public IP directly attached —
            # this exposes SSH/RDP etc. straight to the internet.
            try:
                vm_full = _arm_get(rid, token, "2023-09-01")
                vm_props = vm_full.get("properties", {})
                extra["vm_size"] = (
                    vm_props.get("hardwareProfile", {}).get("vmSize")
                )
                # OS + license: Windows VM with no licenseType = paying full
                # license, so Azure Hybrid Benefit can apply.
                extra["vm_os_type"] = (
                    vm_props.get("storageProfile", {}).get("osDisk", {}).get("osType")
                )
                extra["vm_license_type"] = vm_props.get("licenseType")
                nic_refs = (vm_full.get("properties", {})
                            .get("networkProfile", {})
                            .get("networkInterfaces", []))
                direct_public_ips = []
                open_mgmt_ports = []
                for nic_ref in nic_refs:
                    nic_id = nic_ref.get("id", "")
                    if not nic_id:
                        continue
                    nic = _arm_get(nic_id, token, "2023-09-01")
                    for ipcfg in nic.get("properties", {}).get("ipConfigurations", []):
                        pip_ref = ipcfg.get("properties", {}).get("publicIPAddress")
                        if pip_ref and pip_ref.get("id"):
                            direct_public_ips.append(pip_ref["id"].split("/")[-1])
                    # Check the NIC's attached NSG (if any) for open mgmt ports
                    nsg_ref = nic.get("properties", {}).get("networkSecurityGroup")
                    if nsg_ref and nsg_ref.get("id") and direct_public_ips:
                        try:
                            nsg = _arm_get(nsg_ref["id"], token, "2023-09-01")
                            for rule in nsg.get("properties", {}).get("securityRules", []):
                                rp = rule.get("properties", {})
                                port = rp.get("destinationPortRange", "")
                                src = rp.get("sourceAddressPrefix", "")
                                if (
                                    rp.get("direction") == "Inbound"
                                    and rp.get("access") == "Allow"
                                    and port in ("22", "3389", "*")
                                    and src in ("*", "Internet", "Any")
                                ):
                                    open_mgmt_ports.append(port)
                        except Exception:
                            pass

                extra["direct_public_ip_attached"] = bool(direct_public_ips)
                extra["direct_public_ip_names"] = direct_public_ips
                extra["mgmt_ports_open_to_internet"] = list(set(open_mgmt_ports))
            except Exception:
                pass

        elif rtype == "microsoft.compute/disks":
            disk = _arm_get(rid, token, "2023-04-02")
            props = disk.get("properties", {})
            extra["disk_state"] = props.get("diskState")   # Attached / Unattached / Reserved
            extra["is_orphaned"] = not bool(disk.get("managedBy"))
            extra["disk_size_gb"] = props.get("diskSizeGB")
            extra["disk_sku"] = (disk.get("sku") or {}).get("name")

        elif rtype == "microsoft.network/publicipaddresses":
            pip = _arm_get(rid, token, "2023-09-01")
            props = pip.get("properties", {})
            extra["is_attached"] = bool(props.get("ipConfiguration"))
            extra["ip_address"] = props.get("ipAddress")
            extra["allocation_method"] = props.get("publicIPAllocationMethod")
            extra["sku_name"] = (pip.get("sku") or {}).get("name")  # Basic vs Standard
            extra["ddos_protection_mode"] = props.get("ddosSettings", {}).get("protectionMode")
            extra["dns_label"] = (props.get("dnsSettings") or {}).get("domainNameLabel")
            extra["idle_timeout_minutes"] = props.get("idleTimeoutInMinutes")

        # ── Network Interfaces (NICs) ────────────────────────────────────────
        elif rtype == "microsoft.network/networkinterfaces":
            nic = _arm_get(rid, token, "2023-09-01")
            props = nic.get("properties", {})
            extra["nic_is_attached"] = bool(props.get("virtualMachine"))
            extra["nic_vm_id"] = (props.get("virtualMachine") or {}).get("id", "").split("/")[-1] or None
            extra["nic_nsg_attached"] = bool(props.get("networkSecurityGroup"))
            extra["nic_accelerated_networking"] = props.get("enableAcceleratedNetworking", False)
            extra["nic_ip_forwarding"] = props.get("enableIPForwarding", False)  # Security concern if unexpected
            ip_configs = props.get("ipConfigurations", [])
            extra["nic_ip_config_count"] = len(ip_configs)
            if ip_configs:
                first_cfg = ip_configs[0].get("properties", {})
                extra["nic_private_ip"] = first_cfg.get("privateIPAddress")
                extra["nic_private_ip_alloc"] = first_cfg.get("privateIPAllocationMethod")  # Static|Dynamic
                extra["nic_has_public_ip"] = bool(first_cfg.get("publicIPAddress"))
            extra["nic_dns_servers"] = props.get("dnsSettings", {}).get("dnsServers", [])

        elif rtype in (
            "microsoft.dbforpostgresql/flexibleservers",
            "microsoft.dbformysql/flexibleservers",
        ):
            server = _arm_get(rid, token, "2022-12-01")
            props = server.get("properties", {})
            sku = server.get("sku") or {}
            extra["compute_tier"] = sku.get("tier")      # Burstable / GeneralPurpose / MemoryOptimized
            extra["vm_size"] = sku.get("name")
            extra["storage_gb"] = (props.get("storage") or {}).get("storageSizeGB")
            extra["backup_retention_days"] = (props.get("backup") or {}).get("backupRetentionDays")
            extra["ha_mode"] = (props.get("highAvailability") or {}).get("mode")
            extra["db_state"] = props.get("state")

        elif rtype == "microsoft.storage/storageaccounts":
            sa = _arm_get(rid, token, "2023-01-01")
            props = sa.get("properties", {})
            sku = sa.get("sku") or {}
            extra["storage_sku"] = sku.get("name")       # Standard_LRS / Premium_LRS etc.
            extra["access_tier"] = props.get("accessTier")  # Hot / Cool
            extra["kind"] = sa.get("kind")
            extra["https_only"] = props.get("supportsHttpsTrafficOnly")

            # Usage-based cost basis: pull real stored capacity from Azure Monitor
            # so we can price the account even when there is no billing data yet.
            cap_bytes = _metric_latest_average(rid, token, "UsedCapacity")
            extra["used_capacity_gb"] = (
                round(cap_bytes / (1024 ** 3), 2) if cap_bytes is not None else None
            )

            # Security posture
            extra["public_network_access"] = props.get("publicNetworkAccess")   # "Enabled"|"Disabled"
            extra["allow_blob_public_access"] = props.get("allowBlobPublicAccess")
            extra["min_tls_version"] = props.get("minimumTlsVersion")           # "TLS1_0"|"TLS1_1"|"TLS1_2"
            network_acls = props.get("networkAcls", {}) or {}
            extra["network_default_action"] = network_acls.get("defaultAction")  # "Allow"=open to all networks
            extra["network_ip_rules_count"] = len(network_acls.get("ipRules", []))
            extra["network_vnet_rules_count"] = len(network_acls.get("virtualNetworkRules", []))

            # Blob soft delete / versioning — cost & resilience config
            try:
                blob_svc = _arm_get(f"{rid}/blobServices/default", token, "2023-01-01")
                blob_props = blob_svc.get("properties", {})
                extra["blob_soft_delete_enabled"] = (
                    blob_props.get("deleteRetentionPolicy", {}).get("enabled", False)
                )
                extra["blob_soft_delete_days"] = (
                    blob_props.get("deleteRetentionPolicy", {}).get("days")
                )
                extra["blob_versioning_enabled"] = blob_props.get("isVersioningEnabled", False)
            except Exception:
                pass

            # Container count — helps flag storage accounts with zero containers (likely unused)
            try:
                containers = _arm_get_all(f"{rid}/blobServices/default/containers", token, "2023-01-01")
                extra["blob_container_count"] = len(containers)
                extra["is_empty"] = len(containers) == 0
            except Exception:
                extra["blob_container_count"] = None
                extra["is_empty"] = None

            # Lifecycle management policy — auto-tiering to Cool/Archive saves money
            try:
                lifecycle = _arm_get(f"{rid}/managementPolicies/default", token, "2023-01-01")
                rules = lifecycle.get("properties", {}).get("policy", {}).get("rules", [])
                extra["has_lifecycle_policy"] = len(rules) > 0
                extra["lifecycle_rule_count"] = len(rules)
            except Exception:
                extra["has_lifecycle_policy"] = False

            # Queue service — detect unused queues
            try:
                queues = _arm_get_all(f"{rid}/queueServices/default/queues", token, "2023-01-01")
                extra["queue_count"] = len(queues)
                # Check queue logging setting
                q_svc = _arm_get(f"{rid}/queueServices/default", token, "2023-01-01")
                q_logging = (q_svc.get("properties", {}).get("cors") or {})
                extra["queue_logging_enabled"] = bool(q_logging)
            except Exception:
                extra["queue_count"] = None

            # File shares — detect over-provisioned or empty shares
            try:
                shares = _arm_get_all(f"{rid}/fileServices/default/shares", token, "2023-01-01")
                extra["file_share_count"] = len(shares)
                if shares:
                    total_quota_gb = sum(
                        s.get("properties", {}).get("shareQuota", 0) for s in shares
                    )
                    extra["file_share_total_quota_gb"] = total_quota_gb
                    # Premium file shares: check for large quota vs actual usage
                    extra["file_share_large_quota"] = total_quota_gb > 1024  # > 1 TB
            except Exception:
                extra["file_share_count"] = None

            # Table service — detect usage
            try:
                tables = _arm_get_all(f"{rid}/tableServices/default/tables", token, "2023-01-01")
                extra["table_count"] = len(tables)
            except Exception:
                extra["table_count"] = None

            # Immutability / WORM policy — can prevent blob tiering
            extra["immutable_storage"] = bool(props.get("immutableStorageWithVersioning", {}).get("enabled", False))

        elif rtype == "microsoft.network/networksecuritygroups":
            nsg = _arm_get(rid, token, "2023-09-01")
            props = nsg.get("properties", {})
            extra["associated_subnet_count"] = len(props.get("subnets", []))
            extra["associated_nic_count"] = len(props.get("networkInterfaces", []))
            extra["is_associated"] = bool(
                props.get("subnets") or props.get("networkInterfaces")
            )
            open_rules = []
            risky_port_rules = []
            for rule in props.get("securityRules", []):
                rp = rule.get("properties", {})
                port = rp.get("destinationPortRange", "")
                src  = rp.get("sourceAddressPrefix", "")
                if (
                    rp.get("direction") == "Inbound"
                    and rp.get("access") == "Allow"
                    and port == "*"
                    and src in ("*", "Internet", "Any")
                ):
                    open_rules.append(rule.get("name", "unknown"))
                if (
                    rp.get("direction") == "Inbound"
                    and rp.get("access") == "Allow"
                    and port in ("22", "3389", "5986", "5985")
                    and src in ("*", "Internet", "Any")
                ):
                    risky_port_rules.append({"port": port, "rule": rule.get("name")})
            extra["open_inbound_rules"] = open_rules
            extra["risky_port_rules"] = risky_port_rules       # SSH/RDP/WinRM open to internet
            extra["total_inbound_rules"] = len([
                r for r in props.get("securityRules", [])
                if r.get("properties", {}).get("direction") == "Inbound"
            ])

        # ── Web Apps & Function Apps ─────────────────────────────────────────
        elif rtype == "microsoft.web/sites":
            site = _arm_get(rid, token, "2023-01-01")
            props = site.get("properties", {})
            site_cfg = props.get("siteConfig") or {}
            kind = (site.get("kind") or "").lower()

            extra["app_kind"] = site.get("kind")
            extra["state"] = props.get("state")
            extra["is_running"] = props.get("state") == "Running"
            extra["always_on"] = site_cfg.get("alwaysOn")
            extra["app_service_plan"] = props.get("serverFarmId", "").split("/")[-1]
            extra["https_only"] = props.get("httpsOnly")
            extra["min_tls_version"] = site_cfg.get("minTlsVersion")
            extra["http20_enabled"] = site_cfg.get("http20Enabled")
            extra["client_cert_enabled"] = props.get("clientCertEnabled")

            # Number of deployment slots (each slot = paid compute)
            try:
                slots = _arm_get_all(f"{rid}/slots", token, "2023-01-01")
                extra["deployment_slot_count"] = len(slots)
            except Exception:
                extra["deployment_slot_count"] = 0

            if "functionapp" in kind:
                extra["function_runtime_name"] = site_cfg.get("functionAppScaleLimit")
                extra["function_language"] = (
                    site_cfg.get("pythonVersion") or
                    site_cfg.get("nodeVersion") or
                    site_cfg.get("javaVersion") or
                    site_cfg.get("netFrameworkVersion")
                )
                extra["function_scale_limit"] = site_cfg.get("functionAppScaleLimit")
                # Hosting plan type affects billing model significantly
                server_farm_id = props.get("serverFarmId", "")
                extra["is_consumption_plan"] = "dynamicPlan" in server_farm_id.lower() or not server_farm_id
            else:
                # Web app specific
                extra["custom_domain_count"] = len(props.get("hostNames", [])) - 1  # subtract default
                extra["health_check_path"] = site_cfg.get("healthCheckPath")

        # ── App Service Plans ────────────────────────────────────────────────
        elif rtype == "microsoft.web/serverfarms":
            plan = _arm_get(rid, token, "2023-01-01")
            props = plan.get("properties", {})
            sku = plan.get("sku") or {}
            extra["asp_sku_name"] = sku.get("name")        # "F1","B1","S1","P1v2","P2v3" etc.
            extra["asp_sku_tier"] = sku.get("tier")        # "Free","Basic","Standard","Premium"
            extra["asp_workers"] = sku.get("capacity")     # number of workers provisioned
            extra["asp_number_of_sites"] = props.get("numberOfSites", 0)
            extra["asp_is_dev_test"] = "dev" in sku.get("name", "").lower()
            extra["asp_os"] = "Linux" if props.get("reserved") else "Windows"

        # ── Azure SQL Database ───────────────────────────────────────────────
        elif rtype == "microsoft.sql/servers/databases":
            db = _arm_get(rid, token, "2023-05-01-preview")
            props = db.get("properties", {})
            sku = db.get("sku") or {}
            extra["sql_sku_name"] = sku.get("name")          # "GP_S_Gen5_2" etc.
            extra["sql_tier"] = sku.get("tier")              # "GeneralPurpose","BusinessCritical"
            extra["sql_max_size_gb"] = round(props.get("maxSizeBytes", 0) / (1024 ** 3), 1)
            extra["sql_status"] = props.get("status")        # "Online" | "Paused"
            extra["sql_zone_redundant"] = props.get("zoneRedundant")
            extra["sql_backup_storage_type"] = props.get("requestedBackupStorageRedundancy")

        # ── Azure Cosmos DB ──────────────────────────────────────────────────
        elif rtype == "microsoft.documentdb/databaseaccounts":
            cosmos = _arm_get(rid, token, "2023-04-15")
            props = cosmos.get("properties", {})
            extra["cosmos_kind"] = cosmos.get("kind")          # "GlobalDocumentDB"|"MongoDB"
            extra["cosmos_consistency"] = (props.get("consistencyPolicy") or {}).get("defaultConsistencyLevel")
            extra["cosmos_locations"] = [
                loc.get("locationName") for loc in props.get("locations", [])
            ]
            extra["cosmos_multi_region"] = len(extra["cosmos_locations"]) > 1
            extra["cosmos_backup_type"] = (props.get("backupPolicy") or {}).get("type")

        # ── Azure Cache for Redis ────────────────────────────────────────────
        elif rtype == "microsoft.cache/redis":
            redis = _arm_get(rid, token, "2023-08-01")
            props = redis.get("properties", {})
            sku = redis.get("sku") or {}
            extra["redis_sku_name"] = sku.get("name")         # "Basic"|"Standard"|"Premium"
            extra["redis_capacity"] = sku.get("capacity")     # 0=250MB … 6=53GB
            extra["redis_ssl_port"] = props.get("sslPort")
            extra["redis_non_ssl_enabled"] = not props.get("enableNonSslPort", True)

        # ── AKS (Kubernetes Service) ─────────────────────────────────────────
        elif rtype == "microsoft.containerservice/managedclusters":
            aks = _arm_get(rid, token, "2024-01-01")
            props = aks.get("properties", {})
            pools = props.get("agentPoolProfiles", [])
            extra["aks_k8s_version"] = props.get("kubernetesVersion")
            extra["aks_node_pools"] = [
                {
                    "name": p.get("name"),
                    "vm_size": p.get("vmSize"),
                    "count": p.get("count"),
                    "min_count": p.get("minCount"),
                    "max_count": p.get("maxCount"),
                    "mode": p.get("mode"),      # "System"|"User"
                    "os_type": p.get("osType"),
                    "spot": p.get("scaleSetPriority") == "Spot",
                }
                for p in pools
            ]
            extra["aks_total_nodes"] = sum(p.get("count") or 0 for p in pools)
            extra["aks_sku_tier"] = (aks.get("sku") or {}).get("tier")   # "Free"|"Standard"|"Premium"

        # ── Container Registry ───────────────────────────────────────────────
        elif rtype == "microsoft.containerregistry/registries":
            acr = _arm_get(rid, token, "2023-01-01-preview")
            props = acr.get("properties", {})
            sku = acr.get("sku") or {}
            extra["acr_sku"] = sku.get("name")          # "Basic"|"Standard"|"Premium"
            extra["acr_storage_used_gb"] = round((props.get("storageAccount") or {}).get("name", 0) or 0, 2)
            extra["acr_admin_enabled"] = props.get("adminUserEnabled")
            extra["acr_public_access"] = props.get("publicNetworkAccess")

        # ── Key Vault ────────────────────────────────────────────────────────
        elif rtype == "microsoft.keyvault/vaults":
            kv = _arm_get(rid, token, "2023-07-01")
            props = kv.get("properties", {})
            extra["kv_sku_name"] = (props.get("sku") or {}).get("name")          # "standard"|"premium"
            extra["kv_soft_delete_enabled"] = props.get("enableSoftDelete", True)
            extra["kv_soft_delete_days"] = props.get("softDeleteRetentionInDays")  # default 90
            extra["kv_purge_protection"] = props.get("enablePurgeProtection", False)
            extra["kv_rbac_authorization"] = props.get("enableRbacAuthorization", False)
            extra["kv_public_access"] = props.get("publicNetworkAccess")          # "Enabled"|"Disabled"

            network_acls = props.get("networkAcls") or {}
            extra["kv_network_default_action"] = network_acls.get("defaultAction")  # "Allow"|"Deny"
            extra["kv_network_ip_rules"] = len(network_acls.get("ipRules", []))
            extra["kv_network_vnet_rules"] = len(network_acls.get("virtualNetworkRules", []))

            # Access policies — more than 16 is a signal to move to RBAC
            access_policies = props.get("accessPolicies", [])
            extra["kv_access_policy_count"] = len(access_policies)
            extra["kv_wide_permissions"] = any(
                set(ap.get("permissions", {}).get("secrets", [])) & {"all", "purge"}
                for ap in access_policies
            )

            # Key/secret counts (Data Plane API — may 403 if SP lacks access)
            kv_name = kv.get("name", "")
            kv_base = f"https://{kv_name}.vault.azure.net"
            _kv_headers = {"Authorization": f"Bearer {token}"}
            try:
                r = _requests.get(f"{kv_base}/keys?api-version=7.4&maxresults=25", headers=_kv_headers, timeout=8)
                if r.ok:
                    extra["kv_key_count"] = len(r.json().get("value", []))
            except Exception:
                pass
            try:
                r = _requests.get(f"{kv_base}/secrets?api-version=7.4&maxresults=25", headers=_kv_headers, timeout=8)
                if r.ok:
                    extra["kv_secret_count"] = len(r.json().get("value", []))
            except Exception:
                pass
            try:
                r = _requests.get(f"{kv_base}/certificates?api-version=7.4&maxresults=25", headers=_kv_headers, timeout=8)
                if r.ok:
                    certs = r.json().get("value", [])
                    extra["kv_certificate_count"] = len(certs)
                    # Check for expiring certs (within 60 days)
                    from datetime import datetime, timezone
                    now_ts = datetime.now(timezone.utc).timestamp()
                    expiring = [
                        c.get("id", "").split("/")[-1]
                        for c in certs
                        if c.get("attributes", {}).get("exp") and
                           c["attributes"]["exp"] - now_ts < 60 * 86400
                    ]
                    extra["kv_expiring_certs"] = expiring
            except Exception:
                pass

            # Operation volume — Key Vault is billed per transaction, so a very
            # high hit count is the main cost signal. Sum ServiceApiHit over 30d.
            try:
                now = datetime.now(timezone.utc)
                start = now - timedelta(days=30)
                mdata = _arm_get(
                    f"{rid}/providers/microsoft.insights/metrics", token, "2023-10-01",
                    params={
                        "metricnames": "ServiceApiHit",
                        "aggregation": "Total",
                        "interval": "P1D",
                        "timespan": f"{start:%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}",
                    },
                )
                vals = mdata.get("value", [])
                series = (vals[0].get("timeseries", []) if vals else [])
                pts = series[0].get("data", []) if series else []
                total_hits = sum(p.get("total") or 0 for p in pts)
                extra["kv_api_hits_30d"] = int(total_hits) if pts else None
            except Exception:
                extra["kv_api_hits_30d"] = None

        # ── API Management ───────────────────────────────────────────────────
        elif rtype == "microsoft.apimanagement/service":
            apim = _arm_get(rid, token, "2023-05-01-preview")
            props = apim.get("properties", {})
            sku   = apim.get("sku") or {}
            extra["apim_sku_name"] = sku.get("name")          # "Developer"|"Basic"|"Standard"|"Premium"|"Consumption"
            extra["apim_sku_capacity"] = sku.get("capacity")  # Number of gateway units
            extra["apim_vnet_type"] = props.get("virtualNetworkType")   # "None"|"External"|"Internal"
            extra["apim_gateway_url"] = props.get("gatewayUrl")
            extra["apim_portal_url"] = props.get("developerPortalUrl")
            extra["apim_provisioning_state"] = props.get("provisioningState")
            extra["apim_multi_region"] = len(props.get("additionalLocations", [])) > 0
            extra["apim_additional_region_count"] = len(props.get("additionalLocations", []))

            # Count APIs deployed (data plane API)
            try:
                apis = _arm_get_all(f"{rid}/apis", token, "2023-05-01-preview")
                extra["apim_api_count"] = len(apis)
                extra["apim_is_empty"] = len(apis) == 0
            except Exception:
                extra["apim_api_count"] = None

            # Count named values, products, subscriptions
            try:
                subs_list = _arm_get_all(f"{rid}/subscriptions", token, "2023-05-01-preview")
                extra["apim_subscription_count"] = len(subs_list)
            except Exception:
                pass

        # ── Load Balancer ────────────────────────────────────────────────────
        elif rtype == "microsoft.network/loadbalancers":
            lb = _arm_get(rid, token, "2023-09-01")
            sku = lb.get("sku") or {}
            props = lb.get("properties", {})
            extra["lb_sku"] = sku.get("name")          # "Basic"|"Standard"
            extra["lb_frontend_count"] = len(props.get("frontendIPConfigurations", []))
            extra["lb_backend_count"] = len(props.get("backendAddressPools", []))
            extra["lb_lb_rule_count"] = len(props.get("loadBalancingRules", []))
            extra["lb_probe_count"] = len(props.get("probes", []))
            extra["lb_nat_rule_count"] = len(props.get("inboundNatRules", []))
            extra["lb_is_unused"] = (
                extra["lb_frontend_count"] == 0 or extra["lb_backend_count"] == 0
            )

        # ── Service Bus / Event Hubs ─────────────────────────────────────────
        elif rtype in ("microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces"):
            ns = _arm_get(rid, token, "2022-10-01-preview")
            sku = ns.get("sku") or {}
            props = ns.get("properties", {})
            extra["ns_sku_name"] = sku.get("name")      # "Basic"|"Standard"|"Premium"
            extra["ns_capacity"] = sku.get("capacity")
            extra["ns_status"] = props.get("status")

        # ── Logic Apps ───────────────────────────────────────────────────────
        elif rtype == "microsoft.logic/workflows":
            logic = _arm_get(rid, token, "2019-05-01")
            props = logic.get("properties", {})
            defn  = props.get("definition", {})
            extra["logic_state"] = props.get("state")
            extra["logic_is_disabled"] = props.get("state") == "Disabled"
            extra["logic_action_count"] = len(defn.get("actions", {}))
            extra["logic_trigger_count"] = len(defn.get("triggers", {}))
            # Trigger type — Recurrence triggers cost per execution
            triggers = defn.get("triggers", {})
            if triggers:
                first_trigger = next(iter(triggers.values()), {})
                extra["logic_trigger_type"] = first_trigger.get("type")  # "Recurrence"|"Request"|"ApiConnection"
                recurrence = first_trigger.get("recurrence", {})
                extra["logic_trigger_frequency"] = recurrence.get("frequency")  # "Minute"|"Hour"|"Day"
                extra["logic_trigger_interval"] = recurrence.get("interval")
            extra["logic_sku_name"] = (props.get("sku") or {}).get("name")   # "Free"|"Standard"
            extra["logic_integration_account"] = bool(props.get("integrationAccount"))
            # Endpoint config
            extra["logic_endpoint_access_control"] = bool(
                (props.get("accessControl") or {}).get("triggers")
            )

        # ── Application Gateway ──────────────────────────────────────────────
        elif rtype == "microsoft.network/applicationgateways":
            agw = _arm_get(rid, token, "2023-09-01")
            props = agw.get("properties", {})
            sku   = props.get("sku") or {}
            waf   = props.get("webApplicationFirewallConfiguration") or {}
            autoscale = props.get("autoscaleConfiguration") or {}

            extra["agw_sku_name"] = sku.get("name")
            extra["agw_tier"] = sku.get("tier")
            extra["agw_capacity"] = sku.get("capacity")        # None if autoscale
            extra["agw_autoscale"] = bool(props.get("autoscaleConfiguration"))
            extra["agw_autoscale_min"] = autoscale.get("minCapacity")
            extra["agw_autoscale_max"] = autoscale.get("maxCapacity")

            extra["agw_waf_enabled"] = waf.get("enabled", False)
            extra["agw_waf_mode"] = waf.get("firewallMode")    # "Detection"|"Prevention"
            extra["agw_waf_ruleset_version"] = waf.get("ruleSetVersion")

            extra["agw_frontend_count"] = len(props.get("frontendIPConfigurations", []))
            extra["agw_backend_pool_count"] = len(props.get("backendAddressPools", []))
            extra["agw_listener_count"] = len(props.get("httpListeners", []))
            extra["agw_rule_count"] = len(props.get("requestRoutingRules", []))

            ssl_policy = props.get("sslPolicy") or {}
            extra["agw_ssl_policy_name"] = ssl_policy.get("policyName")
            extra["agw_ssl_min_protocol"] = ssl_policy.get("minProtocolVersion")   # "TLSv1_0"|"TLSv1_2"

            extra["agw_is_empty"] = (
                extra["agw_backend_pool_count"] == 0 or extra["agw_rule_count"] == 0
            )

        # ── Recovery Services Vault (RSV) ────────────────────────────────────
        elif rtype == "microsoft.recoveryservices/vaults":
            rsv = _arm_get(rid, token, "2023-04-01")
            props = rsv.get("properties", {})
            sku = rsv.get("sku") or {}
            extra["rsv_sku_name"] = sku.get("name")          # "Standard" | "RS0"
            extra["rsv_sku_tier"] = sku.get("tier")
            extra["rsv_redundancy"] = props.get("redundancySettings", {}).get("standardTierStorageRedundancy")
            # "LocallyRedundant" | "GeoRedundant" | "ZoneRedundant"
            extra["rsv_cross_region_restore"] = props.get("redundancySettings", {}).get("crossRegionRestore")
            # Pull backup usage summary (item counts)
            try:
                usage = _arm_get(f"{rid}/usages", token, "2016-06-01")
                usage_items = usage.get("value", [])
                protected = next(
                    (u for u in usage_items if u.get("name", {}).get("value") == "protectedItemCount"),
                    None,
                )
                extra["rsv_protected_items"] = int((protected or {}).get("currentValue", 0))
                storage = next(
                    (u for u in usage_items if u.get("name", {}).get("value") == "GBsUsed"),
                    None,
                )
                extra["rsv_storage_used_gb"] = round(float((storage or {}).get("currentValue", 0)), 2)
            except Exception:
                extra["rsv_protected_items"] = None
                extra["rsv_storage_used_gb"] = None

            # Backup-policy retention — long retention (esp. daily) drives most of
            # the backup-storage cost. Compute the longest daily/weekly retention
            # and the longest long-term (monthly/yearly) retention across policies.
            try:
                policies = _arm_get_all(f"{rid}/backupPolicies", token, "2023-04-01")
                max_daily_days = 0
                max_ltr_years = 0.0
                for pol in policies:
                    rp = (pol.get("properties", {}) or {}).get("retentionPolicy", {}) or {}
                    daily = (rp.get("dailySchedule", {}) or {}).get("retentionDuration", {}) or {}
                    if (daily.get("durationType") == "Days") and daily.get("count"):
                        max_daily_days = max(max_daily_days, int(daily["count"]))
                    weekly = (rp.get("weeklySchedule", {}) or {}).get("retentionDuration", {}) or {}
                    if (weekly.get("durationType") == "Weeks") and weekly.get("count"):
                        max_daily_days = max(max_daily_days, int(weekly["count"]) * 7)
                    monthly = (rp.get("monthlySchedule", {}) or {}).get("retentionDuration", {}) or {}
                    if (monthly.get("durationType") == "Months") and monthly.get("count"):
                        max_ltr_years = max(max_ltr_years, int(monthly["count"]) / 12.0)
                    yearly = (rp.get("yearlySchedule", {}) or {}).get("retentionDuration", {}) or {}
                    if (yearly.get("durationType") == "Years") and yearly.get("count"):
                        max_ltr_years = max(max_ltr_years, int(yearly["count"]))
                extra["rsv_max_daily_retention_days"] = max_daily_days or None
                extra["rsv_max_ltr_years"] = round(max_ltr_years, 1) if max_ltr_years else None
            except Exception:
                extra["rsv_max_daily_retention_days"] = None
                extra["rsv_max_ltr_years"] = None

        # ── Backup Policies (Microsoft.RecoveryServices/vaults/backupPolicies)
        # These are child resources; top-level list won't show them individually,
        # but the parent RSV enrichment above covers usage. No separate handler needed.

        # ── Virtual Networks (VNet) ──────────────────────────────────────────
        elif rtype == "microsoft.network/virtualnetworks":
            vnet = _arm_get(rid, token, "2023-09-01")
            props = vnet.get("properties", {})
            address_space = props.get("addressSpace", {}).get("addressPrefixes", [])
            subnets = props.get("subnets", [])

            extra["vnet_address_space"] = address_space
            extra["vnet_subnet_count"] = len(subnets)
            extra["vnet_peering_count"] = len(props.get("virtualNetworkPeerings", []))
            extra["vnet_dns_servers"] = props.get("dhcpOptions", {}).get("dnsServers", [])

            # Summarise each subnet — useful for detecting empty or mis-configured ones
            subnet_summaries = []
            for sn in subnets:
                sp = sn.get("properties", {})
                subnet_summaries.append({
                    "name": sn.get("name"),
                    "address_prefix": sp.get("addressPrefix"),
                    "nsg_attached": bool(sp.get("networkSecurityGroup")),
                    "route_table_attached": bool(sp.get("routeTable")),
                    "delegations": [d.get("properties", {}).get("serviceName") for d in sp.get("delegations", [])],
                    "service_endpoints": [se.get("service") for se in sp.get("serviceEndpoints", [])],
                    "private_endpoint_network_policies": sp.get("privateEndpointNetworkPolicies"),
                })
            extra["vnet_subnets"] = subnet_summaries

            # Flag VNets with no peerings and only one subnet (likely abandoned dev VNet)
            extra["vnet_is_isolated"] = (
                extra["vnet_peering_count"] == 0
                and extra["vnet_subnet_count"] <= 1
            )

        # ── Route Tables ─────────────────────────────────────────────────────
        elif rtype == "microsoft.network/routetables":
            rt = _arm_get(rid, token, "2023-09-01")
            props = rt.get("properties", {})
            subnets_assoc = props.get("subnets", [])
            routes = props.get("routes", [])
            extra["rt_route_count"] = len(routes)
            extra["rt_associated_subnet_count"] = len(subnets_assoc)
            extra["rt_is_orphaned"] = len(subnets_assoc) == 0
            # Check for a catch-all 0.0.0.0/0 route (internet egress pattern — worth reviewing)
            extra["rt_has_default_route"] = any(
                (r.get("properties") or {}).get("addressPrefix") == "0.0.0.0/0"
                for r in routes
            )

        # ── VPN / ExpressRoute Gateways ───────────────────────────────────────
        elif rtype == "microsoft.network/virtualnetworkgateways":
            gw = _arm_get(rid, token, "2023-09-01")
            props = gw.get("properties", {})
            sku = (props.get("sku") or {})
            extra["vng_sku_name"] = sku.get("name")    # "Basic"|"VpnGw1"|"VpnGw2" etc.
            extra["vng_sku_tier"] = sku.get("tier")
            extra["vng_gateway_type"] = props.get("gatewayType")   # "Vpn"|"ExpressRoute"
            extra["vng_vpn_type"] = props.get("vpnType")
            extra["vng_active_active"] = props.get("activeActive", False)
            # Connection count (is it actually in use?)
            try:
                conns = _arm_get_all(f"{rid}/connections", token, "2023-09-01")
                extra["vng_connection_count"] = len(conns)
                extra["vng_is_unused"] = len(conns) == 0
            except Exception:
                extra["vng_connection_count"] = None
                extra["vng_is_unused"] = None

        # ── NAT Gateways ─────────────────────────────────────────────────────
        elif rtype == "microsoft.network/natgateways":
            nat = _arm_get(rid, token, "2023-09-01")
            props = nat.get("properties", {})
            extra["nat_idle_timeout_min"] = props.get("idleTimeoutInMinutes")
            extra["nat_public_ip_count"] = len(props.get("publicIpAddresses", []))
            extra["nat_associated_subnet_count"] = len(props.get("subnets", []))
            extra["nat_is_orphaned"] = extra["nat_associated_subnet_count"] == 0

        # ── Managed disk snapshots ───────────────────────────────────────────
        elif rtype == "microsoft.compute/snapshots":
            snap = _arm_get(rid, token, "2023-04-02")
            props = snap.get("properties", {})
            extra["snapshot_size_gb"] = props.get("diskSizeGB")
            extra["snapshot_incremental"] = props.get("incremental", False)
            extra["snapshot_sku"] = (snap.get("sku") or {}).get("name")   # Standard_LRS / Standard_ZRS
            extra["snapshot_time_created"] = props.get("timeCreated")

        # ── Log Analytics workspaces ─────────────────────────────────────────
        elif rtype == "microsoft.operationalinsights/workspaces":
            ws = _arm_get(rid, token, "2022-10-01")
            props = ws.get("properties", {})
            extra["la_retention_days"] = props.get("retentionInDays")
            extra["la_sku"] = ((props.get("sku") or {}).get("name"))       # PerGB2018 / CapacityReservation
            wq = (props.get("workspaceCapping") or {}).get("dailyQuotaGb")
            extra["la_daily_quota_gb"] = wq if wq and wq > 0 else None

        # ── Azure Firewall ───────────────────────────────────────────────────
        elif rtype == "microsoft.network/azurefirewalls":
            fw = _arm_get(rid, token, "2023-09-01")
            props = fw.get("properties", {})
            extra["fw_tier"] = (props.get("sku") or {}).get("tier")        # Standard / Premium / Basic
            extra["fw_ip_config_count"] = len(props.get("ipConfigurations", []))
            rule_collections = (
                props.get("applicationRuleCollections", [])
                + props.get("networkRuleCollections", [])
                + props.get("natRuleCollections", [])
            )
            extra["fw_has_policy"] = bool(props.get("firewallPolicy"))
            extra["fw_rule_collection_count"] = len(rule_collections)
            extra["fw_is_idle"] = (
                extra["fw_ip_config_count"] == 0
                or (not extra["fw_has_policy"] and extra["fw_rule_collection_count"] == 0)
            )

        # ── VM Scale Sets ────────────────────────────────────────────────────
        elif rtype == "microsoft.compute/virtualmachinescalesets":
            vmss = _arm_get(rid, token, "2023-09-01")
            props = vmss.get("properties", {})
            sku = vmss.get("sku") or {}
            extra["vmss_vm_size"] = sku.get("name")
            extra["vmss_capacity"] = sku.get("capacity")
            extra["vmss_overprovision"] = props.get("overprovision")
            extra["vmss_spot"] = (
                (props.get("virtualMachineProfile", {}) or {})
                .get("priority") == "Spot"
            )

    except Exception:
        pass  # Best-effort — never fail the whole scan for one resource

    # Attach CPU utilisation (best-effort) so right-sizing / tier-downgrade
    # recommendations can be gated on real usage instead of a blanket rule.
    _enrich_cpu(rid, token, rtype, extra)

    return {**res, **extra}


def _rest_list_resources_in_group(resource_group: str, subscription_id: str | None = None) -> list[dict]:
    token = _get_access_token()
    sub = subscription_id or os.environ["AZURE_SUBSCRIPTION_ID"]
    all_res = _arm_get_all(
        f"/subscriptions/{sub}/resourceGroups/{resource_group}/resources"
        f"?$expand=createdTime,changedTime,provisioningState",
        token, "2021-04-01",
    )
    resources = []
    for res in all_res:
        sku = res.get("sku")
        sku_info = None
        if isinstance(sku, dict):
            sku_info = {
                "name": sku.get("name"), "tier": sku.get("tier"),
                "size": sku.get("size"), "capacity": sku.get("capacity"),
            }
        base = {
            "id": res.get("id", ""),
            "name": res.get("name", ""),
            "type": res.get("type", ""),
            "location": res.get("location", ""),
            "sku": sku_info,
            "tags": res.get("tags") or {},
            "kind": res.get("kind"),
            "resource_group": resource_group,
            "created_time": res.get("createdTime"),
            "changed_time": res.get("changedTime"),
            "provisioning_state": res.get("provisioningState"),
        }
        # Deep-enrich with per-type API calls
        resources.append(_enrich_resource(base, token))
    return resources


def _rest_get_resource_costs(resource_group: str, subscription_id: str | None = None) -> list[dict]:
    try:
        token = _get_access_token()
        sub = subscription_id or os.environ["AZURE_SUBSCRIPTION_ID"]
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        body = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {
                "from": month_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [
                    {"type": "Dimension", "name": "ResourceId"},
                    {"type": "Dimension", "name": "ResourceType"},
                ],
                "filter": {
                    "dimensions": {
                        "name": "ResourceGroupName",
                        "operator": "In",
                        "values": [resource_group],
                    }
                },
            },
        }
        raw = _arm_post(
            f"/subscriptions/{sub}/providers/Microsoft.CostManagement/query",
            token, "2023-11-01", body,
        )
        columns = [c["name"].lower() for c in raw.get("properties", {}).get("columns", [])]
        costs = []
        for row in raw.get("properties", {}).get("rows", []):
            entry = dict(zip(columns, row))
            raw_cost = float(entry.get("cost", 0))
            currency = str(entry.get("currency", "USD")).upper()
            costs.append({
                "resource_id": str(entry.get("resourceid", "")),
                "resource_type": str(entry.get("resourcetype", "")),
                "cost_original": round(raw_cost, 4),
                "currency_original": currency,
                "cost_usd": _to_usd(raw_cost, currency),
            })
        return costs
    except Exception:
        return []


# ---------------------------------------------------------------------------
# CLI path (local dev with az login)
# ---------------------------------------------------------------------------

def _find_az() -> str:
    if sys.platform == "win32":
        for candidate in ("az.cmd", "az"):
            found = shutil.which(candidate)
            if found:
                return found
        hardcoded = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
        if os.path.exists(hardcoded):
            return hardcoded
        return "az.cmd"
    return shutil.which("az") or "az"


_AZ_EXECUTABLE = _find_az()


def _run_az(args: list[str]) -> Any:
    try:
        result = subprocess.run(
            [_AZ_EXECUTABLE] + args, capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        raise AzureCLINotInstalledError(
            "Azure CLI ('az') is not installed or not on PATH."
        )
    except subprocess.TimeoutExpired:
        raise AzureCLIError("Azure CLI command timed out.")

    stderr = result.stderr.strip()
    if result.returncode != 0:
        lower = stderr.lower()
        if "please run 'az login'" in lower or "not logged in" in lower:
            raise AzureNotLoggedInError("Not logged in. Run 'az login'.")
        if "resource group" in lower and ("not found" in lower or "could not be found" in lower):
            raise AzureResourceGroupNotFoundError(f"Resource group not found: {stderr}")
        raise AzureCLIError(f"Azure CLI failed: {stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AzureCLIError(f"Failed to parse az output: {exc}") from exc


def _cli_list_resource_groups() -> list[dict]:
    raw = _run_az(["group", "list", "-o", "json"])
    return [{"name": rg.get("name", ""), "location": rg.get("location", ""),
             "tags": rg.get("tags") or {},
             "provisioning_state": rg.get("properties", {}).get("provisioningState", "")}
            for rg in raw]


def _cli_list_resources_in_group(resource_group: str) -> list[dict]:
    raw = _run_az(["resource", "list", "--resource-group", resource_group, "-o", "json"])
    resources = []
    for res in raw:
        sku = res.get("sku")
        sku_info = None
        if isinstance(sku, dict):
            sku_info = {"name": sku.get("name"), "tier": sku.get("tier"),
                        "size": sku.get("size"), "capacity": sku.get("capacity")}
        resources.append({
            "id": res.get("id", ""), "name": res.get("name", ""),
            "type": res.get("type", ""), "location": res.get("location", ""),
            "sku": sku_info, "tags": res.get("tags") or {},
            "kind": res.get("kind"), "resource_group": res.get("resourceGroup", resource_group),
        })
    return resources


def _cli_get_resource_costs(resource_group: str) -> list[dict]:
    try:
        account = _run_az(["account", "show", "-o", "json"])
        sub = account["id"]
        uri = (f"https://management.azure.com/subscriptions/{sub}"
               f"/providers/Microsoft.CostManagement/query?api-version=2023-11-01")
        body = json.dumps({"type": "ActualCost", "timeframe": "MonthToDate",
                           "dataset": {"granularity": "None",
                                       "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                                       "grouping": [{"type": "Dimension", "name": "ResourceId"},
                                                    {"type": "Dimension", "name": "ResourceType"}],
                                       "filter": {"dimensions": {"name": "ResourceGroupName",
                                                                 "operator": "In", "values": [resource_group]}}}})
        raw = _run_az(["rest", "--method", "POST", "--uri", uri, "--body", body, "-o", "json"])
        columns = [c["name"].lower() for c in raw["properties"]["columns"]]
        result = []
        for r in raw["properties"]["rows"]:
            e = dict(zip(columns, r))
            raw_cost = float(e.get("cost", 0))
            currency = str(e.get("currency", "USD")).upper()
            result.append({
                "resource_id": str(e.get("resourceid", "")),
                "resource_type": str(e.get("resourcetype", "")),
                "cost_original": round(raw_cost, 4),
                "currency_original": currency,
                "cost_usd": _to_usd(raw_cost, currency),
            })
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_current_subscription_id() -> str:
    if _use_rest():
        return os.environ["AZURE_SUBSCRIPTION_ID"]
    return _run_az(["account", "show", "-o", "json"])["id"]


def list_subscriptions() -> list[dict]:
    """
    Return all Azure subscriptions the service principal can see.
    Each entry: {subscription_id, display_name, state, tenant_id}
    Falls back to the single configured subscription in CLI mode.
    """
    if _use_rest():
        return _rest_list_subscriptions()
    # CLI mode: return the currently active subscription only
    acct = _run_az(["account", "show", "-o", "json"])
    return [{
        "subscription_id": acct.get("id", ""),
        "display_name": acct.get("name", ""),
        "state": acct.get("state", "Enabled"),
        "tenant_id": acct.get("tenantId", ""),
    }]


def list_resource_groups(subscription_id: str | None = None) -> list[dict]:
    """List all resource groups. Pass subscription_id to target a specific subscription."""
    if _use_rest():
        return _rest_list_resource_groups(subscription_id)
    return _cli_list_resource_groups()


def list_resources_in_group(resource_group: str, subscription_id: str | None = None) -> list[dict]:
    """Scan all resources in a resource group, with deep per-type enrichment."""
    if _use_rest():
        return _rest_list_resources_in_group(resource_group, subscription_id)
    return _cli_list_resources_in_group(resource_group)


def get_resource_costs(resource_group: str, subscription_id: str | None = None) -> list[dict]:
    """Fetch month-to-date actual costs for a resource group."""
    if _use_rest():
        return _rest_get_resource_costs(resource_group, subscription_id)
    return _cli_get_resource_costs(resource_group)


def list_advisor_recommendations(subscription_id: str | None = None) -> dict[str, list[dict]]:
    """Fetch Azure Advisor recommendations, keyed by lowercased resource id.

    Advisor is Microsoft's own optimisation engine; folding its findings in lets
    us (a) corroborate our deterministic rules (raising confidence when they
    agree) and (b) surface anything Advisor caught that we don't rule on yet.

    Best-effort: returns {} on any failure (SP lacks access, REST-only, etc.),
    so the pipeline never breaks if Advisor data is unavailable.
    """
    if not _use_rest():
        return {}
    try:
        token = _get_access_token()
        sub = subscription_id or os.environ["AZURE_SUBSCRIPTION_ID"]
        items = _arm_get_all(
            f"/subscriptions/{sub}/providers/Microsoft.Advisor/recommendations",
            token, "2023-01-01",
        )
    except Exception:
        return {}

    by_resource: dict[str, list[dict]] = {}
    for it in items:
        props = it.get("properties", {}) or {}
        rid = (props.get("resourceMetadata", {}) or {}).get("resourceId", "")
        if not rid:
            continue
        short = props.get("shortDescription", {}) or {}
        entry = {
            "category": props.get("category"),          # Cost | Performance | Security | ...
            "impact": props.get("impact"),              # High | Medium | Low
            "problem": short.get("problem"),
            "solution": short.get("solution"),
            "savings_usd": (
                (props.get("extendedProperties", {}) or {}).get("savingsAmount")
            ),
        }
        by_resource.setdefault(rid.lower(), []).append(entry)
    return by_resource
