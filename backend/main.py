"""
main.py
-------
FastAPI application for AI Cloud Cost Detective.

Request flow
------------
  ①  POST /api/auth/signup  — create account, return JWT
  ①  POST /api/auth/login   — validate credentials, return JWT
  POST /api/analyze  (auth required)
    ③  azure_scanner  — az CLI scan of the resource group
    ④  db             — create analysis row (status=running)
    ⑤  ai_analyzer   — OpenAI gpt-4o cost analysis
    ⑥  db             — update row with full results (status=completed)
  GET  /api/resource-groups  (auth required)
  GET  /api/history          (auth required — scoped to calling user)
  WS   /ws/progress/{analysis_id} — live progress stream
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr

from ai_analyzer import AIAnalyzerError, OpenAIKeyMissingError, analyze_resources
from auth import create_token, get_current_user, hash_password, verify_password
from azure_scanner import (
    AzureCLIError,
    AzureCLINotInstalledError,
    AzureNotLoggedInError,
    AzureResourceGroupNotFoundError,
    get_resource_costs,
    list_resource_groups,
    list_resources_in_group,
    list_subscriptions,
)
from db import (
    close_pool,
    create_analysis,
    create_user,
    get_analyses,
    get_analysis_by_id,
    get_user_by_email,
    init_pool,
    update_analysis,
)
from excel_exporter import build_excel_bytes


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI Cloud Cost Detective API",
    description=(
        "Scans Azure resource groups via the Azure CLI, analyses costs with "
        "OpenAI gpt-4o, and persists results in Azure Managed PostgreSQL."
    ),
    version="4.0.0",
    lifespan=lifespan,
)

_extra_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"] + _extra_origins,
    allow_origin_regex=r"https://.*\.onrender\.com",   # allow all Render preview URLs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AuthRequest(BaseModel):
    email: EmailStr
    password: str

class AnalyzeRequest(BaseModel):
    resource_group: str
    analysis_id: Optional[str] = None


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self._active: dict[str, WebSocket] = {}

    async def connect(self, analysis_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._active[analysis_id] = ws

    def disconnect(self, analysis_id: str) -> None:
        self._active.pop(analysis_id, None)

    async def push(self, analysis_id: str, message: str) -> None:
        ws = self._active.get(analysis_id)
        if ws is None:
            return
        try:
            await ws.send_json({"progress": message})
        except Exception:
            self.disconnect(analysis_id)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def _map_cli_error(exc: AzureCLIError) -> HTTPException:
    if isinstance(exc, AzureCLINotInstalledError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AzureNotLoggedInError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, AzureResourceGroupNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _map_ai_error(exc: AIAnalyzerError) -> HTTPException:
    if isinstance(exc, OpenAIKeyMissingError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Auth routes  (step ①)
# ---------------------------------------------------------------------------

@app.post("/api/auth/signup", summary="Create a new account")
async def signup(body: AuthRequest):
    existing = await get_user_by_email(body.email)
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    pw_hash = hash_password(body.password)
    user = await create_user(email=body.email, password_hash=pw_hash)

    token = create_token(user["id"], user["email"])
    return {"token": token, "user": {"id": user["id"], "email": user["email"]}}


@app.post("/api/auth/login", summary="Log in and receive a JWT")
async def login(body: AuthRequest):
    user = await get_user_by_email(body.email)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_token(user["id"], user["email"])
    return {"token": token, "user": {"id": user["id"], "email": user["email"]}}


# ---------------------------------------------------------------------------
# Resource-group routes
# ---------------------------------------------------------------------------

@app.get("/api/resource-groups", summary="List Azure resource groups")
async def get_resource_groups(current_user: dict = Depends(get_current_user)):
    try:
        groups = await asyncio.wait_for(
            asyncio.to_thread(list_resource_groups),
            timeout=25,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Azure API timed out. Check SP credentials and network.")
    except AzureCLIError as exc:
        raise _map_cli_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}") from exc

    return {"resource_groups": groups, "count": len(groups)}


# ---------------------------------------------------------------------------
# Analysis route  (steps ③ ④ ⑤ ⑥)
# ---------------------------------------------------------------------------

@app.post("/api/analyze", summary="Scan and AI-analyze a resource group")
async def analyze_resource_group(
    body: AnalyzeRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Pipeline:
      ③ Azure CLI scan  →  ④ DB row created  →  ⑤ OpenAI analysis  →  ⑥ DB updated

    Pre-generate an analysis_id client-side and open the WebSocket
    /ws/progress/{analysis_id} *before* calling this endpoint to receive
    live progress messages at each stage.
    """
    rg = body.resource_group.strip()
    if not rg:
        raise HTTPException(status_code=422, detail="'resource_group' must not be empty.")

    user_id: str = current_user["sub"]
    analysis_id = body.analysis_id or str(uuid.uuid4())

    # ④ Create pending row
    await create_analysis(analysis_id=analysis_id, resource_group=rg, user_id=user_id)

    # ③ Azure CLI scan
    await manager.push(analysis_id, "Fetching resource groups...")
    await manager.push(analysis_id, f"Scanning resources in {rg}...")
    try:
        resources = await asyncio.to_thread(list_resources_in_group, rg)
    except AzureCLIError as exc:
        await update_analysis(analysis_id, status="failed")
        raise _map_cli_error(exc) from exc

    # Fetch real billing data from Azure Cost Management (best-effort, non-blocking)
    await manager.push(analysis_id, "Fetching actual cost data from Azure...")
    actual_costs = await asyncio.to_thread(get_resource_costs, rg)

    # ⑤ AI cost analysis
    await manager.push(analysis_id, "Analyzing costs with AI...")
    try:
        analysis = await asyncio.to_thread(analyze_resources, rg, resources, actual_costs)
    except AIAnalyzerError as exc:
        await update_analysis(analysis_id, status="failed", resources_scanned=len(resources))
        raise _map_ai_error(exc) from exc

    # ⑥ Persist results
    await manager.push(analysis_id, "Storing results...")
    issues_found = len(analysis.get("issues", []))
    raw_savings = analysis.get("total_estimated_monthly_savings_usd")
    estimated_savings_str = (
        f"${raw_savings:.2f}/month" if isinstance(raw_savings, (int, float)) else "Unknown"
    )

    # Attach the raw cost breakdown so the Report page can render it
    analysis["actual_cost_breakdown"] = actual_costs

    await update_analysis(
        analysis_id,
        status="completed",
        resources_scanned=len(resources),
        issues_found=issues_found,
        estimated_savings=estimated_savings_str,
        analysis_result=analysis,
    )

    await manager.push(analysis_id, "Analysis complete")

    return {
        "analysis_id": analysis_id,
        "resource_group": rg,
        "resource_count": len(resources),
        "resources": resources,
        "analysis": analysis,
    }


