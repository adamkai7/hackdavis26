import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


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