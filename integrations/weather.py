"""
Weather + air quality integration — Open-Meteo.

Why Open-Meteo:
- Free, no API key, no signup
- One endpoint for forecast (temp, humidity, wind, UV, precip, cloud cover)
- Separate endpoint for air quality (US AQI, PM2.5, PM10, ozone)
- Handles unit conversion server-side (fahrenheit, mph)
- Timezone-aware (returns local times for the given timezone)

Caching:
Both endpoints update ~hourly. A 30-minute in-memory TTL cache cuts the
API load on the morning brief + chat path dramatically without making
the forecast stale. Process restart clears the cache — fine.

Shape of the summary:
summarize_today() returns a multi-line string built for the coach's
layered context. It includes: current conditions, daily high/low, UV
peak, AQI snapshot, and a sparse hourly strip (6am / 9am / noon / 3pm /
6pm / 9pm) so the coach can pick a run time.
"""

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Open-Meteo WMO weather codes → short readable label.
# Not exhaustive; the common ones are covered.
_WEATHER_CODE_LABELS = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "heavy showers",
    82: "violent showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm w/ hail",
    99: "severe thunderstorm",
}

# US AQI bucket labels (standard EPA categories).
def _aqi_category(aqi: Optional[float]) -> str:
    if aqi is None:
        return "n/a"
    if aqi <= 50:
        return "good"
    if aqi <= 100:
        return "moderate"
    if aqi <= 150:
        return "unhealthy for sensitive"
    if aqi <= 200:
        return "unhealthy"
    if aqi <= 300:
        return "very unhealthy"
    return "hazardous"


# UV index bucket labels (WHO).
def _uv_category(uv: Optional[float]) -> str:
    if uv is None:
        return "n/a"
    if uv < 3:
        return "low"
    if uv < 6:
        return "moderate"
    if uv < 8:
        return "high"
    if uv < 11:
        return "very high"
    return "extreme"


