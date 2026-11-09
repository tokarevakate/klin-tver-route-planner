"""Маршруты между городами из локального справочника через OSRM."""

import csv
from dataclasses import dataclass
from functools import lru_cache
import json
from math import isfinite
import os
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

OSRM_URL = "https://routing.openstreetmap.de/routed-car"
OSRM_LOCK = Lock()
last_request = float("-inf")


class RoutingUnavailable(Exception):
    """Внешний сервис недоступен или вернул неожиданный ответ."""


@dataclass(frozen=True)
class City:
    name: str
    region: str
    lon: float
    lat: float

    @property
    def label(self) -> str:
        if not self.region or normalize(self.name) == normalize(self.region):
            return self.name
        return f"{self.name} — {self.region}"

    @property
    def coordinates(self) -> str:
        return f"{self.lon:.7f},{self.lat:.7f}"


@lru_cache(maxsize=1)
def cities() -> tuple[City, ...]:
    path = Path(__file__).with_name("russian_cities.csv")
    with path.open(encoding="utf-8", newline="") as file:
        return tuple(City(row["name"], row["region"], float(row["lon"]), float(row["lat"]))
                     for row in csv.DictReader(file))


def normalize(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").split())


def find_city(value: str) -> City:
    if not value.strip() or len(value) > 200:
        raise ValueError("Введите название города из справочника.")
    name = normalize(value)
    matches = [city for city in cities()
               if name in (normalize(city.name), normalize(city.label))]
    if not matches:
        raise ValueError(f"«{value}» отсутствует в справочнике городов России.")
    if len(matches) > 1:
        labels = ", ".join(city.label for city in matches)
        raise ValueError(f"Уточните город «{value}». Варианты: {labels}.")
    return matches[0]


@lru_cache(maxsize=64)
def routes_for(base_url: str, start: City, finish: City) -> tuple[dict, ...]:
    global last_request
    params = urlencode({"alternatives": "true", "overview": "simplified", "geometries": "geojson"})
    path = f"/route/v1/driving/{start.coordinates};{finish.coordinates}?{params}"
    request = Request(base_url.rstrip("/") + path, headers={
        "User-Agent": "KlinTverRoutePlanner/2.0 (+https://github.com/tokarevakate/klin-tver-route-planner)",
        "Accept": "application/json",
    })
    try:
        with OSRM_LOCK:
            sleep(max(0, 1 - (monotonic() - last_request)))
            last_request = monotonic()
            try:
                response = urlopen(request, timeout=12)
            except HTTPError as exc:
                if exc.code != 400:
                    exc.close()
                    raise
                response = exc
            with response:
                payload = json.loads(response.read(4_000_000))
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RoutingUnavailable("Сервис маршрутов временно недоступен. Попробуйте позже.") from exc

    if not isinstance(payload, dict):
        raise RoutingUnavailable("Сервис маршрутов вернул неожиданный ответ.")
    if payload.get("code") in {"NoRoute", "NoSegment"}:
        raise ValueError("Сервис не нашёл автомобильный маршрут между этими городами.")
    if payload.get("code") != "Ok":
        raise RoutingUnavailable("Сервис маршрутов вернул неожиданный ответ.")
    try:
        routes = tuple(
            {"distance": float(route["distance"]), "duration": float(route["duration"]),
             "geometry": route["geometry"]["coordinates"]}
            for route in payload["routes"]
        )
        if not routes or not all(
            isfinite(route["distance"]) and route["distance"] > 0
            and isfinite(route["duration"]) and route["duration"] > 0
            and len(route["geometry"]) >= 2
            and all(len(point) == 2 and all(isfinite(coord) for coord in point)
                    for point in route["geometry"])
            for route in routes
        ):
            raise ValueError("invalid route")
        return routes
    except (KeyError, TypeError, ValueError) as exc:
        raise RoutingUnavailable("Сервис маршрутов вернул неполные данные.") from exc


def city_plan(origin: str, destination: str, objective: str, consumption) -> dict:
    if objective not in {"duration", "distance", "fuel"}:
        raise ValueError("Выберите критерий оптимизации.")
    start, finish = find_city(origin), find_city(destination)
    if start == finish:
        raise ValueError("Выберите два разных города.")
    base_url = os.environ.get("ROUTE_PLANNER_OSRM_URL", OSRM_URL)
    options = []
    for route in routes_for(base_url, start, finish):
        km = route["distance"] / 1000
        minutes = route["duration"] / 60
        speed = min(130, max(10, 60 * km / minutes))
        options.append({"distance": km, "duration": minutes,
                        "fuel": consumption(speed, 0, None) * km / 100,
                        "geometry": route["geometry"]})
    options.sort(key=lambda route: (route[objective], route["duration"], route["distance"]))
    return {"origin": start.label, "destination": finish.label, "routes": options}
