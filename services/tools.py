# services/tools.py
from typing import TypedDict
from supabase import Client
import asyncio
import os
from typing import Optional
import httpx


# Federal Poverty Level + median-income lookup


# 2026 HHS Poverty Guidelines (48 contiguous states + DC).
# Verify against aspe.hhs.gov/poverty-guidelines before demo if precision matters.
FPL_BASE = {
    1: 15_650,
    2: 21_150,
    3: 26_650,
    4: 32_150,
    5: 37_650,
    6: 43_150,
    7: 48_650,
    8: 54_150,
}
FPL_PER_ADDITIONAL = 5_500

# Median household income by ZIP — seed with demo-relevant zips.
# Source: ACS 5-year estimates. Add more before the demo if your judges'
# test calls might come from elsewhere.
ZIP_MEDIAN_INCOME = {
    "95616": 84_000,   # Davis
    "95618": 110_000,  # West Davis
    "95817": 56_000,   # Sacramento (Oak Park)
    "95820": 52_000,   # Sacramento
    "95823": 61_000,   # South Sacramento
    "95824": 49_000,   # Fruitridge
    "95838": 47_000,   # North Sacramento (Del Paso)
}
DEFAULT_MEDIAN_INCOME = 75_000

# ============================================================
# Type definitions
# ============================================================
class TierResult(TypedDict):
    tier: str        # "A" (highest priority), "B" (moderate), "C" (general)
    label: str       # human-readable, for the assistant to speak
    fpl_ratio: float # diagnostic — median income / FPL threshold


def fpl_threshold(household_size: int) -> int:
    """Federal poverty threshold for a given household size."""
    n = max(1, household_size)
    if n <= 8:
        return FPL_BASE[n]
    return FPL_BASE[8] + (n - 8) * FPL_PER_ADDITIONAL


def assign_income_tier(zip_code: str, household_size: int) -> TierResult:
    """
    Estimates need tier from area median income vs. household FPL.
    No personal income is collected — this is an area-based proxy.
    """
    median = ZIP_MEDIAN_INCOME.get(zip_code, DEFAULT_MEDIAN_INCOME)
    fpl = fpl_threshold(household_size)
    ratio = median / fpl

    if ratio < 1.3:
        return {"tier": "A", "label": "high-priority", "fpl_ratio": ratio}
    if ratio < 2.0:
        return {"tier": "B", "label": "moderate-priority", "fpl_ratio": ratio}
    return {"tier": "C", "label": "general", "fpl_ratio": ratio}


# Tool handler
async def register_new_user(
    supabase: Client,
    phone: str,
    zip_code: str,
    household_size: int,
    lang: str = "en",
) -> dict:
    """
    Tool: register_new_user(phone, zip, household_size)

    Inserts a row into `users`, computes income tier via zip + household
    size, and returns the tier so the assistant can use it on its next
    turn (typically followed by get_available_food).

    Returns a dict that gets JSON-serialized into the Vapi webhook
    response under {"results": [{"toolCallId": ..., "result": <this>}]}.
    """
    # Vapi sometimes sends numeric args as strings — coerce defensively.
    try:
        household_size = int(household_size)
    except (TypeError, ValueError):
        household_size = 1

    # Accept "95616" or "95616-1234"; keep only the 5-digit prefix.
    zip_code = str(zip_code).strip().split("-")[0][:5]
    phone = phone.strip()

    tier = assign_income_tier(zip_code, household_size)

    row = {
        "phone": phone,
        "zip": zip_code,
        "household_size": household_size,
        "income_tier": tier["tier"],
        "lang": lang,
        # `id`, `status`, `created_at` set by DB defaults
    }

    # Upsert on phone — if the caller hangs up mid-onboarding and calls
    # back, this updates rather than throwing on the unique constraint.
    result = (
        supabase.table("users")
        .upsert(row, on_conflict="phone")
        .execute()
    )

    if not result.data:
        raise RuntimeError(f"users upsert returned no row: {result}")

    return {
        "user_id": str(result.data[0]["id"]),
        "tier": tier["tier"],
        "label": tier["label"],
        "fpl_ratio": round(tier["fpl_ratio"], 2),
        "registered": True,
    }


# ============================================================
# Tool 3: register_donor
# ============================================================

