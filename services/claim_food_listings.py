# services/tools.py — add to existing module
import asyncio
import os
from typing import Optional
import httpx
from supabase import Client


async def claim_food_listing(
    supabase: Client,
    listing_id: str,
    phone: str,
) -> dict:
    """
    Tool: claim_food_listing(listing_id, phone)

    Atomically marks the listing as claimed, inserts a claim row, and fires
    an outbound Vapi call to the donor in the background. Returns enough
    detail for the assistant to read back to the caller.
    """
    phone = phone.strip()
    listing_id = str(listing_id).strip()

    # PART 1: THE ATOMIC CLAIM
    # The UPDATE ... WHERE status = 'available' is the race-safe bit.
    # Only one of two concurrent claimers gets a matching row back.

    update = (
        supabase.table("listings")
        .update({"status": "claimed", "claimed_at": "now()"})
        .eq("id", listing_id)
        .eq("status", "available")    # ← this clause is what makes it atomic
        .execute()
    )

    if not update.data:

        # PART 2: THE FAILURE-REASON LOOKUP
        # update.data is empty → either the listing doesn't exist OR
        # someone else claimed it first. Disambiguate so the assistant
        # can speak the right line.

        check = (
            supabase.table("listings")
            .select("status")
            .eq("id", listing_id)
            .execute()
        )
        if not check.data:
            return {"success": False, "reason": "listing_not_found"}
        return {"success": False, "reason": "already_claimed"}
        # end PART 2 

    listing = update.data[0]
    #end PART 1

    # PART 4 (helper call): THE DUAL-ROLE CLAIMER LOOKUP
    # _lookup_claimer figures out whether this phone belongs to a
    # food bank or a recipient — defined further down.
    claimer = await _lookup_claimer(supabase, phone)
    #  end PART 4 (call site) 

    # Record the claim.
    claim = (
        supabase.table("claims")
        .insert({
            "listing_id": listing_id,
            "claimer_phone": phone,
            "claimer_role": claimer["role"],
            "claimer_id": claimer["id"],
        })
        .execute()
    )

    # Pull donor info for the notification call.
    donor = (
        supabase.table("donors")
        .select("phone, name, business")
        .eq("id", listing["donor_id"])
        .single()
        .execute()
    ).data


    # PART 3: FIRE-AND-FORGET OUTBOUND CALL
    # asyncio.create_task schedules the coroutine and returns
    # immediately. The webhook response goes back to Vapi without
    # waiting for the donor's phone to ring.

    if donor:
        asyncio.create_task(
            _notify_donor_of_claim(donor=donor, listing=listing, claimer=claimer)
        )
    # end PART 3 

    # Return what the assistant should speak back to the claimer.
    return {
        "success": True,
        "claim_id": str(claim.data[0]["id"]) if claim.data else None,
        "food_type": listing["food_type"],
        "quantity": listing["quantity"],
        "pickup_time": listing["pickup_time"],
        "donor_business": donor["business"] if donor else None,
        "donor_phone": donor["phone"] if donor else None,
    }


# PART 4 (definition): THE DUAL-ROLE CLAIMER LOOKUP
# Checks food_banks first (verified only), then users. The role
# returned drives both the claims-table row and the wording of the
# donor notification call ("a shelter" vs "a community member").

async def _lookup_claimer(supabase: Client, phone: str) -> dict:
    """A claimer is either a verified food bank or a registered recipient."""
    fb = (
        supabase.table("food_banks")
        .select("id, name")
        .eq("phone", phone)
        .eq("status", "verified")     # ← unverified food banks can't claim
        .execute()
    )
    if fb.data:
        return {"role": "food_bank", "id": fb.data[0]["id"], "name": fb.data[0]["name"]}

    user = (
        supabase.table("users")
        .select("id")
        .eq("phone", phone)
        .execute()
    )
    if user.data:
        return {"role": "recipient", "id": user.data[0]["id"], "name": "a community member"}

    return {"role": "unknown", "id": None, "name": "someone"}
#  end PART 4 


async def _notify_donor_of_claim(donor: dict, listing: dict, claimer: dict) -> None:
    """Outbound Vapi call telling the donor who claimed their listing."""
    prompt = (
        f"You are calling on behalf of the food rescue service. "
        f"The donor at {donor['business']} listed {listing['quantity']} "
        f"{listing['food_type']} for pickup at {listing['pickup_time']}. "
        f"Tell them {claimer['name']} has claimed the listing and will pick it up. "
        f"Thank them, confirm the pickup time, and end the call. "
        f"Be brief — under 30 seconds. Match the donor's language."
    )

    payload = {
        "phoneNumberId": os.environ["VAPI_PHONE_NUMBER_ID"],
        "assistantId": os.environ["VAPI_ASSISTANT_ID"],
        "customer": {"number": donor["phone"]},
        "assistantOverrides": {"systemPrompt": prompt},
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                "https://api.vapi.ai/call/phone",
                headers={"Authorization": f"Bearer {os.environ['VAPI_API_KEY']}"},
                json=payload,
            )
    except Exception as e:
        print(f"Donor notification failed for listing {listing.get('id')}: {e}")