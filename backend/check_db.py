import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def check():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL"))

    users = await conn.fetch("SELECT id, email FROM users ORDER BY created_at")
    print(f"Users ({len(users)}):")
    for u in users:
        uid = str(u["id"])
        print(f"  {uid}  {u['email']}")

    analyses = await conn.fetch(
        "SELECT id, user_id, resource_group, status FROM analyses ORDER BY created_at DESC"
    )
    print(f"\nAnalyses ({len(analyses)}):")
    for a in analyses:
        uid = str(a["user_id"]) if a["user_id"] else "NULL"
        print(f"  status={a['status']}  rg={a['resource_group']}  user_id={uid}")

    await conn.close()

asyncio.run(check())
