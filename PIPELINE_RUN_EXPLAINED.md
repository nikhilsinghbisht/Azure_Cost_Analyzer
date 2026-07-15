# How a Pipeline Run Works — Step by Step

A simple, end-to-end explanation of what happens every time the daily
Azure cost scan runs. Read top to bottom; no deep tech knowledge needed.

---

## 0. The 30-second version

```
Timer (06:00 UTC)  ─►  Azure DevOps agent wakes up
        │
        ├─ installs Python + libraries
        ├─ builds the command-line arguments
        └─ runs  backend/pipeline.py
                       │
                       ├─ log in to Azure (service principal)
                       ├─ find every subscription
                       │     └─ find every resource group
                       │           └─ scan resources + real prices + billing
                       │                 └─ analyse (Rules Engine + GPT-4o)
                       │                       ├─ write an Excel file
                       │                       └─ save results to PostgreSQL
                       │
                       └─ print a summary table
        │
        └─ publish all Excel files as downloadable "cost-reports" artifacts
```

Two things are produced every run: **Excel files** (one per subscription+RG)
and **database records** (viewable in the web app).

---

## 1. What starts the pipeline?

The pipeline (`azure-pipelines.yml`) can start in **three ways**:

| How | Trigger | Notes |
|---|---|---|
| **Automatic (daily)** | Schedule `0 6 * * *` = 06:00 UTC (11:30 IST) | `always: true` — runs even if code didn't change |
| **Manual** | Azure DevOps → Pipelines → Run | You can set filters (see below) |
| **Not on code push** | `trigger: none` | Pushing code does **not** start a scan |

**Manual run options (parameters):**
- `subscriptionIds` — comma-separated IDs, or `all` (default) to scan everything
- `resourceGroups` — comma-separated names, or `all` (default)
- `skipDatabase` — `true` to skip saving to the DB (Excel only)
- `openaiModel` — override the AI model, or `default` to use `gpt-4o`

---

## 2. Where does it run?

On a **self-hosted agent** — your own VM registered in the `Default` agent pool
(`pool: name: 'Default'`). This avoids needing paid Microsoft-hosted parallelism.
The job has a **180-minute timeout** because a full tenant scan can be slow.

---

## 3. The Azure DevOps steps (what the agent does)

These are the steps in `azure-pipelines.yml`, in order:

**Step 1 — Set up Python**
Creates a virtual environment and installs everything in
`backend/requirements.txt`, then remembers the Python path for later steps.
```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

**Step 2 — Build the command arguments**
Turns your run parameters into command-line flags for `pipeline.py`:
- `all` → no filter flag (scan everything)
- specific IDs/names → `--subscription-ids ...` / `--resource-groups ...`
- `skipDatabase=true` → `--no-db`
- model → sets `OPENAI_MODEL` (override or `gpt-4o`)

**Step 3 — Create the output folder**
`mkdir -p $(Build.ArtifactStagingDirectory)/cost-reports` — where Excel files go.

**Step 4 — Run the scan (the main event)**
```bash
.venv/bin/python pipeline.py <args>
```
All secrets are injected here as environment variables from the
**variable group** `azure-cost-detective-secrets`:
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
`AZURE_SUBSCRIPTION_ID`, `OPENAI_API_KEY`, `OPENAI_MODEL`,
`DATABASE_URL`, `JWT_SECRET`.

**Step 5 — List the generated reports**
Prints how many `.xlsx` files were produced.

**Step 6 — Publish artifacts**
Uploads every Excel file as a pipeline artifact called **`cost-reports`**.
Download from: *Pipelines → the run → Artifacts → cost-reports*.

**Step 7 — Print summary**
Shows run date, trigger, branch, and where to find the reports.

> Steps 5–7 use `condition: always()` so you still get whatever was produced
> even if part of the scan failed.

---

## 4. Inside `pipeline.py` (the real work)

This is what actually scans Azure and builds the reports. It runs
`run_pipeline()`.

### 4.1 Log in to Azure
Uses the **service principal** credentials (tenant + client ID + secret) to get
an OAuth token. Every Azure call uses this token.

### 4.2 Discover subscriptions
```
list_subscriptions()  →  GET /subscriptions
```
Returns **every subscription the service principal can see**. If you passed
specific IDs, it filters to those; otherwise it takes all of them.
(For "see all subscriptions" to work, the SP needs **Reader** +
**Cost Management Reader** at the Management Group level.)

### 4.3 For each subscription → discover resource groups
```
list_resource_groups(sub_id)  →  GET /subscriptions/{sub}/resourcegroups
```
If you passed a resource-group filter, only those are kept.

### 4.4 For each resource group → scan + analyse (`_scan_rg`)
This is the core loop, repeated for every RG:

1. **List + enrich resources**
   `list_resources_in_group()` — lists resources and makes extra API calls to
   fetch deep detail (VM power state, disk SKU, storage `UsedCapacity`, NSG
   rules, etc.).

2. **Fetch real billing**
   `get_resource_costs()` — this month's actual spend from Azure Cost
   Management (non-fatal if it fails; a new resource may have no spend yet).

3. **Analyse** — `analyze_resources()` runs the **two layers**:
   - **Rules Engine** (`rules_engine.py`) — deterministic, real-Azure-price
     findings (VM right-size/Reserved/Spot, disk tier, storage, etc.).
   - **GPT-4o** (`ai_analyzer.py`) — adds nuanced findings.
   - The two are **merged** and de-duplicated on `(resource_name, category)`;
     the rules-engine numbers are authoritative for cost math.

### 4.5 Write the Excel report
```python
xlsx = build_excel_bytes(analysis, rg_name, ...)
fpath = output_dir / f"{YYYYMMDD}_{subscription}_{resourceGroup}.xlsx"
fpath.write_bytes(xlsx)
```
One multi-sheet `.xlsx` per subscription+RG (Summary, Issues, Cost Breakdown,
Recommendations).

### 4.6 Save to the database
Unless `--no-db` was passed, the full analysis JSON is written to the
`analyses` table in **PostgreSQL** (`create_analysis` + `update_analysis`).
This is what appears on the web app's **History** and **Report** pages.

### 4.7 Print the summary table
At the end, a table lists every subscription/RG with: status, resources
scanned, issues found, and estimated monthly savings — plus grand totals.

---

## 5. What you get after a run

| Output | Where | Lives how long |
|---|---|---|
| **Excel files** | ADO → run → Artifacts → `cost-reports` | Per ADO retention |
| **Excel files (on VM)** | the `--output-dir` folder (e.g. `/home/azureuser/reports`) | Until deleted |
| **Database records** | PostgreSQL `analyses` table | Permanent |
| **Web app view** | History + Report pages (reads the DB) | Permanent |
| **Console log** | ADO run logs | Per ADO retention |

---

## 6. What if something fails?

- **A resource fails to enrich** → that one field is skipped; the scan continues.
- **Cost fetch fails** → treated as non-fatal; analysis still runs using real
  Azure list prices as the cost basis.
- **AI call fails for an RG** → that RG is marked `failed` in the summary; other
  RGs still complete.
- **DB is unavailable** → a warning is logged; Excel files are still produced.
- **Any RG failed** → `pipeline.py` exits with code 1 so Azure DevOps marks the
  run as failed (but artifacts are still published thanks to `always()`).

---

## 7. Quick reference — the key files

| File | Role |
|---|---|
| `azure-pipelines.yml` | The Azure DevOps schedule, steps, secrets, artifacts |
| `backend/pipeline.py` | Orchestrates the whole scan (subs → RGs → analyse → save) |
| `backend/azure_scanner.py` | Talks to Azure: lists + enriches resources, billing, metrics |
| `backend/rules_engine.py` | Deterministic, real-price cost & security findings |
| `backend/ai_analyzer.py` | Calls GPT-4o and merges its findings with the rules engine |
| `backend/excel_exporter.py` | Turns an analysis into a multi-sheet Excel file |
| `backend/db.py` | Saves/loads analyses in PostgreSQL |