class WeatherClient:
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

    def __init__(self, config):
        self.lat = config.HOME_LAT
        self.lng = config.HOME_LNG
        self.city = config.HOME_CITY
        self.tz = config.TIMEZONE
        # TTL cache: key → (timestamp, payload). 30 min.
        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_ttl_s = 30 * 60

    def _cached(self, key: str) -> Optional[dict]:
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, payload = entry
        if time.time() - ts > self._cache_ttl_s:
            return None
        return payload

    def _store(self, key: str, payload: dict):
        self._cache[key] = (time.time(), payload)

    async def _fetch_forecast(self) -> dict:
        cached = self._cached("forecast")
        if cached is not None:
            return cached
        params = {
            "latitude": self.lat,
            "longitude": self.lng,
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "wind_speed_10m,wind_gusts_10m,uv_index,is_day,precipitation,"
                "weather_code,cloud_cover"
            ),
            "hourly": (
                "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "precipitation_probability,wind_speed_10m,uv_index,cloud_cover,"
                "weather_code"
            ),
            "daily": (
                "temperature_2m_max,temperature_2m_min,sunrise,sunset,"
                "uv_index_max,precipitation_probability_max,wind_speed_10m_max,"
                "weather_code"
            ),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": self.tz,
            "forecast_days": 1,
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(self.FORECAST_URL, params=params)
            r.raise_for_status()
            data = r.json()
        self._store("forecast", data)
        return data

    async def _fetch_air(self) -> dict:
        cached = self._cached("air")
        if cached is not None:
            return cached
        params = {
            "latitude": self.lat,
            "longitude": self.lng,
            "current": "us_aqi,pm2_5,pm10,ozone,nitrogen_dioxide",
            "hourly": "us_aqi,pm2_5,pm10,ozone",
            "timezone": self.tz,
            "forecast_days": 1,
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(self.AIR_URL, params=params)
            r.raise_for_status()
            data = r.json()
        self._store("air", data)
        return data

    # ── Public summarizer ───────────────────────────────────────────────────

    def _is_configured(self) -> bool:
        """True iff a real home location is set. (0.0, 0.0) is the null-island
        default — treat that as 'unset' and skip weather entirely."""
        return not (self.lat == 0.0 and self.lng == 0.0) and bool(self.city)

    async def summarize_today(self) -> str:
        """Return a multi-line weather block for the coach's context.

        Never raises — on failure, returns an empty string so the rest of
        the brief still goes out. Also returns empty string when the home
        location isn't configured.
        """
        if not self._is_configured():
            return ""
        try:
            fc = await self._fetch_forecast()
            air = await self._fetch_air()
        except Exception as e:
            logger.warning(f"Weather fetch failed (non-fatal): {e}")
            return ""

        lines: list[str] = []
        lines.append(f"WEATHER ({self.city}):")

        cur = fc.get("current") or {}
        cond = _WEATHER_CODE_LABELS.get(cur.get("weather_code"), "?")
        cur_aqi = (air.get("current") or {}).get("us_aqi")
        cur_pm25 = (air.get("current") or {}).get("pm2_5")
        lines.append(
            f"  Now: {cur.get('temperature_2m')}°F (feels {cur.get('apparent_temperature')}°F) "
            f"| {cond} | humidity {cur.get('relative_humidity_2m')}% "
            f"| wind {cur.get('wind_speed_10m')}mph"
            + (f" gust {cur['wind_gusts_10m']}mph" if cur.get("wind_gusts_10m") else "")
            + f" | UV {cur.get('uv_index')} ({_uv_category(cur.get('uv_index'))})"
            + f" | AQI {cur_aqi} ({_aqi_category(cur_aqi)}), PM2.5 {cur_pm25}"
        )

        daily = fc.get("daily") or {}
        sunrise = ""
        sunset = ""
        if daily.get("time"):
            hi = daily["temperature_2m_max"][0]
            lo = daily["temperature_2m_min"][0]
            uv_max = daily["uv_index_max"][0]
            precip_max = daily["precipitation_probability_max"][0]
            wind_max = daily["wind_speed_10m_max"][0]
            sunrise = (daily["sunrise"][0] or "")[-5:]
            sunset = (daily["sunset"][0] or "")[-5:]
            day_cond = _WEATHER_CODE_LABELS.get(daily["weather_code"][0], "?")
            lines.append(
                f"  Today: high {hi}°F / low {lo}°F | {day_cond} "
                f"| UV peaks {uv_max} ({_uv_category(uv_max)}) "
                f"| max precip chance {precip_max}% "
                f"| max wind {wind_max}mph "
                f"| sunrise {sunrise} / sunset {sunset}"
            )

        # ── SUN block ────────────────────────────────────────────────────
        # Dylan loves the sun — surface the peak-UV hour, the high-UV
        # "strong-sun" window (UV ≥ 6), and meaningful-sun bookends
        # (UV ≥ 3) as peak-seeking info, not avoidance. Also emit a
        # compact hourly UV curve so the coach can pick a precise window.
        hourly = fc.get("hourly") or {}
        times = hourly.get("time") or []
        uvs = hourly.get("uv_index") or []
        hour_uv: list[tuple[int, float]] = []
        for t, uv in zip(times, uvs):
            if uv is None:
                continue
            try:
                hh = int(t.split("T")[1][:2])
            except Exception:
                continue
            hour_uv.append((hh, uv))

        if hour_uv:
            sun_parts: list[str] = []
            peak_hh, peak_uv = max(hour_uv, key=lambda x: x[1])
            sun_parts.append(
                f"peak UV {peak_uv} ({_uv_category(peak_uv)}) at {peak_hh:02d}:00"
            )
            high_hours = [hh for hh, uv in hour_uv if uv >= 6]
            if high_hours:
                sun_parts.append(
                    f"strong-sun window {min(high_hours):02d}:00–{max(high_hours)+1:02d}:00 (UV ≥ 6)"
                )
            else:
                sun_parts.append("no UV ≥ 6 hours today (weaker-sun day)")
            any_hours = [hh for hh, uv in hour_uv if uv >= 3]
            if any_hours:
                sun_parts.append(
                    f"UV ≥ 3 from {min(any_hours):02d}:00 to {max(any_hours)+1:02d}:00"
                )
            lines.append(f"  Sun: {' | '.join(sun_parts)}")
            curve = " ".join(
                f"{hh:02d}={uv}"
                for hh, uv in sorted(hour_uv)
                if 6 <= hh <= 20
            )
            if curve:
                lines.append(f"  UV curve (06–20): {curve}")
            if sunrise and sunset:
                lines.append(f"  Daylight: sunrise {sunrise} → sunset {sunset}")

        # Sparse hourly strip at training-relevant hours.
        target_hours = [6, 9, 12, 15, 18, 21]
        strip = []
        for i, t in enumerate(times):
            try:
                hh = int(t.split("T")[1][:2])
            except Exception:
                continue
            if hh in target_hours:
                temp = hourly["temperature_2m"][i]
                feels = hourly["apparent_temperature"][i]
                hum = hourly["relative_humidity_2m"][i]
                precip = hourly["precipitation_probability"][i]
                uv = hourly["uv_index"][i]
                wind = hourly["wind_speed_10m"][i]
                strip.append(
                    f"{hh:02d}:00 {temp}°F/f{feels} {hum}%H {wind}mph "
                    f"UV{uv} rain{precip}%"
                )
        if strip:
            lines.append("  Hourly: " + " | ".join(strip))

        # AQI intraday peak
        air_h = air.get("hourly") or {}
        air_times = air_h.get("time") or []
        air_aqi = air_h.get("us_aqi") or []
        if air_aqi:
            # Find next 12h window from "now"-ish (first hourly entry) max
            upcoming = [v for v in air_aqi if v is not None]
            if upcoming:
                lines.append(
                    f"  AQI today: min {min(upcoming)} / max {max(upcoming)} "
                    f"({_aqi_category(max(upcoming))})"
                )

        return "\n".join(lines)
