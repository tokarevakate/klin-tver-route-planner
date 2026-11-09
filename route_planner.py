"""Учебная оптимизация маршрута и времени выезда из Клина в Тверь."""

import argparse
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from math import ceil, isfinite
from pathlib import Path
import re
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from city_routing import OSRM_URL, RoutingUnavailable, cities, city_plan, discover_routes, find_city

MINUTES_PER_DAY = 1440
IDLE_LITRES_PER_HOUR = 0.6
EPSILON = 1e-8
OBJECTIVES = {
    "cost": "прямые расходы",
    "distance": "расстояние",
    "fuel": "расход топлива",
    "duration": "время в пути",
    "score": "расходы с оценкой времени",
}


@dataclass(frozen=True)
class Segment:
    name: str
    km: float
    grade: float
    radius: int | None
    # Пары (км/ч, мин задержки на км) вне пика, утром и вечером.
    rates: tuple[tuple[float, float], ...]
    paid: bool = False
    toll_rubles: int | None = None


@dataclass(frozen=True)
class Route:
    name: str
    toll: float | None
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class Trip:
    route: Route
    departure: int
    arrival: float
    km: float
    fuel: float
    fuel_cost: float
    score: float | None

    @property
    def duration(self) -> float:
        return self.arrival - self.departure

    @property
    def cost(self) -> float | None:
        return None if self.route.toll is None else self.fuel_cost + self.route.toll


FREE_WEST = Segment("М-10: Клин — Завидово", 31, 0.45, 850,
                    ((70, 0.07), (55, 0.24), (60, 0.18)))
FREE_EAST = Segment("М-10: Завидово — Тверь-Юг", 35, 0.45, 900,
                    ((72, 0.08), (42, 0.47), (55, 0.25)))
KLIN_ACCESS = Segment("А-108: Клин — въезд на М-11", 9, 0.35, 700,
                      ((55, 0.07), (44, 0.20), (46, 0.17)))
ZAVIDOVO_ACCESS = Segment("Подъезд к М-11 у Завидова", 4, 0.30, 550,
                          ((50, 0.04), (45, 0.09), (45, 0.09)))
PAID_WEST = Segment("М-11: ПВП 89 — 124", 34, 0.20, 2000,
                    ((90, 0.01), (88, 0.03), (88, 0.03)), paid=True)
PAID_EAST = Segment("М-11: 124 — 149 км", 25, 0.18, 2000,
                    ((90, 0.01), (88, 0.03), (88, 0.03)), paid=True)
TVER_ACCESS = Segment("Тверь-Юг — центр Твери", 15, 0.25, 700,
                      ((48, 0.14), (34, 0.42), (38, 0.31)))

ROUTES = (
    Route("М-10", 0, (FREE_WEST, FREE_EAST, TVER_ACCESS)),
    Route("М-10 → М-11", 140, (FREE_WEST, ZAVIDOVO_ACCESS, PAID_EAST, TVER_ACCESS)),
    Route("М-11", 370, (KLIN_ACCESS, PAID_WEST, PAID_EAST, TVER_ACCESS)),
)


# Учебные расстояния, скорости, уклоны и задержки. Плата за указанные
# въезды и съезды берётся из опубликованных таблиц категории I.
MOSCOW_ACCESS = Segment("Москва · подъезд к М-11", 25, 0.15, 800,
                        ((55, 0.1), (40, 0.25), (37, 0.30)))
MOSCOW_M10 = Segment("М-10 · Москва — Солнечногорск", 68, 0.25, 900,
                     ((65, 0.12), (48, 0.27), (46, 0.32)))
NORTH_M10 = Segment("М-10 · Солнечногорск — Санкт-Петербург через Тверь и Великий Новгород",
                    634, 0.25, 1200, ((75, 0.07), (60, 0.14), (59, 0.16)))
MOSCOW_M11 = Segment("М-11 · км 15–58", 43, 0.12, 2300,
                     ((95, 0.015), (86, 0.03), (85, 0.04)), paid=True)
NORTH_M11 = Segment("М-11 · км 58–684", 626, 0.15, 2500,
                    ((98, 0.005), (96, 0.01), (94, 0.01)), paid=True)
SPB_ACCESS = Segment("Санкт-Петербург · КАД и городские дороги", 23, 0.12, 1000,
                     ((55, 0.09), (43, 0.21), (40, 0.27)))

