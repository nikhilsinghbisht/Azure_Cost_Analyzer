"""
recommendation.py
------------------
The scoring & ranking layer for the recommendation engine.

Design principles
-----------------
1. **Deterministic decisions.** Every recommendation is produced by a
   hand-written rule (rules_engine.py). This module never *decides* anything;
   it only *scores, ranks and describes* what the rules produced. The AI layer
   later rewrites the prose, but the numbers/decisions here are final.

2. **Multiple ranked recommendations per resource.** A single resource can have
   several candidate actions (e.g. a VM: downsize / move to B-series / buy a
   Reserved Instance / enable auto-shutdown). Each is scored independently and
   ranked by *priority* so the client sees the highest-impact action first.

3. **Rich, explainable schema.** Each recommendation carries a title, category,
   description, reason, current vs recommended configuration, savings,
   confidence, risk, priority and a Microsoft Learn documentation link — while
   remaining 100% backward compatible with the legacy flat `issue` schema that
   the Excel exporter, database and frontend already consume.

Scoring model (all deterministic, reproducible)
-----------------------------------------------
* confidence (0-1): how sure we are the action is safe/correct. Driven by the
  strength of the evidence — measured metrics beat heuristics, and a longer
  metric window with more headroom raises confidence.
* risk (Low/Medium/High): blast-radius if the change is wrong. Deleting a
  resource or touching production (tag/age signal) is riskier than a reversible
  config toggle.
* priority score: savings × confidence × risk_weight. This is what we rank on,
  so "big, safe, high-confidence" wins over "small, risky, guess".
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Canonical categories (the 6 the product surfaces, plus the legacy aliases the
# rules already emit so nothing breaks during the transition).
# ---------------------------------------------------------------------------
CATEGORIES = {
    "Idle",
    "Overprovisioned",
    "Underprovisioned",
    "Cost Saving",
    "Performance",
    "Security",
    "Governance",
}

# Map every legacy rules-engine category onto a canonical one.
_CATEGORY_ALIAS = {
    "unused / idle": "Idle",
    "over-provisioned": "Overprovisioned",
    "overprovisioned": "Overprovisioned",
    "under-provisioned": "Underprovisioned",
    "wrong pricing tier": "Cost Saving",
    "redundancy config": "Cost Saving",
    "optimization opportunity": "Cost Saving",
    "misconfigured": "Performance",
    "security risk": "Security",
    # Tagging is governance (no $ savings) — never map to Cost Saving.
    "tagging / governance": "Governance",
    "governance": "Governance",
}

# Risk → multiplier for the priority score. Low-risk actions are preferred at
# equal savings because they are safe to apply.
_RISK_WEIGHT = {"Low": 1.0, "Medium": 0.7, "High": 0.45}

# Severity mapping kept for the legacy `severity` field (Excel colour-codes it).
_PRIORITY_TO_SEVERITY = {"P1": "high", "P2": "medium", "P3": "low", "P4": "low"}


def canonical_category(raw: str | None) -> str:
    if not raw:
        return "Cost Saving"
    return _CATEGORY_ALIAS.get(raw.strip().lower(), raw)


# ---------------------------------------------------------------------------
# Documentation references (Microsoft Learn). Keyed by an action slug so every
# recommendation can point the client at authoritative guidance.
# ---------------------------------------------------------------------------
_DOC_URLS: dict[str, str] = {
    "vm_downsize": "https://learn.microsoft.com/azure/virtual-machines/sizes",
    "vm_bseries": "https://learn.microsoft.com/azure/virtual-machines/sizes-b-series-burstable",
    "vm_reserved": "https://learn.microsoft.com/azure/cost-management-billing/reservations/save-compute-costs-reservations",
    "vm_autoshutdown": "https://learn.microsoft.com/azure/virtual-machines/auto-shutdown-vm",
    "vm_spot": "https://learn.microsoft.com/azure/virtual-machines/spot-vms",
    "vm_deallocated": "https://learn.microsoft.com/azure/virtual-machines/states-billing",
    "disk_orphaned": "https://learn.microsoft.com/azure/virtual-machines/disks-find-unattached-portal",
    "disk_tier": "https://learn.microsoft.com/azure/virtual-machines/disks-types",
    "snapshot_stale": "https://learn.microsoft.com/azure/virtual-machines/disks-incremental-snapshots",
    "storage_tier": "https://learn.microsoft.com/azure/storage/blobs/access-tiers-overview",
    "storage_lifecycle": "https://learn.microsoft.com/azure/storage/blobs/lifecycle-management-overview",
    "storage_redundancy": "https://learn.microsoft.com/azure/storage/common/storage-redundancy",
    "sql_tier": "https://learn.microsoft.com/azure/azure-sql/database/service-tiers-sql-database-vcore",
    "sql_rightsize": "https://learn.microsoft.com/azure/azure-sql/database/scale-resources",
    "db_tier": "https://learn.microsoft.com/azure/postgresql/flexible-server/concepts-compute-storage",
    "appservice_rightsize": "https://learn.microsoft.com/azure/app-service/manage-scale-up",
    "aks_spot": "https://learn.microsoft.com/azure/aks/spot-node-pool",
    "aks_autoscale": "https://learn.microsoft.com/azure/aks/cluster-autoscaler",
    "redis_tier": "https://learn.microsoft.com/azure/azure-cache-for-redis/cache-overview",
    "cosmos_region": "https://learn.microsoft.com/azure/cosmos-db/optimize-cost-regions",
    "appgw_idle": "https://learn.microsoft.com/azure/application-gateway/understanding-pricing",
    "lb_idle": "https://learn.microsoft.com/azure/load-balancer/load-balancer-overview",
    "publicip_orphaned": "https://learn.microsoft.com/azure/virtual-network/ip-services/public-ip-addresses",
    "natgw_idle": "https://learn.microsoft.com/azure/nat-gateway/nat-overview",
    "firewall_idle": "https://learn.microsoft.com/azure/firewall/firewall-faq",
    "keyvault_tier": "https://learn.microsoft.com/azure/key-vault/general/overview",
    "rsv_redundancy": "https://learn.microsoft.com/azure/backup/backup-create-recovery-services-vault",
    "loganalytics_retention": "https://learn.microsoft.com/azure/azure-monitor/logs/data-retention-configure",
    "acr_tier": "https://learn.microsoft.com/azure/container-registry/container-registry-skus",
    "namespace_tier": "https://learn.microsoft.com/azure/service-bus-messaging/service-bus-premium-messaging",
    "reserved_instance": "https://learn.microsoft.com/azure/cost-management-billing/reservations/save-compute-costs-reservations",
    "savings_plan": "https://learn.microsoft.com/azure/cost-management-billing/savings-plan/savings-plan-compute-overview",
    "tags_governance": "https://learn.microsoft.com/azure/cloud-adoption-framework/ready/azure-best-practices/resource-tagging",
    "security": "https://learn.microsoft.com/azure/security/fundamentals/best-practices-and-patterns",
}


def doc_url(action: str | None) -> str | None:
    return _DOC_URLS.get(action or "")


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------
# base confidence per evidence class. "measured" facts (an orphaned disk, a
# stopped VM) are near-certain; metric-gated right-sizing depends on whether we
# actually have the metric; pure heuristics (tier guesses) are the least sure.
_BASE_CONFIDENCE = {
    "measured_fact": 0.95,   # e.g. disk unattached, VM deallocated, IP orphaned
    "metric_backed": 0.90,   # a downsize backed by real low CPU over the window
    "metric_missing": 0.55,  # same action but no metric data → hedge
    "heuristic": 0.70,       # tier/redundancy guess from config alone
    "security": 0.92,        # security best-practice, config-verified
    "governance": 0.75,      # tagging/age based governance signal
}


def score_confidence(
    evidence: str,
    *,
    window_days: int | None = None,
    util_headroom: float | None = None,
) -> float:
    """Deterministic confidence in [0,1].

    evidence      : one of _BASE_CONFIDENCE keys.
    window_days   : length of the metric window backing the decision (7/30/90).
                    A longer window is more trustworthy.
    util_headroom : how far below the downsize threshold the resource sits
                    (0 = right at the line, 1 = completely idle). More headroom
                    → higher confidence.
    """
    conf = _BASE_CONFIDENCE.get(evidence, 0.70)
    if window_days:
        # +0 at 7d, up to +0.05 at 90d — longer observation is more reliable.
        conf += min((window_days - 7) / 83.0, 1.0) * 0.05
    if util_headroom is not None:
        # scale ±0.08 around the base depending on how idle the resource is.
        conf += (util_headroom - 0.5) * 0.16
    return round(max(0.05, min(conf, 0.99)), 2)


# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------
def score_risk(
    *,
    reversible: bool,
    destructive: bool,
    is_production: bool | None,
    performance_impact: bool,
) -> str:
    """Return 'Low' | 'Medium' | 'High'.

    destructive        : the fix deletes data/resources (disk, VM, account).
    reversible         : the change can be undone easily (a config toggle).
    is_production      : inferred from tags/age — production raises risk.
    performance_impact : the change could reduce capacity/perf (right-sizing).
    """
    score = 0
    if destructive:
        score += 2
    if performance_impact:
        score += 1
    if not reversible:
        score += 1
    if is_production:
        score += 1
    if score >= 3:
        return "High"
    if score >= 1:
        return "Medium"
    return "Low"


# ---------------------------------------------------------------------------
# Priority scoring & label
# ---------------------------------------------------------------------------
def priority_score(savings: float, confidence: float, risk: str) -> float:
    """Rankable numeric priority: expected, risk-adjusted monthly saving."""
    return round((savings or 0) * confidence * _RISK_WEIGHT.get(risk, 0.7), 2)


def priority_label(score: float, is_security: bool = False) -> str:
    """Bucket the numeric score into P1..P4. Security always ≥ P2."""
    if is_security:
        return "P1" if score <= 0 else "P1"
    if score >= 100:
        return "P1"
    if score >= 25:
        return "P2"
    if score >= 5:
        return "P3"
    return "P4"


# ---------------------------------------------------------------------------
# Production / age inference from tags + resource age
# ---------------------------------------------------------------------------
_PROD_TAG_VALUES = {"prod", "production", "prd", "live"}
_NONPROD_TAG_VALUES = {"dev", "development", "test", "testing", "qa", "uat", "staging", "sandbox", "demo"}


def infer_production(res: dict) -> bool | None:
    """Best-effort: is this a production resource? None if unknown.

    Reads common tag keys (env/environment/stage/tier). Used to raise risk on
    changes to prod and to flag *non*-prod resources as auto-shutdown / idle
    candidates.
    """
    tags = {k.lower(): str(v).lower() for k, v in (res.get("tags") or {}).items()}
    for key in ("environment", "env", "stage", "tier", "usage"):
        val = tags.get(key)
        if not val:
            continue
        if any(p in val for p in _PROD_TAG_VALUES):
            return True
        if any(n in val for n in _NONPROD_TAG_VALUES):
            return False
    # Name-based fallback (common convention like "vm-dev-01").
    name = (res.get("name") or "").lower()
    if any(f"-{n}" in name or f"{n}-" in name for n in _NONPROD_TAG_VALUES):
        return False
    if any(f"-{p}" in name or f"{p}-" in name for p in _PROD_TAG_VALUES):
        return True
    return None


def is_nonproduction(res: dict) -> bool:
    return infer_production(res) is False


def resource_age_days(res: dict) -> int | None:
    """Age of the resource in days from its createdTime, if available."""
    created = res.get("created_time")
    if not created:
        return None
    try:
        from datetime import datetime, timezone
        s = str(created).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ranking & de-duplication
# ---------------------------------------------------------------------------
def rank_and_dedup(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort recommendations by priority (desc) and drop exact duplicates.

    De-dup key is (resource_name, category, title) so a resource can still
    carry several *different* recommendations, but never the same one twice.
    Security findings are always kept even if they tie on score.
    """
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for r in recs:
        key = (
            (r.get("resource_name") or "").lower(),
            (r.get("category") or "").lower(),
            (r.get("title") or r.get("issue") or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    unique.sort(
        key=lambda x: (
            x.get("_priority_score", 0),
            x.get("estimated_monthly_savings_usd") or 0,
        ),
        reverse=True,
    )
    return unique


def group_by_resource(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Produce a per-resource view: each resource with its ranked recommendation
    list. Additive output (the flat issues[] list is still the source of truth).
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for r in recs:
        name = r.get("resource_name") or ""
        if name not in groups:
            groups[name] = {
                "resource_name": name,
                "resource_type": r.get("resource_type"),
                "recommendations": [],
                "total_savings_usd": 0.0,
            }
            order.append(name)
        groups[name]["recommendations"].append(r)
        groups[name]["total_savings_usd"] += r.get("estimated_monthly_savings_usd") or 0

    result = []
    for name in order:
        g = groups[name]
        g["recommendations"].sort(key=lambda x: x.get("_priority_score", 0), reverse=True)
        g["total_savings_usd"] = round(g["total_savings_usd"], 2)
        g["top_recommendation"] = g["recommendations"][0]["title"] if g["recommendations"] else None
        result.append(g)
    result.sort(key=lambda x: x["total_savings_usd"], reverse=True)
    return result