# ---------------------------------------------------------------------------
# History route
# ---------------------------------------------------------------------------

@app.get("/api/history", summary="Get past analyses for the authenticated user")
async def get_history(current_user: dict = Depends(get_current_user)):
    user_id: str = current_user["sub"]
    analyses = await get_analyses(user_id=user_id)
    return {"analyses": analyses, "count": len(analyses)}


# ---------------------------------------------------------------------------
# Subscriptions route
# ---------------------------------------------------------------------------

@app.get("/api/subscriptions", summary="List all Azure subscriptions accessible to the service principal")
async def get_subscriptions(current_user: dict = Depends(get_current_user)):
    try:
        subs = await asyncio.wait_for(
            asyncio.to_thread(list_subscriptions),
            timeout=25,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Azure API timed out.")
    except AzureCLIError as exc:
        raise _map_cli_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}") from exc
    return {"subscriptions": subs, "count": len(subs)}


# ---------------------------------------------------------------------------
# Excel export route
# ---------------------------------------------------------------------------

@app.get(
    "/api/analyses/{analysis_id}/export/excel",
    summary="Download analysis report as Excel (.xlsx)",
    response_class=Response,
)
async def export_excel(
    analysis_id: str,
    current_user: dict = Depends(get_current_user),
):
    row = await get_analysis_by_id(analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    if row["status"] != "completed" or not row.get("analysis_result"):
        raise HTTPException(status_code=400, detail="Analysis is not yet complete.")

    analysis_result = row["analysis_result"]
    resource_group  = row["resource_group"]
    resource_count  = row.get("resources_scanned")

    # subscription_id may be embedded in the result by the pipeline
    subscription_id = analysis_result.get("subscription_id")

    xlsx_bytes = await asyncio.to_thread(
        build_excel_bytes,
        analysis_result,
        resource_group,
        resource_count,
        subscription_id,
    )

    safe_rg   = resource_group.replace(" ", "_").replace("/", "-")
    filename  = f"azure-cost-report_{safe_rg}_{analysis_id[:8]}.xlsx"

    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# WebSocket progress stream
# ---------------------------------------------------------------------------

@app.websocket("/ws/progress/{analysis_id}")
async def websocket_progress(ws: WebSocket, analysis_id: str):
    """
    Connect *before* POST /api/analyze to receive progress messages.
    Pushes JSON: { "progress": "<message>" }
    """
    await manager.connect(analysis_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(analysis_id)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