KAZAN_FREE = Segment("М-7 · Казань — Владимир через Чебоксары и Нижний Новгород",
                     630, 0.4, 1000, ((77, 0.08), (65, 0.15), (63, 0.19)))
VLADIMIR_FREE = Segment("М-7 · Владимир — Москва", 190, 0.3, 850,
                        ((70, 0.12), (55, 0.28), (52, 0.32)))
VLADIMIR_LINK = Segment("Владимир · подъезд к М-12", 12, 0.3, 750,
                        ((60, 0.08), (48, 0.19), (48, 0.19)))
KAZAN_LINK = Segment("Казань · подъезд к М-12 по Р-239", 29, 0.25, 850,
                     ((65, 0.08), (50, 0.19), (48, 0.23)))
KAZAN_PAID = Segment("М-12 · Казань (Р-239) — Владимир", 610, 0.2, 2500,
                     ((100, 0.01), (97, 0.02), (94, 0.025)), paid=True)
VLADIMIR_PAID = Segment("М-12 · Владимир — Москва", 170, 0.18, 2500,
                        ((100, 0.01), (95, 0.03), (92, 0.04)), paid=True)

NORTH_ROUTES = (
    Route("М-10 · бесплатный", 0, (MOSCOW_M10, NORTH_M10)),
    Route("М-10 + М-11 · въезд у Солнечногорска", 3956, (MOSCOW_M10, NORTH_M11, SPB_ACCESS)),
    Route("М-11 · платный", 4906, (MOSCOW_ACCESS, MOSCOW_M11, NORTH_M11, SPB_ACCESS)),
)
KAZAN_ROUTES = (
    Route("М-7 · бесплатный", 0, (KAZAN_FREE, VLADIMIR_FREE)),
    Route("М-7 + М-12 · через Владимир", 1737,
          (KAZAN_FREE, VLADIMIR_LINK, VLADIMIR_PAID, MOSCOW_ACCESS)),
    Route("М-12 · платный", 5847, (KAZAN_LINK, KAZAN_PAID, VLADIMIR_PAID, MOSCOW_ACCESS)),
)
# М-4 до северного въезда в Воронеж. Плата за ПВП 62 — отдельная
# развязка внутри км 21–93, а ПВП 515 находится за выбранным съездом.
M4_TARIFF = "https://avtodor-tr.ru/road/tariffs/#m4"
M4_PAID_RATES = ((98, 0.025), (94, 0.04), (93, 0.045))
M4_BYPASS_RATES = ((72, 0.08), (61, 0.16), (58, 0.20))
M4_SECTIONS = (
    (21, 93, 310, 77, "Каширское шоссе"),
    (93, 211, 400, 131, "Р-22, Р-114 и Узловая"),
    (211, 266, 260, 61, "Крутой Верх — Богородицк — Кузовка"),
    (266, 322, 260, 63, "Чернятино — Покровка — Пушкари"),
    (322, 401, 370, 87, "Бабарыкино, Яркино и Елец"),
    (401, 464, 230, 70, "Старая М-4 через Задонск и Хлевное"),
)
M4_APPROACH = Segment("Москва · подъезд к М-4", 25, 0.15, 800,
                      ((55, 0.1), (40, 0.25), (37, 0.30)))
VORONEZH_ACCESS = Segment("М-4 · км 464–492 и северный въезд в Воронеж", 46, 0.25, 1000,
                          ((69, 0.06), (56, 0.14), (52, 0.20)))
M4_MAIN = tuple(
    Segment(f"М-4 · км {start}–{finish}", finish - start, 0.22, 2200, M4_PAID_RATES,
            paid=True, toll_rubles=toll)
    for start, finish, toll, _, _ in M4_SECTIONS
)
M4_BYPASSES = tuple(
    Segment(f"Объезд км {start}–{finish} · {name}", distance, 0.3, 850, M4_BYPASS_RATES)
    for start, finish, _, distance, name in M4_SECTIONS
)
VORONEZH_ROUTES = (
    Route("М-4 · бесплатные объезды", 0, (M4_APPROACH, *M4_BYPASSES, VORONEZH_ACCESS)),
    Route("М-4 · платно до Быковки, далее объезды",
          sum(segment.toll_rubles or 0 for segment in M4_MAIN[:2]),
          (M4_APPROACH, *M4_MAIN[:2], *M4_BYPASSES[2:], VORONEZH_ACCESS)),
    Route("М-4 «Дон» · платный основной ход",
          sum(segment.toll_rubles or 0 for segment in M4_MAIN),
          (M4_APPROACH, *M4_MAIN, VORONEZH_ACCESS)),
)
M11_TARIFF = "https://avtodor-tr.ru/upload/iblock/402/k44r7q8jiukdw84ropm7pgd9khjh5o4h.pdf"
M11_CONCESSION = "https://m11-neva.ru/upload/iblock/932/o3urum9w3ea9pvskhhrw7uh4b97klvkv.pdf"
M12_TARIFF = "https://avtodor-tr.ru/upload/iblock/fe5/a2e7y350b1wrbw28x181m10z1eeblnfd.pdf"
NORTH_SOURCES = (("Автодор · М-11 км 58–684", M11_TARIFF),
                 ("МСС · М-11 км 15–58", M11_CONCESSION))
