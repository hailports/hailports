"""Travel search via Travelpayouts Data API — flights + hotels, $0 ongoing.

Exposes three tools the engine can pick when the user asks for travel:
    travel_flight_search    cheapest flight for a route within a date window
    travel_flight_calendar  cheapest price per day across a month
    travel_hotel_search     cheapest hotels in a city for given dates

Data API returns cached marketplace prices, not live booking rates — good
enough for "find me the cheapest, I'll book it myself on Kiwi/Booking."
Every result includes a deep-link the user can click to complete the booking
on the partner site.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

import httpx

from tools.base import BaseTool, make_tool_def
from core.api_client import ensure_external_api_allowed

log = logging.getLogger(__name__)

TP_BASE = "https://api.travelpayouts.com"
HOTELLOOK_BASE = "https://engine.hotellook.com/api/v2"

# Travelpayouts affiliate marker — click-throughs on the returned links
# attribute to this marker. Same key that appears in the dashboard URLs.
MARKER = os.environ.get("TRAVELPAYOUTS_MARKER", "")


def _key() -> str:
    return os.environ.get("TRAVELPAYOUTS_API_KEY", "")


def _travelpayouts_headers(key: str) -> dict[str, str]:
    return {"X-Access-Token": key} if key else {}


def _sanitize_provider_error(provider: str, status_code: int) -> str:
    return f"{provider} request failed with status {status_code}. Check provider credentials and service availability."


def _kiwi_deeplink(
    origin: str, dest: str, depart: str, return_: str | None = None,
    airline: str | None = None, cabin_class: str | None = None,
) -> str:
    """Build a Kiwi.com search URL that pre-fills origin/dest/dates + filters.

    airline: 2-letter IATA carrier code (e.g. 'DL' for Delta) — Kiwi narrows results.
    cabin_class: 'economy' | 'premium_economy' | 'business' | 'first'. When set to
        'economy' or higher, we also request regular-fare only (filters out basic
        economy 'hacker fares' that omit carry-on/seat selection).
    """
    base = f"https://www.kiwi.com/us/search/results/{origin.upper()}/{dest.upper()}/{depart}"
    if return_:
        base += f"/{return_}"
    params = []
    if MARKER:
        params.append(f"affilid={MARKER}")
    if airline:
        params.append(f"carriers={airline.upper()}")
    if cabin_class:
        cc = cabin_class.lower().replace(" ", "_")
        # Kiwi uses cabinClass=ECONOMY|PREMIUM_ECONOMY|BUSINESS|FIRST
        kc = {"economy": "ECONOMY", "main_cabin": "ECONOMY", "premium_economy": "PREMIUM_ECONOMY",
              "business": "BUSINESS", "first": "FIRST"}.get(cc, "ECONOMY")
        params.append(f"cabinClass={kc}")
        # Exclude hacker/basic-economy fares — Kiwi flag for regular fares with luggage
        params.append("hand_luggage=1")
    if params:
        base += "?" + "&".join(params)
    return base


def _booking_deeplink(
    city: str, checkin: str, checkout: str,
    brand: str | None = None, near: str | None = None,
) -> str:
    """Build a Booking.com search URL. Optional brand (Hilton, Marriott, Hyatt) and
    near (neighborhood/street) are concatenated into the ss= search term so Booking
    surfaces matching properties at the top."""
    import urllib.parse as _up
    search_parts = []
    if brand:
        search_parts.append(brand)
    search_parts.append(city)
    if near:
        search_parts.append(near)
    ss = _up.quote_plus(" ".join(search_parts))
    url = f"https://www.booking.com/searchresults.html?ss={ss}&checkin={checkin}&checkout={checkout}"
    if MARKER:
        url += f"&aid={MARKER}"
    return url


class TravelTool(BaseTool):
    name = "travel"
    description = "Flight and hotel price search via Travelpayouts."

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "travel_flight_search",
                "Find the cheapest flight between two airports/cities within a date window. "
                "Returns the lowest price found plus a booking deep-link the user clicks to complete the reservation. "
                "Supports filtering by airline (Delta, United, etc.) and cabin class (economy/premium/business/first). "
                "Set cabin_class='economy' or 'main_cabin' to exclude basic-economy 'hacker' fares from the Kiwi search.",
                {
                    "origin":    {"type": "string", "description": "IATA code (e.g. 'LAX', 'DEN', 'JFK')"},
                    "destination": {"type": "string", "description": "IATA code"},
                    "depart_date": {"type": "string", "description": "YYYY-MM-DD departure"},
                    "return_date": {"type": "string", "description": "YYYY-MM-DD return (optional for one-way)"},
                    "direct_only": {"type": "boolean", "description": "Restrict to direct flights (default false)"},
                    "airline":     {"type": "string", "description": "2-letter IATA carrier code, e.g. 'DL' (Delta), 'UA' (United), 'AA' (American), 'WN' (Southwest), 'AS' (Alaska), 'F9' (Frontier), 'B6' (JetBlue). Narrows results to one airline. Leave empty to search all."},
                    "cabin_class": {"type": "string", "description": "Cabin class: 'economy' / 'main_cabin' / 'premium_economy' / 'business' / 'first'. Defaults to any. Set 'economy' to exclude basic-economy hacker fares."},
                },
                ["origin", "destination", "depart_date"],
            ),
            make_tool_def(
                "travel_flight_calendar",
                "Get the cheapest price for a route on every day of a given month — useful for 'what's the cheapest day to fly next month'.",
                {
                    "origin":      {"type": "string"},
                    "destination": {"type": "string"},
                    "month":       {"type": "string", "description": "YYYY-MM (e.g. '2026-05')"},
                },
                ["origin", "destination", "month"],
            ),
            make_tool_def(
                "travel_hotel_search",
                "Find hotels in a city for given check-in/out dates. Returns a Booking.com deep-link pre-filled with dates. "
                "Supports filtering by hotel brand (Hilton, Marriott, Hyatt, IHG, Four Seasons, etc.) and a neighborhood/street hint "
                "(e.g. 'Old Market', 'Chicago Street', 'downtown') so Booking surfaces matching properties at the top.",
                {
                    "city":        {"type": "string", "description": "City name (e.g. 'Denver', 'Omaha', 'New York')"},
                    "checkin":     {"type": "string", "description": "YYYY-MM-DD"},
                    "checkout":    {"type": "string", "description": "YYYY-MM-DD"},
                    "adults":      {"type": "integer", "description": "Number of adults (default 1)"},
                    "brand":       {"type": "string", "description": "Hotel brand name to prioritize, e.g. 'Hilton', 'Marriott', 'Hyatt'. Optional."},
                    "near":        {"type": "string", "description": "Neighborhood, street, or landmark (e.g. 'Chicago Street', 'Old Market', 'near airport'). Optional."},
                },
                ["city", "checkin", "checkout"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        key = _key()
        if not key:
            return "ERROR: TRAVELPAYOUTS_API_KEY not set in .env"

        if tool_name == "travel_flight_search":
            return await self._flight_search(tool_input, key)
        if tool_name == "travel_flight_calendar":
            return await self._flight_calendar(tool_input, key)
        if tool_name == "travel_hotel_search":
            return await self._hotel_search(tool_input)
        return f"Unknown tool: {tool_name}"

    # --- Flights -----------------------------------------------------------
    async def _flight_search(self, inp: dict, key: str) -> str:
        ensure_external_api_allowed("Travel API")
        origin  = inp["origin"].upper()
        dest    = inp["destination"].upper()
        depart  = inp["depart_date"]
        ret     = inp.get("return_date") or ""
        direct  = "true" if inp.get("direct_only") else "false"
        airline_filter = (inp.get("airline") or "").upper().strip() or None
        cabin_class    = (inp.get("cabin_class") or "").strip() or None

        url = f"{TP_BASE}/v1/prices/cheap"
        params = {
            "origin": origin, "destination": dest,
            "depart_date": depart[:7], "return_date": ret[:7] if ret else "",
            "direct": direct, "currency": "USD",
        }
        if airline_filter:
            params["airline"] = airline_filter
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params=params, headers=_travelpayouts_headers(key))

        if r.status_code != 200:
            return _sanitize_provider_error("Travelpayouts", r.status_code)

        data = r.json().get("data", {}).get(dest, {})
        kiwi_url = _kiwi_deeplink(origin, dest, depart, ret or None,
                                  airline=airline_filter, cabin_class=cabin_class)
        if not data:
            return (f"No cached price for {origin}→{dest} in {depart[:7]}"
                    + (f" on {airline_filter}" if airline_filter else "")
                    + f". Try a wider window or click: {kiwi_url}")

        # Pick the cheapest offer
        options = sorted(data.values(), key=lambda o: o.get("price", 9e9))
        top = options[:3]
        header_bits = [origin + "→" + dest, depart]
        if ret:
            header_bits.append("→ " + ret)
        if airline_filter:
            header_bits.append(airline_filter + " only")
        if cabin_class:
            header_bits.append(cabin_class)
        lines = ["Cheapest " + " · ".join(header_bits) + ":"]
        for i, opt in enumerate(top, 1):
            price     = opt.get("price", "?")
            airline   = opt.get("airline", "")
            stops     = opt.get("number_of_changes", "?")
            depart_at = opt.get("departure_at", "")[:16].replace("T", " ")
            lines.append(f"  {i}. ${price}  {airline}  ·  {stops} stop(s)  ·  dep {depart_at}")
        lines.append("")
        lines.append(f"Book: {kiwi_url}")
        if cabin_class and cabin_class.lower() in ("economy", "main_cabin"):
            lines.append("(Kiwi filter set to main-cabin regular fares — basic economy excluded)")
        return "\n".join(lines)

    async def _flight_calendar(self, inp: dict, key: str) -> str:
        ensure_external_api_allowed("Travel API")
        origin = inp["origin"].upper()
        dest   = inp["destination"].upper()
        month  = inp["month"]  # YYYY-MM

        url = f"{TP_BASE}/v1/prices/calendar"
        params = {
            "origin": origin, "destination": dest,
            "depart_date": month, "currency": "USD",
        }
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params=params, headers=_travelpayouts_headers(key))

        if r.status_code != 200:
            return _sanitize_provider_error("Travelpayouts", r.status_code)

        data = r.json().get("data", {})
        if not data:
            return f"No calendar data for {origin}→{dest} in {month}."

        by_day = sorted(data.items(), key=lambda kv: kv[1].get("price", 9e9))
        cheapest = by_day[:7]
        lines = [f"Cheapest days to fly {origin}→{dest} in {month}:"]
        for day, info in cheapest:
            lines.append(f"  {day}: ${info.get('price', '?')}  ({info.get('airline', '')}, {info.get('number_of_changes', '?')} stop)")
        cheapest_day = by_day[0][0]
        lines.append("")
        lines.append(f"Cheapest day: {cheapest_day} — book: {_kiwi_deeplink(origin, dest, cheapest_day)}")
        return "\n".join(lines)

    # --- Hotels ------------------------------------------------------------
    async def _hotel_search(self, inp: dict) -> str:
        """Return a Booking.com deep-link with pre-filled dates, guest count, brand, and location.

        Hotellook's public cached-price endpoints were deprecated by
        Travelpayouts — they now require auth/marker that only paid partners
        get. For our "I find, you book" flow this is fine: the deep-link
        lands the user on Booking.com with everything pre-filled and real
        live prices visible without needing a key on our side.
        """
        city     = inp["city"]
        checkin  = inp["checkin"]
        checkout = inp["checkout"]
        adults   = int(inp.get("adults", 1))
        brand    = (inp.get("brand") or "").strip() or None
        near     = (inp.get("near") or "").strip() or None
        url = _booking_deeplink(city, checkin, checkout, brand=brand, near=near)
        if adults != 1:
            url += f"&group_adults={adults}"
        nights = (datetime.fromisoformat(checkout) - datetime.fromisoformat(checkin)).days
        header_bits = []
        if brand: header_bits.append(brand)
        header_bits.append(city)
        if near: header_bits.append("near " + near)
        header = " · ".join(header_bits)
        return (
            f"{header} · {checkin} → {checkout} · {nights} night(s) · {adults} adult(s)\n"
            f"Live prices + book: {url}"
        )
