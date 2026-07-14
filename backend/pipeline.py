"""
pipeline.py
-----------
Standalone daily scan pipeline.

Behaviour
---------
  1. Authenticates with Azure using service-principal credentials from env.
  2. Discovers all enabled subscriptions visible to the SP (or a filtered list).
  3. For each subscription → each resource group → scans resources + costs →
     runs AI analysis → saves result to PostgreSQL → writes an Excel report.
  4. Prints a summary table at the end.

Usage
-----
  # Scan everything in the tenant
  python pipeline.py

  # Limit to specific subscriptions
  python pipeline.py --subscription-ids sub-uuid-1 sub-uuid-2

  # Limit to specific resource groups (all subscriptions)
  python pipeline.py --resource-groups my-prod-rg my-dev-rg

  # Override output directory for Excel files (default: ./pipeline_reports)
  python pipeline.py --output-dir /tmp/reports

  # Dry-run: scan + Excel but skip DB write
  python pipeline.py --no-db

Environment variables required
--------------------------------
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SUBSCRIPTION_ID
  OPENAI_API_KEY
  DATABASE_URL  (optional if --no-db)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Ensure backend/ is on sys.path when run from any directory
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ai_analyzer import analyze_resources
from azure_scanner import (
    AzureCLIError,
    get_resource_costs,
    list_resource_groups,
    list_resources_in_group,
    list_subscriptions,
)
from excel_exporter import build_excel_bytes

_db_available = False   # set to True once init_pool() succeeds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# DB helpers (optional — skipped with --no-db)
# ---------------------------------------------------------------------------

async def _db_init() -> bool:
    """Initialise the DB pool once. Returns True on success."""
    global _db_available
    if _db_available:
        return True
    try:
        from db import init_pool
        await init_pool()
        _db_available = True
        return True
    except Exception as exc:
        log.warning("DB unavailable: %s", exc)
        return False


async def _db_save(
    analysis_id: str,
    resource_group: str,
    resources: list,
    analysis: dict,
    *,
    subscription_id: str | None = None,
) -> None:
    """Persist an analysis to the database (best-effort)."""
    from db import create_analysis, update_analysis

    try:
        await create_analysis(
            analysis_id=analysis_id,
            resource_group=resource_group,
            user_id=None,
        )
        issues_found = len(analysis.get("issues", []))
        raw_savings  = analysis.get("total_estimated_monthly_savings_usd")
        savings_str  = f"${raw_savings:.2f}/month" if isinstance(raw_savings, (int, float)) else "Unknown"

        if subscription_id:
            analysis["subscription_id"] = subscription_id

        await update_analysis(
            analysis_id,
            status="completed",
            resources_scanned=len(resources),
            issues_found=issues_found,
            estimated_savings=savings_str,
            analysis_result=analysis,
        )
        log.info("  ✔ Saved to DB  analysis_id=%s", analysis_id)
    except Exception as exc:
        log.warning("  ✘ DB save failed: %s", exc)


# ---------------------------------------------------------------------------
# Single-RG scan
# ---------------------------------------------------------------------------

def _scan_rg(
    resource_group: str,
    subscription_id: str,
    sub_name: str,
) -> dict | None:
    """
    Full scan → AI analysis for one resource group.
    Returns a result dict, or None on failure.
    """
    log.info("  ▶ Scanning RG: %s  (sub: %s)", resource_group, sub_name)

    try:
        resources = list_resources_in_group(resource_group, subscription_id)
        log.info("    Found %d resources", len(resources))
    except AzureCLIError as exc:
        log.error("    Scanner error: %s", exc)
        return None

    try:
        actual_costs = get_resource_costs(resource_group, subscription_id)
        log.info("    Fetched %d cost entries", len(actual_costs))
    except Exception as exc:
        log.warning("    Cost fetch failed (non-fatal): %s", exc)
        actual_costs = []

    try:
        analysis = analyze_resources(resource_group, resources, actual_costs)
        analysis["actual_cost_breakdown"] = actual_costs
        analysis["subscription_id"] = subscription_id
        analysis["subscription_name"] = sub_name
        log.info(
            "    AI analysis complete — %d issues, est. savings $%.2f/mo",
            len(analysis.get("issues", [])),
            analysis.get("total_estimated_monthly_savings_usd") or 0,
        )
        return {"resources": resources, "analysis": analysis}
    except Exception as exc:
        log.error("    AI analysis failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(
    subscription_ids: list[str] | None,
    resource_group_filter: list[str] | None,
    output_dir: Path,
    no_db: bool,
) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []

    # Initialise DB connection once for the whole pipeline run
    if not no_db:
        await _db_init()

    # 1. Discover subscriptions
    log.info("Discovering subscriptions…")
    try:
        all_subs = list_subscriptions()
    except Exception as exc:
        log.error("Failed to list subscriptions: %s", exc)
        sys.exit(1)

    if subscription_ids:
        subs = [s for s in all_subs if s["subscription_id"] in subscription_ids]
        if not subs:
            log.error("None of the requested subscription IDs were found/accessible.")
            sys.exit(1)
    else:
        subs = all_subs

    log.info("Processing %d subscription(s): %s",
             len(subs), [s["display_name"] for s in subs])

    for sub in subs:
        sub_id   = sub["subscription_id"]
        sub_name = sub["display_name"]
        log.info("━━━ Subscription: %s (%s) ━━━", sub_name, sub_id)

        # 2. Discover resource groups
        try:
            rgs = list_resource_groups(sub_id)
        except Exception as exc:
            log.error("  Failed to list RGs for %s: %s", sub_name, exc)
            continue

        if resource_group_filter:
            rgs = [rg for rg in rgs if rg["name"] in resource_group_filter]

        log.info("  %d resource group(s) to scan", len(rgs))

        for rg in rgs:
            rg_name = rg["name"]

            entry = {
                "subscription_id":   sub_id,
                "subscription_name": sub_name,
                "resource_group":    rg_name,
                "status":            "failed",
                "resources_scanned": 0,
                "issues_found":      0,
                "estimated_savings": 0.0,
                "excel_path":        None,
                "analysis_id":       None,
            }

            result = _scan_rg(rg_name, sub_id, sub_name)

            if result is None:
                summary.append(entry)
                continue

            resources = result["resources"]
            analysis  = result["analysis"]
            analysis_id = str(uuid.uuid4())

            entry["status"]            = "completed"
            entry["resources_scanned"] = len(resources)
            entry["issues_found"]      = len(analysis.get("issues", []))
            entry["estimated_savings"] = analysis.get("total_estimated_monthly_savings_usd") or 0.0
            entry["analysis_id"]       = analysis_id

            # 3. Write Excel report
            try:
                xlsx = build_excel_bytes(analysis, rg_name, len(resources), sub_id)
                ts   = datetime.now(timezone.utc).strftime("%Y%m%d")
                safe_rg   = rg_name.replace(" ", "_").replace("/", "-")
                safe_sub  = sub_name.replace(" ", "_").replace("/", "-")
                fname     = f"{ts}_{safe_sub}_{safe_rg}.xlsx"
                fpath     = output_dir / fname
                fpath.write_bytes(xlsx)
                entry["excel_path"] = str(fpath)
                log.info("  ✔ Excel written: %s", fpath)
            except Exception as exc:
                log.warning("  ✘ Excel generation failed: %s", exc)

            # 4. Save to DB
            if not no_db:
                await _db_save(
                    analysis_id=analysis_id,
                    resource_group=rg_name,
                    resources=resources,
                    analysis=analysis,
                    subscription_id=sub_id,
                )

            summary.append(entry)

    # Close the shared DB pool at the end of the full pipeline run
    if not no_db and _db_available:
        try:
            from db import close_pool
            await close_pool()
        except Exception:
            pass

    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _print_summary(summary: list[dict]) -> None:
    if not summary:
        log.info("No resource groups were processed.")
        return

    print("\n" + "=" * 90)
    print(f"{'PIPELINE SUMMARY':^90}")
    print("=" * 90)
    print(f"{'Subscription':<28} {'Resource Group':<28} {'Status':<10} "
          f"{'Resources':>10} {'Issues':>8} {'Savings/mo':>12}")
    print("-" * 90)

    total_res, total_iss, total_sav = 0, 0, 0.0
    for e in summary:
        status_icon = "✔" if e["status"] == "completed" else "✘"
        print(
            f"{e['subscription_name'][:27]:<28} {e['resource_group'][:27]:<28} "
            f"{status_icon} {e['status']:<8} {e['resources_scanned']:>10} "
            f"{e['issues_found']:>8} ${e['estimated_savings']:>10.2f}"
        )
        total_res += e["resources_scanned"]
        total_iss += e["issues_found"]
        total_sav += e["estimated_savings"]

    print("-" * 90)
    print(f"{'TOTALS':<58} {total_res:>10} {total_iss:>8} ${total_sav:>10.2f}")
    print("=" * 90 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily Azure cost scan pipeline — scans all subscriptions and RGs."
    )
    parser.add_argument(
        "--subscription-ids", nargs="*", metavar="SUB_ID",
        help="Limit scan to specific subscription UUIDs (default: all accessible).",
    )
    parser.add_argument(
        "--resource-groups", nargs="*", metavar="RG",
        help="Limit scan to specific resource group names (applied across all subscriptions).",
    )
    parser.add_argument(
        "--output-dir", default="./pipeline_reports",
        help="Directory to write Excel files (default: ./pipeline_reports).",
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="Skip writing results to the database.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    log.info("Pipeline starting — output dir: %s", output_dir)
    log.info("DB write: %s", "disabled" if args.no_db else "enabled")

    summary = asyncio.run(run_pipeline(
        subscription_ids=args.subscription_ids,
        resource_group_filter=args.resource_groups,
        output_dir=output_dir,
        no_db=args.no_db,
    ))

    _print_summary(summary)

    failed = [e for e in summary if e["status"] == "failed"]
    if failed:
        log.warning("%d resource group(s) failed to scan.", len(failed))
        sys.exit(1)

    log.info("Pipeline complete.")


if __name__ == "__main__":
    main()
