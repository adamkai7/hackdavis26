# Mesa

A multilingual AI voice platform connecting surplus food from restaurants 
and grocery stores to verified food shelters and community members in need.
Built at HackDavis 2026 in 24 hours. Our number is (984) 302-3261.

## The Problem

1 in 7 Americans face food insecurity while 30% of grocery store food gets 
thrown away. The people who need food most are locked out of every existing 
solution — no smartphone, no data plan, no documentation, no English. We 
built something with zero barriers. One phone call. Any language. Done.

## How It Works

**Donor flow:** A restaurant calls one number and says "50 burritos ready 
at 9pm." Mesa's AI parses the listing, saves it to the database, and 
immediately calls nearby verified food banks to alert them. The food bank 
says yes, the listing is claimed, and the donor gets a callback confirming 
pickup.

**Recipient flow:** Anyone calls the same number in English or Spanish, gets 
connected to nearby shelters with available food, and finds out exactly where 
to go — no account, no ID, no app required.

## Architecture

Mesa is a fully agentic system. Nine tool handlers cover the complete flow: 
caller identification, donor registration, food bank registration, listing 
creation, outbound food bank notification, listing claims, organization 
verification, and nearby shelter lookup. The AI autonomously classifies 
donations, routes to the highest-need recipient, escalates as expiry 
approaches, and confirms pickup — no human dispatcher needed.

## Tech Stack

| Layer | Tool |
|---|---|
| Voice AI | Vapi — inbound/outbound calls, bilingual management, tool dispatch |
| Backend | FastAPI — webhook server routing Vapi tool calls |
| Database | Supabase (Postgres) — donors, food banks, listings, claims |
| AI Core | Claude API — NLU, conversation routing, org verification |
| Deployment | Railway — auto-deploys from GitHub on every push |
| Verification | ProPublica Nonprofit API (EIN) + Nominatim/OSM (geocoding) |

## Hard Parts

Getting Vapi's webhook integration right was the core technical challenge — 
the dispatcher has to respond within ~5 seconds with exactly the right 
payload format or Vapi rejects it. Parallel async checks for organization 
verification (EIN, geocode, web presence) had to be carefully time-bounded 
to stay within that window.

## Full Writeup

Devpost: https://devpost.com/software/mesa-3bxth0  
Original repo: https://github.com/adamkai7/hackdavis26