CORRIDORS = {
    ("Москва", "Воронеж"): (VORONEZH_ROUTES, (("Автодор · М-4 и бесплатные объезды", M4_TARIFF),)),
    ("Москва", "Санкт-Петербург"): (NORTH_ROUTES, NORTH_SOURCES),
    ("Казань", "Москва"): (KAZAN_ROUTES, (("Автодор · М-12", M12_TARIFF),)),
    ("Клин", "Тверь"): (
        (ROUTES[0], replace(ROUTES[1], toll=150), replace(ROUTES[2], toll=410)),
        (("Автодор · М-11", M11_TARIFF),),
    ),
}
CITY_FUEL_PRICE = 65.0
CITY_PEAK_DELAY = 10.0


def consumption(speed: float, grade: float, radius: int | None) -> float:
    """Расход в л/100 км при нулевом ускорении по модели первой работы."""
    # FuelCoeffs.from_vehicle(VehicleParams()), vehicle-route.
    # Коэффициент спуска в исходной калибровке равен нулю.
    flat = 6.700070172932973 - 0.08112399995420332 * speed + 0.0008807510214262922 * speed**2
    climb = 1.8327313488603818 * max(grade, 0)
    turning = 0.002 * speed**2 / radius if radius is not None else 0
    return max(0.5, flat + climb + turning)


