"""Weather — fetch forecasts from yr.no (MET Norway, no API key needed).

Minimal example gist showing the pattern: a class with async methods,
auto-discovered by the kernel, hot-reloaded on save.
"""

import json
import urllib.request

__repld_usage__ = "from weather import Weather; w = Weather(); w.now('Oslo')"


class Weather:
    """Simple weather lookups via yr.no's public API."""

    _BASE = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
    _GEOCODE = "https://nominatim.openstreetmap.org/search"
    _HEADERS = {"User-Agent": "repld-example/1.0 github.com/angelsen/repld"}

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=self._HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _geocode(self, place: str) -> tuple[float, float]:
        url = f"{self._GEOCODE}?q={place}&format=json&limit=1"
        data = self._get(url)
        if not data:
            raise ValueError(f"Could not geocode {place!r}")
        return float(data[0]["lat"]), float(data[0]["lon"])

    def now(self, place: str) -> dict:
        """Current weather for a place name."""
        lat, lon = self._geocode(place)
        url = f"{self._BASE}?lat={lat:.4f}&lon={lon:.4f}"
        fc = self._get(url)
        ts = fc["properties"]["timeseries"][0]
        d = ts["data"]["instant"]["details"]
        return {
            "place": place,
            "time": ts["time"],
            "temp_c": d.get("air_temperature"),
            "wind_m_s": d.get("wind_speed"),
            "humidity_%": d.get("relative_humidity"),
        }

    def forecast(self, place: str, hours: int = 12) -> list[dict]:
        """Hourly forecast for the next N hours."""
        lat, lon = self._geocode(place)
        url = f"{self._BASE}?lat={lat:.4f}&lon={lon:.4f}"
        fc = self._get(url)
        rows = []
        for ts in fc["properties"]["timeseries"][:hours]:
            d = ts["data"]["instant"]["details"]
            rows.append(
                {
                    "time": ts["time"],
                    "temp_c": d.get("air_temperature"),
                    "wind_m_s": d.get("wind_speed"),
                }
            )
        return rows
