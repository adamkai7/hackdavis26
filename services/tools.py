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
DEFAULT_MEDIAN_INCOME = 75_000  # fallback when zip is unknown


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
