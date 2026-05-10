import os
import httpx
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


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

    # Get the listing details
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

            await client.post(
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