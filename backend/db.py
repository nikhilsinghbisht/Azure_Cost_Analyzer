"""
db.py
-----
asyncpg connection pool and query helpers for Azure Managed PostgreSQL.

Tables
------
  users     — id, email, password_hash, created_at
  analyses  — id, user_id, resource_group, resources_scanned, issues_found,
              estimated_savings, analysis_result (jsonb), status, created_at
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# Module-level pool reference — initialised in init_pool(), torn down in close_pool()
_pool: asyncpg.Pool | None = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register JSON/JSONB codecs so asyncpg decodes those columns to Python dicts."""
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def init_pool() -> None:
    """Create the asyncpg connection pool and ensure tables exist."""
    global _pool

    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Copy .env.example to .env and fill in the connection string."
        )

    _pool = await asyncpg.create_pool(
        dsn,
        min_size=2,
        max_size=10,
        init=_init_connection,
    )
    await _create_tables()


async def close_pool() -> None:
    """Gracefully close the connection pool on server shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — did lifespan run?")
    return _pool


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT        UNIQUE NOT NULL,
    password_hash TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_DDL_ANALYSES = """
CREATE TABLE IF NOT EXISTS analyses (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID        REFERENCES users(id) ON DELETE SET NULL,
    resource_group    TEXT        NOT NULL,
    resources_scanned INT         NOT NULL DEFAULT 0,
    issues_found      INT         NOT NULL DEFAULT 0,
    estimated_savings TEXT,
    analysis_result   JSONB,
    status            TEXT        NOT NULL DEFAULT 'pending',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def _create_tables() -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(_DDL_USERS)
        await conn.execute(_DDL_ANALYSES)


# ---------------------------------------------------------------------------
# Users CRUD
# ---------------------------------------------------------------------------

async def create_user(email: str, password_hash: str) -> dict[str, Any]:
    """Insert a new user row and return it as a plain dict."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (email, password_hash)
            VALUES ($1, $2)
            RETURNING id, email, password_hash, created_at
            """,
            email,
            password_hash,
        )
    return _row_to_dict(row)


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Return the user row for the given email, or None if not found."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, password_hash, created_at FROM users WHERE email = $1",
            email,
        )
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Analyses CRUD
# ---------------------------------------------------------------------------

async def create_analysis(
    *,
    analysis_id: str,
    resource_group: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Insert a new analysis row with status='running'.
    Returns the full row as a plain dict.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO analyses (id, user_id, resource_group, status)
            VALUES ($1, $2, $3, 'running')
            RETURNING id, user_id, resource_group, resources_scanned,
                      issues_found, estimated_savings, analysis_result,
                      status, created_at
            """,
            uuid.UUID(analysis_id),
            uuid.UUID(user_id) if user_id else None,
            resource_group,
        )
    return _row_to_dict(row)


async def update_analysis(
    analysis_id: str,
    *,
    status: str,
    resources_scanned: int = 0,
    issues_found: int = 0,
    estimated_savings: str | None = None,
    analysis_result: dict[str, Any] | None = None,
) -> None:
    """Update progress fields and final status on an existing analysis row."""
    result_json = json.dumps(analysis_result) if analysis_result is not None else None

    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE analyses
            SET status            = $2,
                resources_scanned = $3,
                issues_found      = $4,
                estimated_savings = $5,
                analysis_result   = $6::jsonb
            WHERE id = $1
            """,
            uuid.UUID(analysis_id),
            status,
            resources_scanned,
            issues_found,
            estimated_savings,
            result_json,
        )


async def get_today_analysis(resource_group: str, subscription_id: str | None = None) -> dict[str, Any] | None:
    """
    Return the most recent completed analysis for this resource_group
    that was created today (UTC). Used by the pipeline to skip re-running
    the AI when a result already exists for today.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, resource_group, resources_scanned,
                   issues_found, estimated_savings, analysis_result,
                   status, created_at
            FROM   analyses
            WHERE  resource_group = $1
              AND  status = 'completed'
              AND  created_at >= NOW()::date
            ORDER  BY created_at DESC
            LIMIT  1
            """,
            resource_group,
        )
    if not row:
        return None
    result = _row_to_dict(row)
    # If a subscription filter is given, make sure this result matches
    if subscription_id:
        ar = result.get("analysis_result") or {}
        if ar.get("subscription_id") and ar["subscription_id"] != subscription_id:
            return None
    return result


async def get_analysis_by_id(analysis_id: str) -> dict[str, Any] | None:
    """Return a single analysis row by its UUID, or None if not found."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, resource_group, resources_scanned,
                   issues_found, estimated_savings, analysis_result,
                   status, created_at
            FROM   analyses
            WHERE  id = $1
            """,
            uuid.UUID(analysis_id),
        )
    return _row_to_dict(row) if row else None


async def get_analyses(user_id: str | None = None) -> list[dict[str, Any]]:
    """
    Return analyses ordered by most recent first.
    When user_id is given, scope the query to that user only.
    """
    async with get_pool().acquire() as conn:
        if user_id:
            rows = await conn.fetch(
                """
                SELECT id, user_id, resource_group, resources_scanned,
                       issues_found, estimated_savings, analysis_result,
                       status, created_at
                FROM   analyses
                WHERE  user_id = $1
                ORDER  BY created_at DESC
                """,
                uuid.UUID(user_id),
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, user_id, resource_group, resources_scanned,
                       issues_found, estimated_savings, analysis_result,
                       status, created_at
                FROM   analyses
                ORDER  BY created_at DESC
                """
            )

    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert an asyncpg Record to a JSON-safe plain dict."""
    d = dict(row)

    for key in ("id", "user_id"):
        if d.get(key) is not None:
            d[key] = str(d[key])

    if isinstance(d.get("created_at"), datetime):
        d["created_at"] = d["created_at"].isoformat()

    # Ensure JSONB column is a Python dict, not a raw JSON string.
    # The pool codec handles this for new connections; this is a safety net.
    ar = d.get("analysis_result")
    if isinstance(ar, str):
        try:
            d["analysis_result"] = json.loads(ar)
        except (json.JSONDecodeError, TypeError):
            d["analysis_result"] = None

    return d