async def register_donor(
    supabase: Client,
    phone: str,
    name: str,
    business: str,
    zip: str,
    lang: str = "en",
) -> dict:
    """
    Fires after assistant collects donor details from a new donor.
    Inserts donor row. Called if identify_caller returns unknown and
    caller says they want to donate.
    """
    phone = normalize_phone(phone)
    zip = str(zip).strip().split("-")[0][:5]

    existing = (
        supabase.table(DONORS_TABLE)
        .select("id, name")
        .eq("phone", phone)
        .execute()
    )
    if existing.data:
        return {"result": f"Welcome back, {existing.data[0]['name']}. You're already registered as a donor."}

    result = (
        supabase.table(DONORS_TABLE)
        .insert({
            "phone": phone,
            "name": name,
            "address": business,
            "zip": zip,
            "lang": lang,
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(f"donors insert returned no row: {result}")

    if lang == "es":
        reply = f"Gracias, {name}. Tu restaurante o tienda ha sido registrado en MiComida. Cuando tengas comida sobrante, simplemente llama y dinos qué tienes."
    else:
        reply = f"Thank you, {name}. Your business has been registered with MiComida. Whenever you have surplus food, just call us and tell us what you have."

    return {"result": reply}


# ============================================================
# Tool 4: get_available_food
# ============================================================

async def get_available_food(supabase: Client, zip: str, income_tier: str) -> dict:
    """
    Fires once recipient is registered or identified.
    Queries listings by zip and status, returns list the assistant reads aloud.
    """
    allowed_statuses = ["food_bank_window", "open"] if income_tier == "free" else ["open"]
    now = datetime.now(timezone.utc).isoformat()

    result = (
        supabase.table(LISTINGS_TABLE)
        .select("id, food_type, quantity, pickup_addr, pickup_time, expiry_time")
        .eq("zip", zip)
        .in_("status", allowed_statuses)
        .or_(f"expiry_time.gt.{now},expiry_time.is.null")
        .order("expiry_time", desc=False)
        .limit(3)
        .execute()
    )

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


# ============================================================
# Tool 5: save_food_listing
# ============================================================

async def save_food_listing(
    supabase: Client,
    food_type: str,
    quantity: str,
    pickup_time: str,
    zip_code: str,
    donor_phone: str,
) -> dict:
    """
    Fires the moment assistant has all 4 fields from a donor.
    Inserts listing to Supabase, returns listing_id.
    """
    donor_phone = normalize_phone(donor_phone)
    zip_code = str(zip_code).strip().split("-")[0][:5]

    parsed_pickup: str | None = None
    if pickup_time:
        try:
            parsed_pickup = datetime.fromisoformat(str(pickup_time)).isoformat()
        except ValueError:
            parsed_pickup = str(pickup_time).strip()

    result = (
        supabase.table(LISTINGS_TABLE)
        .insert({
            "donor_phone": donor_phone,
            "food_type": str(food_type).strip(),
            "quantity": str(quantity).strip(),
            "zip": zip_code,
            "pickup_time": parsed_pickup,
            "status": "available",
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(f"listings insert returned no row: {result}")

    listing = result.data[0]
    return {
        "listing_id": str(listing["id"]),
        "donor_phone": donor_phone,
        "food_type": food_type,
        "quantity": quantity,
        "zip": zip_code,
        "pickup_time": parsed_pickup,
        "status": listing["status"],
    }


# ============================================================
# Tool 6: notify_food_banks
# ============================================================

async def notify_food_banks(supabase: Client, listing_id: str, zip: str) -> dict:
    """
    Fires immediately after save_food_listing returns a listing_id.
    Queries verified food banks in zip, triggers outbound Vapi calls
    to each in their preferred language.
    """
    result = (
        supabase.table(FOOD_BANKS_TABLE)
        .select("phone, preferred_lang, name")
        .eq("zip", zip)
        .eq("status", "verified")
        .execute()
    )
    food_banks = result.data
    if not food_banks:
        return {"result": "No verified food banks found in this area."}

    listing = (
        supabase.table(LISTINGS_TABLE)
        .select("food_type, quantity, pickup_time, pickup_addr")
        .eq("id", listing_id)
        .single()
        .execute()
    )
    if not listing.data:
        return {"result": "Listing not found."}

    l = listing.data
    food_desc = f"{l['quantity']} {l['food_type']}"
    pickup = l.get("pickup_time") or "time not specified"
    addr = l.get("pickup_addr") or "address not specified"

    supabase.table(LISTINGS_TABLE).update({"status": "food_bank_window"}).eq("id", listing_id).execute()

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

            await client.post(
                "https://api.vapi.ai/call/phone",
                headers={"Authorization": f"Bearer {vapi_key}"},
                json={
                    "assistantId": assistant_id,
                    "assistantOverrides": {
                        "systemPrompt": prompt,
                        "variable": {"listing_id": listing_id},
                    },
                    "phoneNumberId": phone_number_id,
                    "customer": {"number": bank["phone"]},
                },
            )

            supabase.table("alert_log").insert({
                "listing_id": listing_id,
                "food_bank_phone": bank["phone"],
            }).execute()

            called.append(bank["name"])

    return {"result": f"Notified {len(called)} food bank(s): {', '.join(called)}. They are being called now."}


# ============================================================
# Tool 7: claim_food_listing  (NEW: search-by-description)
# ============================================================

def _distance_score(
    listing: dict,
    caller_zip: str | None,
    hint_zip: str | None,
    hint_text: str,
) -> int:
    """
    Lower = better match. Heuristic, no geocoding.

    0  hint is a zip, matches listing zip exactly
    1  caller's home zip matches listing zip
    2  hint text appears in listing pickup_addr (case-insensitive)
    3  hint zip prefix (3 digits) matches listing zip prefix
    4  caller zip prefix matches listing zip prefix
    5  anything else available
    """
    listing_zip = str(listing.get("zip") or "")[:5]
    listing_addr = str(listing.get("pickup_addr") or "").lower()
    hint_lower = hint_text.lower().strip()

    if hint_zip and listing_zip == hint_zip:
        return 0
    if caller_zip and listing_zip == caller_zip:
        return 1
    if hint_lower and len(hint_lower) >= 3 and hint_lower in listing_addr:
        return 2
    if hint_zip and len(listing_zip) >= 3 and listing_zip[:3] == hint_zip[:3]:
        return 3
    if caller_zip and len(listing_zip) >= 3 and listing_zip[:3] == caller_zip[:3]:
        return 4
    return 5


async def _lookup_caller_zip(supabase: Client, phone: str) -> str | None:
    """Best-effort zip lookup across users → donors → food_banks."""
    for table in (USERS_TABLE, DONORS_TABLE, FOOD_BANKS_TABLE):
        try:
            r = (
                supabase.table(table)
                .select("zip")
                .eq("phone", phone)
                .limit(1)
                .execute()
            )
            if r and r.data:
                z = _normalize_zip5(str(r.data[0].get("zip") or ""))
                if z:
                    return z
        except Exception:
            continue
    return None


async def claim_food_listing(
    supabase: Client,
    food_type: str,
    pickup_hint: str,
    phone: str,
) -> dict:
    """
    Fires when a recipient or food bank confirms they want food matching a
    spoken description. The assistant passes through what the caller said
    rather than tracking listing IDs across turns.

    Args:
        food_type:   what the caller wants — "burritos", "pizza", "produce"
        pickup_hint: where — neighborhood, address fragment, or zip ("El Centro", "95820")
        phone:       caller's phone

    Flow: search matching listings → rank by distance → atomically claim
    the closest. On race loss, fall through to next candidate.
    """
    phone = normalize_phone(phone)
    food_query = str(food_type).strip()
    hint = str(pickup_hint or "").strip()

    if not food_query:
        return {"success": False, "reason": "missing_food_type"}

    # ─── Resolve location context for ranking ──────────────────
    caller_zip = await _lookup_caller_zip(supabase, phone)
    hint_zip = _normalize_zip5(hint) if hint else None

    # ─── Query candidate listings ──────────────────────────────
    now = datetime.now(timezone.utc).isoformat()
    candidates_resp = (
        supabase.table(LISTINGS_TABLE)
        .select(
            "id, food_type, quantity, pickup_addr, pickup_time, "
            "expiry_time, zip, donor_phone, status"
        )
        .ilike("food_type", f"%{food_query}%")
        .in_("status", ["available", "food_bank_window", "open"])
        .or_(f"expiry_time.gt.{now},expiry_time.is.null")
        .limit(20)
        .execute()
    )
    candidates = candidates_resp.data or []
    if not candidates:
        return {
            "success": False,
            "reason": "no_matching_listings",
            "message": f"No '{food_query}' is available right now.",
        }

    # ─── Rank: distance score, then expiry-soonest as tiebreak ──
    def sort_key(c: dict) -> tuple:
        return (
            _distance_score(c, caller_zip, hint_zip, hint),
            c.get("expiry_time") or "9999",  # null expiry sorts last
        )
    ranked = sorted(candidates, key=sort_key)

    # ─── Atomic claim with race-loss fallthrough ───────────────
    listing = None
    for candidate in ranked:
        update = (
            supabase.table(LISTINGS_TABLE)
            .update({"status": "claimed"})
            .eq("id", candidate["id"])
            .in_("status", ["available", "food_bank_window", "open"])
            .execute()
        )
        if update.data:
            listing = update.data[0]
            break

    if listing is None:
        return {
            "success": False,
            "reason": "all_candidates_claimed",
            "message": "Those listings were just claimed by someone else. Try again in a moment.",
        }

    # ─── Insert claim row + notify donor (unchanged from before) ─
    claimer = await _lookup_claimer(supabase, phone)

    claim = (
        supabase.table(CLAIMS_TABLE)
        .insert({
            "listing_id": listing["id"],
            "claimer_phone": phone,
            "claimer_type": claimer["role"],
        })
        .execute()
    )

    donor = (
        supabase.table(DONORS_TABLE)
        .select("phone, name, address")
        .eq("phone", listing["donor_phone"])
        .single()
        .execute()
    ).data

    if donor:
        asyncio.create_task(
            _notify_donor_of_claim(donor=donor, listing=listing, claimer=claimer)
        )

    return {
        "success": True,
        "claim_id": str(claim.data[0]["id"]) if claim.data else None,
        "listing_id": str(listing["id"]),
        "food_type": listing.get("food_type"),
        "quantity": listing.get("quantity"),
        "pickup_addr": listing.get("pickup_addr"),
        "pickup_time": listing.get("pickup_time"),
        "donor_phone": donor["phone"] if donor else None,
    }

# ============================================================
# Tool 8: register_food_bank
# ============================================================

async def register_food_bank(
    supabase: Client,
    phone: str,
    name: str,
    ein: str | None,
    address: str,
    zip_code: str,
    lang: str = "en",
) -> dict:
    """
    Fires after assistant collects contact info from a food bank caller,
    before verify_organization runs. Inserts food_bank row with status pending.
    """
    phone_n = normalize_phone(phone)
    param_digits = _ein_digits_if_valid(ein)
    ein_value: str | None = _ein_display_from_digits(param_digits) if param_digits else None

    existing = await asyncio.to_thread(
        lambda: (
            supabase.table(FOOD_BANKS_TABLE)
            .select("ein")
            .eq("phone", phone_n)
            .maybe_single()
            .execute()
        )
    )
    existing_row = getattr(existing, "data", None) if existing is not None else None
    if ein_value is None and existing_row and existing_row.get("ein"):
        prev = _ein_digits_if_valid(str(existing_row["ein"]))
        if prev:
            ein_value = _ein_display_from_digits(prev)

    result = await asyncio.to_thread(
        lambda: (
            supabase.table(FOOD_BANKS_TABLE)
            .upsert(
                {
                    "phone": phone_n,
                    "name": str(name).strip(),
                    "ein": ein_value,
                    "address": str(address).strip(),
                    "zip": str(zip_code).strip().split("-")[0][:5],
                    "preferred_lang": str(lang).strip() or "en",
                    "status": "pending",
                },
                on_conflict="phone",
            )
            .execute()
        )
    )

    row_data = getattr(result, "data", None) if result is not None else None
    if not row_data:
        raise RuntimeError(f"food_banks upsert returned no row: {result}")

    food_bank = row_data[0]
    return {
        "food_bank_id": str(food_bank["id"]),
        "name": name,
        "ein": ein_value,
        "address": address,
        "zip": zip_code,
        "lang": lang,
        "status": food_bank["status"],
    }