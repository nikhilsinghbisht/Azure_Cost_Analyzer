"""
sku_advisor.py
--------------
Dynamic SKU right-sizing WITHOUT hardcoded SKU→SKU mappings.

Given a resource's current VM size, its region and its measured CPU utilisation,
this module derives candidate cheaper sizes by:

  1. Parsing the Azure VM naming convention (Standard_<family><vCPU><attrs>_v<ver>)
     to read the family and vCPU count — the "N" in D2/D4/D8 is the vCPU count by
     Azure convention, so we never hardcode a size table.
  2. Computing the *target* vCPU from utilisation: pick the smallest standard
     vCPU step that keeps projected peak CPU under a safe head-room threshold.
  3. Generating same-family candidates at each vCPU step ≤ current, plus a
     Burstable B-series equivalent, and pricing each against the LIVE retail API.
  4. Ranking survivors by real monthly cost difference.

Prices come from finding_rules._vm_prices (live retail API, cached).
"""

from __future__ import annotations

import math
import re
from typing import Any

# Standard vCPU steps Azure offers across most families. Used only to *walk
# down* to a smaller size — not a price table.
_VCPU_STEPS = [1, 2, 4, 8, 16, 32, 48, 64, 96, 128]
# Burstable B-series vCPU steps that actually exist.
_B_VCPU_STEPS = [1, 2, 4, 8, 12, 16, 20]

# Keep projected PEAK CPU under this after resizing (safety head-room).
_TARGET_PEAK_CEILING = 60.0

_SIZE_RE = re.compile(r"^Standard_([A-Za-z]+?)(\d+)([a-z]*)(?:_v(\d+))?$")


def _parse_size(size: str) -> dict | None:
    """Standard_D8s_v5 → {family:'D', vcpu:8, attrs:'s', ver:'5'}."""
    if not size:
        return None
    m = _SIZE_RE.match(size.strip())
    if not m:
        return None
    return {
        "family": m.group(1),
        "vcpu": int(m.group(2)),
        "attrs": m.group(3) or "",
        "ver": m.group(4),
    }


def _compose_size(parsed: dict, vcpu: int) -> str:
    ver = f"_v{parsed['ver']}" if parsed.get("ver") else ""
    return f"Standard_{parsed['family']}{vcpu}{parsed['attrs']}{ver}"


def _target_vcpu(current_vcpu: int, cpu_max: float | None, cpu_avg: float | None) -> int:
    """Smallest vCPU count that keeps projected peak CPU under the ceiling.

    If a VM peaks at 30% on 8 vCPU, it uses ~2.4 vCPU-equivalents, so 4 vCPU
    keeps it comfortably under 60%. We use peak (fallback avg) to stay safe.
    """
    signal = cpu_max if cpu_max is not None else cpu_avg
    if signal is None:
        return current_vcpu
    used_vcpu = current_vcpu * (signal / 100.0)
    needed = used_vcpu / (_TARGET_PEAK_CEILING / 100.0)
    target = max(1, math.ceil(needed))
    # snap up to the nearest real step and never exceed current
    for step in _VCPU_STEPS:
        if step >= target:
            return min(step, current_vcpu)
    return current_vcpu


def _nearest_b_size(vcpu: int) -> str:
    for step in _B_VCPU_STEPS:
        if step >= vcpu:
            return "B1ms" if step == 1 else f"B{step}ms"
    return "B20ms"


def cheaper_alternatives(
    current_size: str,
    location: str | None,
    cpu_avg: float | None,
    cpu_max: float | None,
    max_candidates: int = 4,
) -> list[dict[str, Any]]:
    """Return ranked cheaper VM SKU options with real monthly cost + saving.

    Each item: {size, vcpu, family, monthly_usd, saving_usd, saving_pct, kind}.
    Empty list if the current size can't be parsed/priced or nothing is cheaper.
    """
    from finding_rules import _vm_prices  # lazy import to avoid a cycle

    parsed = _parse_size(current_size)
    if not parsed:
        return []

    cur_price = _vm_prices(current_size, location).get("ondemand")
    if not cur_price:
        return []

    target = _target_vcpu(parsed["vcpu"], cpu_max, cpu_avg)
    candidates: list[dict] = []
    seen: set[str] = {current_size}

    # 1) Same-family smaller sizes from target up to (but not including) current.
    for step in _VCPU_STEPS:
        if step < target or step >= parsed["vcpu"]:
            continue
        size = _compose_size(parsed, step)
        if size in seen:
            continue
        seen.add(size)
        price = _vm_prices(size, location).get("ondemand")
        if price and price < cur_price:
            candidates.append({
                "size": size, "vcpu": step, "family": parsed["family"],
                "monthly_usd": round(price, 2), "kind": "same-family-downsize",
            })

    # 2) Burstable B-series equivalent at the target vCPU (great for low, spiky load).
    b_size = f"Standard_{_nearest_b_size(target)}"
    if b_size not in seen:
        seen.add(b_size)
        b_price = _vm_prices(b_size, location).get("ondemand")
        if b_price and b_price < cur_price:
            candidates.append({
                "size": b_size, "vcpu": target, "family": "B",
                "monthly_usd": round(b_price, 2), "kind": "burstable",
            })

    for c in candidates:
        c["saving_usd"] = round(cur_price - c["monthly_usd"], 2)
        c["saving_pct"] = round((1 - c["monthly_usd"] / cur_price) * 100, 1)

    candidates.sort(key=lambda c: c["saving_usd"], reverse=True)
    return candidates[:max_candidates]