def traffic_at(time: float) -> tuple[int, float]:
    """Индекс режима и абсолютная минута его окончания."""
    day_start = (time // MINUTES_PER_DAY) * MINUTES_PER_DAY
    for end, regime in ((420, 0), (570, 1), (990, 0), (1170, 2), (MINUTES_PER_DAY, 0)):
        if time < day_start + end:
            return regime, day_start + end
    raise ValueError("Не удалось определить режим трафика")


def simulate(route: Route, departure: int, price: float, value_of_time: float) -> Trip:
    time = float(departure)
    fuel = 0.0
    for segment in route.segments:
        for grade in (segment.grade, -segment.grade):
            remaining = segment.km / 2
            while remaining > EPSILON:
                regime, until = traffic_at(time)
                speed, idle = segment.rates[regime]
                minutes_per_km = 60 / speed + idle
                available_km = (until - time) / minutes_per_km
                driven = min(remaining, available_km)
                time = until if available_km <= remaining else time + driven * minutes_per_km
                fuel += driven * (consumption(min(130, max(10, speed)), grade, segment.radius) / 100
                                  + IDLE_LITRES_PER_HOUR * idle / 60)
                remaining -= driven

    fuel_cost = fuel * price
    score = (None if route.toll is None
             else fuel_cost + route.toll + value_of_time * (time - departure) / 60)
    return Trip(route, departure, time, sum(s.km for s in route.segments), fuel, fuel_cost, score)


def ranking(trip: Trip, objective: str) -> tuple[float, ...]:
    metric = trip.km if objective == "distance" else getattr(trip, objective)
    cost = trip.cost
    return (float("inf") if metric is None else metric,
            float("inf") if cost is None else cost, trip.fuel, trip.arrival, trip.departure)


def parse_time(value: str) -> int:
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise argparse.ArgumentTypeError("укажите время в формате ЧЧ:ММ")
    hours, minutes = map(int, value.split(":"))
    return 60 * hours + minutes


def format_time(time: float) -> str:
    days, minute = divmod(ceil(time - EPSILON), MINUTES_PER_DAY)
    hours, minutes = divmod(minute, 60)
    suffix = f" (+{days} д.)" if days else ""
    return f"{hours:02}:{minutes:02}{suffix}"


def print_table(trips: list[Trip], deadline: int | None) -> None:
    headers = ("Маршрут", "Выезд", "Прибытие", "Мин", "Км", "Литры",
               "Топливо, ₽", "Проезд, ₽", "Всего, ₽", "Со временем, ₽")
    rows = [headers]
    for trip in trips:
        late = deadline is not None and trip.arrival > deadline + EPSILON
        rows.append((
            trip.route.name,
            format_time(trip.departure),
            format_time(trip.arrival) + (" *" if late else ""),
            f"{trip.duration:.1f}",
            str(trip.km),
            f"{trip.fuel:.2f}",
            f"{trip.fuel_cost:.2f}",
            str(trip.route.toll),
            f"{trip.cost:.2f}",
            f"{trip.score:.2f}",
        ))
    widths = [max(map(len, column)) for column in zip(*rows)]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def is_feasible(trip: Trip, deadline: int | None) -> bool:
    return deadline is None or trip.arrival <= deadline + EPSILON


def plan(start: int, end: int, deadline: int | None, price: float,
         value_of_time: float, objective: str,
         routes: tuple[Route, ...] = ROUTES) -> tuple[list[Trip], list[Trip], Trip | None]:
    if start > end:
        raise ValueError("Конец окна выезда должен быть не раньше начала.")
    if deadline is not None and deadline < start:
        raise ValueError("Срок прибытия должен быть не раньше начала окна выезда.")
    if not isfinite(price) or not 0 < price <= 1000:
        raise ValueError("Цена топлива должна быть больше 0 и не выше 1000 ₽/л.")
    if not isfinite(value_of_time) or not 0 <= value_of_time <= 10000:
        raise ValueError("Оценка времени должна быть от 0 до 10 000 ₽/ч.")
    if objective not in OBJECTIVES:
        raise ValueError("Выберите критерий оптимизации.")

    departures = [*range(start, end, 15), end]
    trips = [simulate(route, departure, price, value_of_time)
             for departure in departures for route in routes]
    ranked = sorted((trip for trip in trips if is_feasible(trip, deadline)),
                    key=lambda trip: ranking(trip, objective))
    per_route = []
    for route in routes:
        best = next((trip for trip in ranked if trip.route == route), None)
        if best is None:
            best = min((trip for trip in trips if trip.route == route),
                       key=lambda trip: trip.arrival)
        per_route.append(best)
    best = next((trip for trip in ranked if objective not in {"cost", "score"} or trip.cost is not None), None)
    return trips, per_route, best


def web_plan(query: str) -> dict:
    fields = parse_qs(query, keep_blank_values=True, max_num_fields=6)
    return study_plan(fields, ROUTES)


def study_plan(fields: dict[str, list[str]], routes: tuple[Route, ...]) -> dict:
    start = parse_time(fields["start"][0])
    end = parse_time(fields["end"][0])
    deadline_text = fields["deadline"][0]
    deadline = parse_time(deadline_text) if deadline_text else None
    trips, per_route, best = plan(start, end, deadline, float(fields["price"][0]),
                                  float(fields["valueOfTime"][0]), fields["objective"][0], routes)
    return {
        "trips": [{
            "route": {"name": trip.route.name, "toll": trip.route.toll},
            "departure": format_time(trip.departure),
            "arrival": format_time(trip.arrival),
            "duration": trip.duration,
            "km": trip.km,
            "fuel": trip.fuel,
            "fuelCost": trip.fuel_cost,
            "cost": trip.cost,
            "score": trip.score,
            "feasible": is_feasible(trip, deadline),
        } for trip in trips],
        "perRoute": [trips.index(trip) for trip in per_route],
        "best": trips.index(best) if best is not None else None,
        "earliest": format_time(min(trip.arrival for trip in trips)),
        "legs": [{"name": segment.name, "km": segment.km, "paid": segment.paid}
                 for segment in best.route.segments] if best is not None else [],
    }


def compare_cities(query: str) -> dict:
    fields = parse_qs(query, keep_blank_values=True, max_num_fields=10)
    if "departure" in fields:
        return fixed_departure(fields)
    return compare_city_window(fields)


def compare_city_window(fields: dict[str, list[str]]) -> dict:
    start, finish = (find_city(fields[key][0]) for key in ("origin", "destination"))
    if start == finish:
        raise ValueError("Выберите два разных города.")
    peak = float(fields["peak"][0])
    if not isfinite(peak) or not 0 <= peak <= 120:
        raise ValueError("Задержка в час пик должна быть от 0 до 120 мин на 100 км.")

    candidates = discover_routes(start, finish, os.environ.get("ROUTE_PLANNER_OSRM_URL", OSRM_URL))
    tolls = json.loads(fields.get("tolls", ["{}"])[0])
    if not isinstance(tolls, dict) or set(tolls) - {candidate["id"] for candidate in candidates}:
        raise ValueError("Список маршрутов изменился. Запустите поиск заново.")
    routes = []
    for index, candidate in enumerate(candidates, 1):
        kind = candidate["toll_road"]
        name = f"Вариант {index} · " + ("с платными участками" if kind is True
                                     else "без платных участков" if kind is False
                                     else "платность не определена")
        amount = tolls.get(candidate["id"])
        toll = float(amount) if amount is not None else (0 if kind is False else None)
        if toll is not None and (not isfinite(toll) or not 0 <= toll <= 100000):
            raise ValueError("Плата за проезд должна быть от 0 до 100 000 ₽.")
        km = candidate["distance"] / 1000
        speed = km * 3600 / candidate["duration"]
        rates = ((speed, 0), (speed, peak / 100), (speed, peak / 100))
        routes.append(Route(name, toll, (Segment(name, km, 0, None, rates, paid=kind is True),)))

    result = study_plan(fields, tuple(routes))
    return {"origin": start.label, "destination": finish.label, "study": result,
            "candidates": [{"id": candidate["id"], "name": route.name,
                            "toll": route.toll, "geometry": candidate["geometry"],
                            "paidRoads": candidate.get("paid_roads", [])}
                           for candidate, route in zip(candidates, routes)],
            "provider": candidates[0]["provider"],
            "missingTolls": sum(route.toll is None for route in routes)}


def fixed_departure(fields: dict[str, list[str]]) -> dict:
    departure = parse_time(fields["departure"][0])
    start, finish = (find_city(fields[key][0]) for key in ("origin", "destination"))
    if start == finish:
        raise ValueError("Выберите два разных города.")

    pair = (start.name, finish.name)
    reverse = pair not in CORRIDORS and pair[::-1] in CORRIDORS
    corridor_key = pair[::-1] if reverse else pair
    corridor = CORRIDORS.get(corridor_key)
    if corridor:
        routes, sources = corridor
        if reverse:
            routes = tuple(replace(route, segments=tuple(
                replace(segment, grade=-segment.grade) for segment in reversed(route.segments)
            )) for route in routes)
        if corridor_key == ("Москва", "Санкт-Петербург"):
            full_m11 = routes[-1]
            concession = next(index for index, segment in enumerate(full_m11.segments)
                              if segment.name == MOSCOW_M11.name)
            to_toll_exit = replace(full_m11, segments=full_m11.segments[:concession + 1])
            crossing = simulate(to_toll_exit, departure, CITY_FUEL_PRICE, 0).arrival % MINUTES_PER_DAY
            concession_toll = 680 if 60 <= crossing < 360 else 950
            routes = (*routes[:-1], replace(full_m11, toll=3956 + concession_toll))
        candidates = [{
            "name": route.name,
            "geometry": None,
            "roads": [f"{segment.name} ({segment.toll_rubles} ₽)"
                      if segment.toll_rubles is not None else segment.name
                      for segment in route.segments],
        } for route in routes]
        provider = "Учебные маршруты по опубликованным тарифам"
    else:
        found = discover_routes(start, finish, os.environ.get("ROUTE_PLANNER_OSRM_URL", OSRM_URL))
        routes = []
        candidates = []
        for index, route in enumerate(found, 1):
            paid = route["toll_road"]
            kind = "с платными участками" if paid is True else (
                "без платных участков" if paid is False else "платность не определена")
            name = f"Вариант {index} · {kind}"
            km = route["distance"] / 1000
            speed = km * 3600 / route["duration"]
            rates = ((speed, 0), (speed, CITY_PEAK_DELAY / 100), (speed, CITY_PEAK_DELAY / 100))
            routes.append(Route(name, 0 if paid is False else None, (Segment(name, km, 0, None, rates),)))
            candidates.append({"name": name, "geometry": route["geometry"],
                               "roads": route.get("paid_roads", [])})
        sources = ()
        provider = found[0]["provider"]

    # Тот же расчёт расхода и затрат, ровно на заданное время.
    time = format_time(departure)
    study = study_plan({"start": [time], "end": [time], "deadline": [""],
                        "price": [str(CITY_FUEL_PRICE)], "valueOfTime": ["0"],
                        "objective": ["cost"]}, tuple(routes))
    trips = study["trips"]
    return {"origin": start.label, "destination": finish.label, "study": study,
            "candidates": candidates, "provider": provider, "sources": sources,
            "fuelPrice": CITY_FUEL_PRICE, "estimated": corridor is not None,
            "missingTolls": sum(route.toll is None for route in routes),
            "fastest": min(range(len(trips)), key=lambda index: trips[index]["duration"]),
            "fuelBest": min(range(len(trips)), key=lambda index: trips[index]["fuel"])}


def serve_web() -> None:
    try:
        page = Path(__file__).with_name("route_planner_web.html").read_bytes()
    except OSError as exc:
        raise SystemExit(f"Не удалось открыть route_planner_web.html рядом со скриптом: {exc}") from exc

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            target = urlsplit(self.path)
            if target.path == "/":
                self.respond(200, "text/html; charset=utf-8", page)
                return
            if target.path not in {"/api/plan", "/api/cities", "/api/city-route", "/api/city-compare"}:
                self.send_error(404)
                return
            try:
                if target.path == "/api/cities":
                    result = sorted(city.label for city in cities())
                elif target.path == "/api/city-compare":
                    result = compare_cities(target.query)
                elif target.path == "/api/city-route":
                    fields = parse_qs(target.query, keep_blank_values=True, max_num_fields=3)
                    result = city_plan(fields["origin"][0], fields["destination"][0],
                                       fields["objective"][0], consumption)
                else:
                    result = web_plan(target.query)
                status = 200
            except (KeyError, argparse.ArgumentTypeError):
                result, status = {"error": "Проверьте заполнение полей."}, 400
            except RoutingUnavailable as exc:
                result, status = {"error": str(exc)}, 502
            except OSError:
                result, status = {"error": "Не удалось загрузить справочник городов."}, 500
            except ValueError as exc:
                result, status = {"error": str(exc)}, 400
            self.respond(status, "application/json; charset=utf-8",
                         json.dumps(result, ensure_ascii=False).encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:
            pass

    try:
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            url = f"http://127.0.0.1:{server.server_port}/"
            print(f"Планировщик: {url}  ·  Для остановки нажмите Ctrl+C.", flush=True)
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    except OSError as exc:
        raise SystemExit(f"Не удалось запустить локальный сервер: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Учебный планировщик Клин → Тверь.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--web", action="store_true", help="открыть планировщик в браузере")
    mode.add_argument("--cli", action="store_true", help="вывести расчёт в Терминале")
    parser.add_argument("--start", type=parse_time, default="07:30", help="начало окна выезда, МСК")
    parser.add_argument("--end", type=parse_time, default="09:00", help="конец окна выезда, МСК")
    parser.add_argument("--deadline", type=parse_time, help="срок прибытия в день выезда, МСК")
    parser.add_argument("--price", type=float, default=65, help="цена топлива, ₽/л")
    parser.add_argument("--value-of-time", type=float, default=400, help="оценка времени, ₽/ч")
    parser.add_argument("--objective", choices=OBJECTIVES, default="cost",
                        help="cost — расходы, fuel — топливо, duration — время, score — расходы со временем")
    parser.add_argument("--all", action="store_true", help="показать результаты для всех выездов")
    args = parser.parse_args()

    if args.web or not sys.argv[1:]:
        serve_web()
        return
    try:
        trips, per_route, best = plan(args.start, args.end, args.deadline, args.price,
                                      args.value_of_time, args.objective)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Клин → Тверь · МСК · проверено вариантов: {len(trips)}")
    print(f"Критерий — {OBJECTIVES[args.objective]}.")
    if best is not None:
        print(f"Выбран {best.route.name}, выезд {format_time(best.departure)}, "
              f"прибытие {format_time(best.arrival)}.")
        print("Участки — " + " → ".join(segment.name for segment in best.route.segments))
    else:
        earliest = min(trip.arrival for trip in trips)
        print(f"Нет маршрута, укладывающегося в срок. Самое раннее прибытие — {format_time(earliest)}.")

    print()
    print_table(trips if args.all else per_route, args.deadline)
    if args.deadline is not None:
        print("* Прибытие после заданного срока.")


if __name__ == "__main__":
    main()
