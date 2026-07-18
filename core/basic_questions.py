"""Legacy simple-question helpers.

Keep the classifiers only; do not emit user-facing canned answers.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from core.api_client import ensure_external_api_allowed


WEATHER_RE = re.compile(r"\b(weather|wether|wather|forecast|temperature|temp)\b", re.I)
_LOCATION_STOP_RE = re.compile(
    r"\b(today|tonight|tomorrow|now|right\s+now|currently|please|pls|the)\b",
    re.I,
)


def is_weather_question(text: str | None) -> bool:
    return bool(WEATHER_RE.search(text or ""))


def is_basic_current_question(text: str | None) -> bool:
    return False


def _clean_location(value: str) -> str:
    text = re.sub(r"[?!.]+$", "", str(value or "")).strip()
    text = _LOCATION_STOP_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,-")
    return text


def extract_weather_location(text: str) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    patterns = [
        r"\b(?:weather|wether|wather|forecast|temperature|temp)\s+(?:in|for|at)?\s+(.+)$",
        r"\b(?:in|for|at)\s+([a-z][a-z .,'-]+?)(?:\s+(?:today|tonight|tomorrow|now|right now|currently))?\??$",
    ]
    for pattern in patterns:
        match = re.search(pattern, clean, re.I)
        if match:
            location = _clean_location(match.group(1))
            if location:
                return location
    return ""


def answer_weather_question(text: str, *, timeout: float = 6.0) -> str:
    location = extract_weather_location(text)
    if not location:
        return "What city should I check the weather for?"

    ensure_external_api_allowed("Weather lookup", source="basic_questions")
    encoded = urllib.parse.quote(location)
    req = urllib.request.Request(
        f"https://wttr.in/{encoded}?format=j1",
        headers={"User-Agent": "claude-stack-weather/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))

    current = (data.get("current_condition") or [{}])[0]
    area = (data.get("nearest_area") or [{}])[0]
    name = (((area.get("areaName") or [{}])[0] or {}).get("value") or location).strip()
    country = (((area.get("country") or [{}])[0] or {}).get("value") or "").strip()
    desc = (((current.get("weatherDesc") or [{}])[0] or {}).get("value") or "current conditions").strip()
    temp_f = str(current.get("temp_F") or "").strip()
    feels_f = str(current.get("FeelsLikeF") or "").strip()
    humidity = str(current.get("humidity") or "").strip()
    wind_mph = str(current.get("windspeedMiles") or "").strip()

    place = f"{name}, {country}" if country and country.lower() not in name.lower() else name
    parts = [f"{place}: {temp_f}F, {desc.lower()}"]
    if feels_f:
        parts.append(f"feels like {feels_f}F")
    if humidity:
        parts.append(f"humidity {humidity}%")
    if wind_mph:
        parts.append(f"wind {wind_mph} mph")
    return "; ".join(parts) + "."


async def answer_basic_question(text: str) -> str | None:
    return None
