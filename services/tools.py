import os
from typing import TypedDict
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── Income tier logic ────────────────────────────────────────────────

FPL_BASE = {1: 15_650, 2: 21_150, 3: 26_650, 4: 32_150,
            5: 37_650, 6: 43_150, 7: 48_650, 8: 54_150}
FPL_PER_ADDITIONAL = 5_500

ZIP_MEDIAN_INCOME = {
    "95616": 84_000, "95618": 110_000, "95817": 56_000,
    "95820": 52_000, "95823": 61_000, "95824": 49_000, "95838": 47_000,
}
DEFAULT_MEDIAN_INCOME = 75_000


class TierResult(TypedDict):
    tier: str
    label: str
    fpl_ratio: float


def fpl_threshold(household_size: int) -> int:
    n = max(1, household_size)
    if n <= 8:
        return FPL_BASE[n]
    return FPL_BASE[8] + (n - 8) * FPL_PER_ADDITIONAL


def assign_income_tier(zip_code: str, household_size: int) -> TierResult:
    median = ZIP_MEDIAN_INCOME.get(zip_code, DEFAULT_MEDIAN_INCOME)
    fpl = fpl_threshold(household_size)
    ratio = median / fpl
    if ratio < 1.3:
        return {"tier": "free", "label": "high-priority", "fpl_ratio": ratio}
    if ratio < 2.0:
        return {"tier": "discount", "label": "moderate-priority", "fpl_ratio": ratio}
    return {"tier": "discount", "label": "general", "fpl_ratio": ratio}


# ── Tool handlers ────────────────────────────────────────────────────

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
        lines.append(f"{item['food_type']}, {item['quantity']}, pickup at {addr} by {pickup}")

    summary = "Here's what's available near you: " + "; ".join(lines)
    summary += ". Would you like to claim any of these?"
    return {"result": summary}


async def register_new_user(phone: str, zip_code: str, household_size: int, lang: str = "en") -> dict:
    try:
        household_size = int(household_size)
    except (TypeError, ValueError):
        household_size = 1

    zip_code = str(zip_code).strip().split("-")[0][:5]
    phone = phone.strip()
    tier = assign_income_tier(zip_code, household_size)

    row = {
        "phone": phone,
        "zip": zip_code,
        "household_size": household_size,
        "income_tier": tier["tier"],
        "lang": lang,
    }

    result = supabase.table("users").upsert(row, on_conflict="phone").execute()

    if not result.data:
        raise RuntimeError(f"users upsert returned no row: {result}")

    return {
        "user_id": str(result.data[0]["id"]),
        "tier": tier["tier"],
        "label": tier["label"],
        "registered": True,
    }
    import httpx

async def notify_food_banks(listing_id: str, zip: str) -> dict:
    
    # Get all verified food banks in this zip
    result = supabase.table("food_banks")\
        .select("phone, preferred_lang, name")\
        .eq("zip", zip)\
        .eq("status", "verified")\
        .execute()

    food_banks = result.data

    if not food_banks:
        return {"result": "No verified food banks found in this area."}

    # Get the listing details so we can describe it in the call
    listing = supabase.table("listings")\
        .select("food_type, quantity, pickup_time, pickup_addr")\
        .eq("id", listing_id)\
        .single()\
        .execute()

    if not listing.data:
        return {"result": "Listing not found."}

    l = listing.data
    food_desc = f"{l['quantity']} {l['food_type']}"
    pickup = l.get("pickup_time") or "time not specified"
    addr = l.get("pickup_addr") or "address not specified"

    # Update listing status to food_bank_window
    supabase.table("listings")\
        .update({"status": "food_bank_window"})\
        .eq("id", listing_id)\
        .execute()

    # Call each food bank via Vapi outbound
    vapi_key = os.getenv("VAPI_API_KEY")
    assistant_id = os.getenv("VAPI_ASSISTANT_ID")
    phone_number_id = os.getenv("VAPI_PHONE_NUMBER_ID")

    called = []

    async with httpx.AsyncClient() as client:
        for bank in food_banks:
            lang = bank.get("preferred_lang", "en")

            if lang == "es":
                prompt = f"Estás llamando en nombre de MiComida. Un donante ha listado {food_desc} para recoger en {addr} a las {pickup}. ¿Puede su banco de alimentos reclamar esta donación? Si es así, confirme ahora."
            else:
                prompt = f"You are calling on behalf of MiComida. A donor has listed {food_desc} for pickup at {addr} at {pickup}. Can your food bank claim this donation? If yes, please confirm now."

            payload = {
                "assistantId": assistant_id,
                "assistantOverrides": {
                    "systemPrompt": prompt,
                    "variable": {"listing_id": listing_id}
                },
                "phoneNumberId": phone_number_id,
                "customer": {
                    "number": bank["phone"]
                }
            }

            response = await client.post(
                "https://api.vapi.ai/call/phone",
                headers={"Authorization": f"Bearer {vapi_key}"},
                json=payload
            )

            # Log the alert
            supabase.table("alert_log").insert({
                "listing_id": listing_id,
                "food_bank_phone": bank["phone"]
            }).execute()

            called.append(bank["name"])

    names = ", ".join(called)
    return {"result": f"Notified {len(called)} food bank(s): {names}. They are being called now."}

async def register_donor(phone: str, name: str, business: str, zip: str, lang: str = "en") -> dict:
    
    phone = phone.strip()
    zip = str(zip).strip().split("-")[0][:5]

    # Check if already registered
    existing = supabase.table("donors")\
        .select("id, name")\
        .eq("phone", phone)\
        .execute()

    if existing.data:
        return {"result": f"Welcome back, {existing.data[0]['name']}. You're already registered as a donor."}

    # Insert new donor row
    row = {
        "phone": phone,
        "name": name,
        "address": business,
        "zip": zip,
        "lang": lang,
    }

    result = supabase.table("donors")\
        .insert(row)\
        .execute()

    if not result.data:
        raise RuntimeError(f"donors insert returned no row: {result}")

    if lang == "es":
        reply = f"Gracias, {name}. Tu restaurante o tienda ha sido registrado en MiComida. Cuando tengas comida sobrante, simplemente llama y dinos qué tienes."
    else:
        reply = f"Thank you, {name}. Your business has been registered with MiComida. Whenever you have surplus food, just call us and tell us what you have."

    return {"result": reply}