"""Учебная оптимизация маршрута и времени выезда из Клина в Тверь."""

import argparse
from dataclasses import dataclass
from math import ceil, isfinite
import re

MINUTES_PER_DAY = 1440
IDLE_LITRES_PER_HOUR = 0.6
EPSILON = 1e-8
OBJECTIVES = {
    "cost": "прямые расходы",
    "fuel": "расход топлива",
    "duration": "время в пути",
    "score": "расходы с оценкой времени",
}


@dataclass(frozen=True)
class Segment:
    name: str
    km: int
    grade: float
    radius: int
    # Пары (км/ч, мин задержки на км) вне пика, утром и вечером.
    rates: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Route:
    name: str
    toll: int
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class Trip:
    route: Route
    departure: int
    arrival: float
    km: int
    fuel: float
    fuel_cost: float
    score: float

    @property
    def duration(self) -> float:
        return self.arrival - self.departure

    @property
    def cost(self) -> float:
        return self.fuel_cost + self.route.toll


FREE_WEST = Segment("М-10: Клин — Завидово", 31, 0.45, 850,
                    ((70, 0.07), (55, 0.24), (60, 0.18)))
FREE_EAST = Segment("М-10: Завидово — Тверь-Юг", 35, 0.45, 900,
                    ((72, 0.08), (42, 0.47), (55, 0.25)))
KLIN_ACCESS = Segment("А-108: Клин — въезд на М-11", 9, 0.35, 700,
                      ((55, 0.07), (44, 0.20), (46, 0.17)))
ZAVIDOVO_ACCESS = Segment("Подъезд к М-11 у Завидова", 4, 0.30, 550,
                          ((50, 0.04), (45, 0.09), (45, 0.09)))
PAID_WEST = Segment("М-11: ПВП 89 — 124", 34, 0.20, 2000,
                    ((90, 0.01), (88, 0.03), (88, 0.03)))
PAID_EAST = Segment("М-11: 124 — 149 км", 25, 0.18, 2000,
                    ((90, 0.01), (88, 0.03), (88, 0.03)))
TVER_ACCESS = Segment("Тверь-Юг — центр Твери", 15, 0.25, 700,
                      ((48, 0.14), (34, 0.42), (38, 0.31)))

ROUTES = (
    Route("М-10", 0, (FREE_WEST, FREE_EAST, TVER_ACCESS)),
    Route("М-10 → М-11", 140, (FREE_WEST, ZAVIDOVO_ACCESS, PAID_EAST, TVER_ACCESS)),
    Route("М-11", 370, (KLIN_ACCESS, PAID_WEST, PAID_EAST, TVER_ACCESS)),
)


def consumption(speed: float, grade: float, radius: int) -> float:
    """Расход в л/100 км при нулевом ускорении по модели первой работы."""
    # FuelCoeffs.from_vehicle(VehicleParams()), vehicle-route.
    # Коэффициент спуска в исходной калибровке равен нулю.
    flat = 6.700070172932973 - 0.08112399995420332 * speed + 0.0008807510214262922 * speed**2
    climb = 1.8327313488603818 * max(grade, 0)
    turning = 0.002 * speed**2 / radius
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
                fuel += driven * (consumption(speed, grade, segment.radius) / 100
                                  + IDLE_LITRES_PER_HOUR * idle / 60)
                remaining -= driven

    fuel_cost = fuel * price
    score = fuel_cost + route.toll + value_of_time * (time - departure) / 60
    return Trip(route, departure, time, sum(s.km for s in route.segments), fuel, fuel_cost, score)


def ranking(trip: Trip, objective: str) -> tuple[float, ...]:
    return getattr(trip, objective), trip.cost, trip.fuel, trip.arrival, trip.departure


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Учебный планировщик Клин → Тверь.")
    parser.add_argument("--start", type=parse_time, default="07:30", help="начало окна выезда, МСК")
    parser.add_argument("--end", type=parse_time, default="09:00", help="конец окна выезда, МСК")
    parser.add_argument("--deadline", type=parse_time, help="срок прибытия в день выезда, МСК")
    parser.add_argument("--price", type=float, default=65, help="цена топлива, ₽/л")
    parser.add_argument("--value-of-time", type=float, default=400, help="оценка времени, ₽/ч")
    parser.add_argument("--objective", choices=OBJECTIVES, default="cost",
                        help="cost — расходы, fuel — топливо, duration — время, score — расходы со временем")
    parser.add_argument("--all", action="store_true", help="показать результаты для всех выездов")
    args = parser.parse_args()

    if args.start > args.end:
        parser.error("конец окна выезда должен быть не раньше начала")
    if args.deadline is not None and args.deadline < args.start:
        parser.error("срок прибытия должен быть не раньше начала окна выезда")
    if not isfinite(args.price) or not 0 < args.price <= 1000:
        parser.error("цена топлива должна быть больше 0 и не выше 1000 ₽/л")
    if not isfinite(args.value_of_time) or not 0 <= args.value_of_time <= 10000:
        parser.error("оценка времени должна быть от 0 до 10 000 ₽/ч")

    departures = [*range(args.start, args.end, 15), args.end]
    trips = [
        simulate(route, departure, args.price, args.value_of_time)
        for departure in departures
        for route in ROUTES
    ]
    feasible = [
        trip for trip in trips
        if args.deadline is None or trip.arrival <= args.deadline + EPSILON
    ]
    ranked = sorted(feasible, key=lambda trip: ranking(trip, args.objective))

    print(f"Клин → Тверь · МСК · проверено вариантов: {len(trips)}")
    print(f"Критерий — {OBJECTIVES[args.objective]}.")
    if ranked:
        best = ranked[0]
        print(f"Выбран {best.route.name}, выезд {format_time(best.departure)}, "
              f"прибытие {format_time(best.arrival)}.")
        print("Участки — " + " → ".join(segment.name for segment in best.route.segments))
    else:
        earliest = min(trip.arrival for trip in trips)
        print(f"Нет маршрута, укладывающегося в срок. Самое раннее прибытие — {format_time(earliest)}.")

    if args.all:
        shown = trips
    else:
        shown = []
        for route in ROUTES:
            route_best = next((trip for trip in ranked if trip.route == route), None)
            if route_best is None:
                route_best = min((trip for trip in trips if trip.route == route),
                                 key=lambda trip: trip.arrival)
            shown.append(route_best)
    print()
    print_table(shown, args.deadline)
    if args.deadline is not None:
        print("* Прибытие после заданного срока.")


if __name__ == "__main__":
    main()
