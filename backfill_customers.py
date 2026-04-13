import os
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv()
load_dotenv(HERE.parent / '.env')
load_dotenv(HERE.parent.parent / 'shopdesk-dashboard' / '.env.local')
load_dotenv(HERE.parent.parent / 'shopdesk-dashboard' / '.env.production')

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or os.getenv("NEXT_PUBLIC_SUPABASE_URL", "")).strip().strip('"').strip("'")
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")).strip().strip('"').strip("'")
PAGE_SIZE = 1000


def normalize_phone(phone: Optional[str]) -> str:
    raw = (phone or "").strip()
    if not raw:
        return ""
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    if raw.startswith('+'):
        return raw
    return digits or raw


async def fetch_all(client: httpx.AsyncClient, table: str, select: str) -> List[dict]:
    rows: List[dict] = []
    offset = 0
    while True:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}?select={select}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Range": f"{offset}-{offset + PAGE_SIZE - 1}",
            },
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


async def main() -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY")

    async with httpx.AsyncClient() as client:
        shops = await fetch_all(client, "shops", "id")
        calls = await fetch_all(client, "calls", "shop_id,caller_phone,caller_name,started_at,created_at")
        bookings = await fetch_all(client, "bookings", "shop_id,customer_phone,customer_name,vehicle,appointment_at,created_at")

        calls_by_shop: Dict[str, List[dict]] = defaultdict(list)
        bookings_by_shop: Dict[str, List[dict]] = defaultdict(list)

        for row in calls:
            calls_by_shop[row["shop_id"]].append(row)
        for row in bookings:
            bookings_by_shop[row["shop_id"]].append(row)

        upserts: List[dict] = []

        for shop in shops:
            shop_id = shop["id"]
            customers: Dict[str, dict] = {}

            for row in calls_by_shop.get(shop_id, []):
                phone = normalize_phone(row.get("caller_phone"))
                if not phone:
                    continue
                customer = customers.setdefault(phone, {
                    "shop_id": shop_id,
                    "phone": phone,
                    "name": None,
                    "vehicle": None,
                    "last_visit": None,
                    "visit_count": 0,
                })
                customer["visit_count"] += 1
                if row.get("caller_name"):
                    customer["name"] = row["caller_name"]
                when = row.get("started_at") or row.get("created_at")
                if when and (customer["last_visit"] is None or when > customer["last_visit"]):
                    customer["last_visit"] = when

            for row in bookings_by_shop.get(shop_id, []):
                phone = normalize_phone(row.get("customer_phone"))
                if not phone:
                    continue
                customer = customers.setdefault(phone, {
                    "shop_id": shop_id,
                    "phone": phone,
                    "name": None,
                    "vehicle": None,
                    "last_visit": None,
                    "visit_count": 1,
                })
                if row.get("customer_name"):
                    customer["name"] = row["customer_name"]
                if row.get("vehicle"):
                    customer["vehicle"] = row["vehicle"]
                when = row.get("appointment_at") or row.get("created_at")
                if when and (customer["last_visit"] is None or when > customer["last_visit"]):
                    customer["last_visit"] = when
                if customer["visit_count"] < 1:
                    customer["visit_count"] = 1

            for customer in customers.values():
                upserts.append({
                    "shop_id": customer["shop_id"],
                    "name": customer["name"] or "Unknown Customer",
                    "phone": customer["phone"],
                    "vehicle": customer["vehicle"],
                    "last_visit": customer["last_visit"],
                    "visit_count": max(customer["visit_count"], 1),
                })

        for i in range(0, len(upserts), 500):
            chunk = upserts[i:i + 500]
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/customers?on_conflict=shop_id,phone",
                json=chunk,
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=representation",
                },
                timeout=60,
            )
            resp.raise_for_status()

        print(f"Backfilled {len(upserts)} customer records across {len(shops)} shops.")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
