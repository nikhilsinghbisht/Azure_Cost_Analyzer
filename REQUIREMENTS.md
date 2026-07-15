# What You Need to Run the App

This is the full checklist of everything required to run **AI Cloud Cost Detective** — the accounts, cloud services, credentials, and local tools. Work top to bottom; if you have all of these, the app will run.

---

## 1. Accounts & Cloud Services (the "you must have these" list)

| # | What | Why it's needed | How to get it |
|---|------|-----------------|---------------|
| 1 | **Azure subscription** | The app scans the resources *inside* your subscription(s) and reads their real cost. Nothing to analyze without one. | [portal.azure.com](https://portal.azure.com) → free or pay-as-you-go |
| 2 | **Azure PostgreSQL** (Flexible Server) | Stores users, analysis history, and every report. The backend won't start without a reachable database. | `az postgres flexible-server create ...` or create in the portal |
| 3 | **Azure Service Principal (SP)** | The app's "login" to Azure so it can scan resources & read cost without your personal `az login`. Required on any server/pipeline deployment. | `az ad sp create-for-rbac` (see §4) |
| 4 | **OpenAI API key** | Powers the GPT-4o analysis that turns raw resource data into recommendations. | [platform.openai.com](https://platform.openai.com) → API keys (needs billing enabled) |

> The **rules engine** also calls the public **Azure Retail Prices API** (`prices.azure.com`) for live pricing. This needs **no key** and **no auth** — just outbound internet access from wherever the app runs.

---

## 2. Local Tools (to run it on your machine)

| Tool | Version | Used by | Check |
|------|---------|---------|-------|
| **Python** | 3.11+ | Backend (FastAPI) + daily pipeline | `python --version` |
| **Node.js** | 18+ | Frontend (React/Vite) | `node --version` |
| **Azure CLI** | latest | Local scanning fallback (`az login`) | `az version` |
| **Git** | any | Cloning the repo | `git --version` |

The backend Python packages are pinned in `backend/requirements.txt`:

```
fastapi, uvicorn, openai, python-dotenv, asyncpg,
websockets, PyJWT, bcrypt, email-validator, requests, openpyxl
```

Install with: `pip install -r backend/requirements.txt`
Frontend packages install with: `npm install` (inside `frontend/`).

---

## 3. Configuration — the `.env` file (backend)

Copy `backend/.env.example` → `backend/.env` and fill in every value:

| Variable | What it is | Example / How to get |
|----------|-----------|----------------------|
| `OPENAI_API_KEY` | Your OpenAI secret key | `sk-...` |
| `OPENAI_MODEL` | Model to use (optional) | `gpt-4o` (default) |
| `DATABASE_URL` | Postgres connection string | `postgresql://user:pass@host:5432/db?sslmode=require` |
| `JWT_SECRET` | Random secret for login tokens | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `JWT_EXPIRY_HOURS` | How long a login lasts | `24` |
| `AZURE_TENANT_ID` | From the Service Principal | output of `az ad sp create-for-rbac` |
| `AZURE_CLIENT_ID` | From the Service Principal | same |
| `AZURE_CLIENT_SECRET` | From the Service Principal | same |
| `AZURE_SUBSCRIPTION_ID` | The subscription to scan | `az account show --query id -o tsv` |

> **Two auth modes (automatic):**
> - **SDK mode** — when the 4 `AZURE_*` vars are set (used on servers/pipeline).
> - **CLI mode** — when they're absent, it falls back to your `az login` session (handy for local dev).

---

## 4. Azure Permissions the Service Principal Needs

The SP must be able to **read** resources, **read** cost, and **read** metrics. Assign these roles on each subscription you want to scan:

```bash
# 1. Create the SP with Reader (lists resources + reads their config & metrics)
az ad sp create-for-rbac --name "CostDetective" --role "Reader" \
  --scopes /subscriptions/<SUBSCRIPTION_ID>

# 2. Add Cost Management Reader (real month-to-date $ spend)
az role assignment create --assignee <CLIENT_ID> \
  --role "Cost Management Reader" \
  --scope /subscriptions/<SUBSCRIPTION_ID>
```

| Role | Grants | Used for |
|------|--------|----------|
| **Reader** | Read resource config + **Azure Monitor metrics** | Listing resources; the CPU/usage metrics that make right-sizing evidence-based |
| **Cost Management Reader** | Read billing/cost data | The real "actual cost this month" figures |

> To scan **multiple** subscriptions in the daily pipeline, repeat the role assignments on **every** subscription (or assign at the management-group level once).

---

## 5. Network / Connectivity

The machine running the backend or pipeline must be able to reach:

- **Azure PostgreSQL** — open a firewall rule for the server's IP:
  ```bash
  az postgres flexible-server firewall-rule create \
    --resource-group <RG> --name <SERVER_NAME> \
    --rule-name allow-app --start-ip-address <IP> --end-ip-address <IP>
  ```
- **`api.openai.com`** — for the AI analysis (outbound HTTPS).
- **`management.azure.com`** — Azure resource + cost + metrics APIs (outbound HTTPS).
- **`prices.azure.com`** — public retail pricing API, no auth (outbound HTTPS).

---

## 6. Quick Start (once you have the above)

```bash
# Backend
cd backend
cp .env.example .env          # then fill in the values from §3
pip install -r requirements.txt
python main.py                # runs on http://localhost:8000

# Frontend (new terminal)
cd frontend
npm install
npm run dev                   # runs on http://localhost:5173
```

Open `http://localhost:5173`, sign up, pick a resource group, and run a scan.

---

## 7. Extra — only if you want the automated daily pipeline

| Need | Why |
|------|-----|
| **Azure DevOps project + repo** | Hosts the `azure-pipelines.yml` schedule |
| **Self-hosted agent** (a VM) | Runs the daily scan (avoids hosted-parallelism purchase) |
| **Variable group** `azure-cost-detective-secrets` | Holds the same secrets from §3 for the pipeline |

The pipeline runs `backend/pipeline.py`, scans all (or filtered) subscriptions + resource groups, writes results to Postgres, and publishes Excel reports as build artifacts. See `PIPELINE_RUN_EXPLAINED.md` for the full run walkthrough.

---

### One-line summary

> To run the app you need: **an Azure subscription**, **an Azure PostgreSQL database**, **an Azure Service Principal** (with *Reader* + *Cost Management Reader*), and **an OpenAI API key** — plus **Python 3.11+**, **Node 18+**, and the `.env` filled in.
