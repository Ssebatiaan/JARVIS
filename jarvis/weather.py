"""Clima de Open-Meteo (gratuito, sin cuenta). Solo se envía el nombre de la ciudad."""

import json
import urllib.parse
import urllib.request
from collections.abc import Callable

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search?"
FORECAST = "https://api.open-meteo.com/v1/forecast?"
CODES = {
    0: "cielo despejado", 1: "cielo casi despejado", 2: "cielo parcialmente nublado", 3: "cielo nublado",
    45: "niebla", 48: "niebla con escarcha", 51: "llovizna ligera", 53: "llovizna", 55: "llovizna intensa",
    56: "llovizna helada", 57: "llovizna helada", 61: "lluvia ligera", 63: "lluvia", 65: "lluvia fuerte",
    66: "lluvia helada", 67: "lluvia helada", 71: "nieve ligera", 73: "nieve", 75: "nieve fuerte",
    77: "nieve granulada", 80: "chubascos ligeros", 81: "chubascos", 82: "chubascos fuertes",
    85: "chubascos de nieve", 86: "chubascos de nieve", 95: "tormenta", 96: "tormenta con granizo",
    99: "tormenta con granizo",
}


class CityNotFound(LookupError):
    """El servicio no conoce esa ciudad."""


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=6) as response:
        return json.loads(response.read(200_000))


def report(city: str, day: str = "hoy", *, get: Callable[[str], dict] = _get) -> str:
    """Frase con el clima. OSError si no hay conexión o la respuesta no se entiende."""
    try:
        found = get(GEOCODE + urllib.parse.urlencode({"name": city, "count": 1, "language": "es"})).get("results")
        if not found:
            raise CityNotFound(city)
        place = found[0]
        data = get(FORECAST + urllib.parse.urlencode({
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 2,
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        }))
        daily, index = data["daily"], 1 if day == "manana" else 0
        low, high = round(daily["temperature_2m_min"][index]), round(daily["temperature_2m_max"][index])
        chance = daily["precipitation_probability_max"][index]
        rain = f" y {chance} % de probabilidad de lluvia" if chance is not None else ""
        where = ", ".join(part for part in (place["name"], place.get("country")) if part)
        if day == "manana":
            sky = CODES.get(daily["weather_code"][1], "tiempo variable")
            return f"Mañana en {where}: {sky}, entre {low} y {high} grados{rain}."
        now = data["current"]
        sky = CODES.get(now["weather_code"], "tiempo variable")
        return (f"En {where} hay {sky} y {round(now['temperature_2m'])} grados, con sensación de "
                f"{round(now['apparent_temperature'])}. Hoy, entre {low} y {high} grados{rain}.")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise OSError("respuesta del clima no válida") from error
