import os
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


async def get_available_food(zip: str, income_tier: str) -> dict:
    
    if income_tier == "free":
        allowed_statuses = ["food_bank_window", "open"]
    else:
        allowed_statuses = ["open"]

    now = datetime.now(timezone.utc).isoformat()

    result = supabase.table("listings")\
        .select("id, food_type, quantity, pickup_addr, pickup_time, expiry_time")\
        .eq("zip", zip)\
        .in_("status", allowed_statuses)\
        .or_(f"expiry_time.gt.{now},expiry_time.is.null")\
        .order("expiry_time", ascending=True)\
        .limit(3)\
        .execute()

    listings = result.data

    if not listings:
        return {"result": "There's no food available near your area right now. Please check back soon."}

    lines = []
    for item in listings:
        pickup = item.get("pickup_time") or "time not specified"
        addr = item.get("pickup_addr") or "address not specified"
        lines.append(
            f"{item['food_type']}, {item['quantity']}, pickup at {addr} by {pickup}"
        )

    summary = "Here's what's available near you: " + "; ".join(lines)
    summary += ". Would you like to claim any of these?"

    return {"result": summary}