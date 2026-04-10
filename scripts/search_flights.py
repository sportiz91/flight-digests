"""Search flights from BCN/MAD/LIS to EZE over a configurable date window.

Outputs a JSON array of search results to stdout. No secrets touched here —
this script is designed to run in an isolated GitHub Actions job.

Usage:
    python search_flights.py [--out path/to/results.json]
"""
import argparse
import json
import random
import sys
import time
from datetime import date, timedelta

# ─── Cookie monkey-patch ───────────────────────────────────────────────────────
# fast-flights doesn't expose a way to inject cookies into its HTTP client.
# Without the Google consent cookies, EU IPs (and the user's WSL) get redirected
# to the GDPR consent screen and parsing returns empty. We replace the library's
# fetch_flights_html with a cookie-aware version. The cookies below are the
# standard "consent given" pair Google honors from any region.
from fast_flights import fetcher as ff_fetcher  # noqa: E402
import fast_flights  # noqa: E402
from primp import Client  # noqa: E402

GOOGLE_CONSENT_COOKIES = {
    "CONSENT": "YES+cb.20210720-07-p0.en+FX+410",
    "SOCS": "CAESHAgBEhJnd3NfMjAyNDA0MDgtMF9SQzIaAmVuIAEaBgiAyJSwBg",
}


def _patched_fetch_flights_html(q, /, *, proxy=None, integration=None):
    if integration is not None:
        return ff_fetcher.fetch_flights_html.__wrapped__(q, proxy=proxy, integration=integration)
    client = Client(
        impersonate="chrome_133",
        impersonate_os="macos",
        referer=True,
        proxy=proxy,
        cookie_store=True,
    )
    params = q.params() if hasattr(q, "params") else {"q": q}
    res = client.get(
        "https://www.google.com/travel/flights",
        params=params,
        cookies=GOOGLE_CONSENT_COOKIES,
    )
    return res.text


ff_fetcher.fetch_flights_html = _patched_fetch_flights_html
fast_flights.fetch_flights_html = _patched_fetch_flights_html
# ──────────────────────────────────────────────────────────────────────────────

from fast_flights import (  # noqa: E402
    FlightQuery,
    Passengers,
    create_query,
    get_flights,
)

ORIGINS = ["BCN", "MAD", "LIS"]
DESTINATION = "EZE"

DAYS_OUT_MIN = 60
DAYS_OUT_MAX = 180
TRIP_LEN_MIN = 30
TRIP_LEN_MAX = 60
COMBOS_PER_ORIGIN = 6
MAX_STOPS = 2
SEAT = "economy"
LANGUAGE = "en"
CURRENCY = "EUR"

# Stopover routes — manual stays of 1-3 days in a connecting city.
# Each route is searched as 3 separate one-way queries that get summed
# (the "buy each leg separately" pattern). Google Flights does not pre-render
# multi-city results in HTML, so we can't parse a single multi-city query.
# direction: "out" = stopover on the way to EZE, "back" = stopover on return
STOPOVER_ROUTES = [
    {"direction": "out", "origin": "BCN", "via": "LIS", "stop_days": 2},   # TAP stopover
    {"direction": "out", "origin": "MAD", "via": "GRU", "stop_days": 2},   # LATAM via São Paulo
    {"direction": "back", "origin": "BCN", "via": "GRU", "stop_days": 2},  # cousin's pattern
    {"direction": "back", "origin": "MAD", "via": "GRU", "stop_days": 2},  # cousin's pattern (MAD)
]
STOPOVER_COMBOS_PER_ROUTE = 2  # date pairs per stopover route


def date_combinations(seed: int):
    """Generate COMBOS_PER_ORIGIN deterministic-but-spread date pairs."""
    rng = random.Random(seed)
    today = date.today()
    pairs = []
    span = DAYS_OUT_MAX - DAYS_OUT_MIN
    step = span // COMBOS_PER_ORIGIN
    for i in range(COMBOS_PER_ORIGIN):
        out_offset = DAYS_OUT_MIN + i * step + rng.randint(0, max(step - 1, 1))
        trip_len = rng.randint(TRIP_LEN_MIN, TRIP_LEN_MAX)
        outbound = today + timedelta(days=out_offset)
        inbound = outbound + timedelta(days=trip_len)
        pairs.append((outbound.isoformat(), inbound.isoformat()))
    return pairs


def _one_way_cheapest(origin: str, dest: str, day: str, max_stops: int = 1):
    """Run a one-way search and return the cheapest option as a dict, or None."""
    try:
        q = create_query(
            flights=[FlightQuery(date=day, from_airport=origin, to_airport=dest)],
            seat=SEAT,
            trip="one-way",
            passengers=Passengers(adults=1),
            language=LANGUAGE,
            currency=CURRENCY,
            max_stops=max_stops,
        )
        result = get_flights(q)
    except Exception as e:
        print(
            f"  [skip] {origin}->{dest} {day}: {type(e).__name__}: {str(e)[:100]}",
            file=sys.stderr,
        )
        return None

    if not result:
        return None

    cheapest = min(result, key=lambda f: f.price or 999999)
    return {
        "from": origin,
        "to": dest,
        "date": day,
        "price_eur": cheapest.price,
        "airlines": cheapest.airlines,
        "duration_min": sum(f.duration or 0 for f in cheapest.flights),
        "url": q.url(),
    }


def _multicity_url(legs: list) -> str:
    """Build a Google Flights multi-city URL for the given legs (for the user to inspect)."""
    flights = [
        FlightQuery(date=leg["date"], from_airport=leg["from"], to_airport=leg["to"])
        for leg in legs
    ]
    q = create_query(
        flights=flights,
        seat=SEAT,
        trip="multi-city",
        passengers=Passengers(adults=1),
        language=LANGUAGE,
        currency=CURRENCY,
        max_stops=1,
    )
    return q.url()


def search_stopover(route: dict, outbound_day: str, return_day: str) -> dict | None:
    """Search a stopover route by decomposing it into 3 one-way queries.

    For direction='out': origin -> via -> EZE ... return EZE -> origin
    For direction='back': origin -> EZE ... return EZE -> via -> origin
    """
    origin = route["origin"]
    via = route["via"]
    stop_days = route["stop_days"]

    if route["direction"] == "out":
        leg1_date = outbound_day  # origin -> via
        leg2_date = (date.fromisoformat(outbound_day) + timedelta(days=stop_days)).isoformat()  # via -> EZE
        leg3_date = return_day    # EZE -> origin (direct)
        legs_def = [
            (origin, via, leg1_date),
            (via, DESTINATION, leg2_date),
            (DESTINATION, origin, leg3_date),
        ]
    else:  # back
        leg1_date = outbound_day  # origin -> EZE (direct)
        leg2_date = (date.fromisoformat(return_day) - timedelta(days=stop_days)).isoformat()  # EZE -> via
        leg3_date = return_day    # via -> origin
        legs_def = [
            (origin, DESTINATION, leg1_date),
            (DESTINATION, via, leg2_date),
            (via, origin, leg3_date),
        ]

    legs = []
    for frm, to, day in legs_def:
        leg = _one_way_cheapest(frm, to, day, max_stops=1)
        if leg is None:
            print(f"    [skip stopover] missing leg {frm}->{to} {day}", file=sys.stderr)
            return None
        legs.append(leg)
        time.sleep(0.6)  # gentle pacing within a single stopover route

    total_price = sum(leg["price_eur"] or 0 for leg in legs)
    total_duration = sum(leg["duration_min"] or 0 for leg in legs)

    return {
        "kind": f"stopover-{route['direction']}",
        "origin": origin,
        "destination": DESTINATION,
        "stopover_city": via,
        "stop_days": stop_days,
        "outbound_date": outbound_day,
        "return_date": return_day,
        "trip_days": (date.fromisoformat(return_day) - date.fromisoformat(outbound_day)).days,
        "legs": legs,
        "total_price_eur": total_price,
        "total_duration_min": total_duration,
        # Multi-city URL on Google Flights — Google may show a single-ticket
        # price that's different (often lower) than our decomposed sum.
        "search_url": _multicity_url([
            {"from": frm, "to": to, "date": day} for frm, to, day in legs_def
        ]),
    }


def search_one(origin: str, outbound: str, inbound: str) -> dict | None:
    """Search a single round-trip combo. Returns the cheapest option or None."""
    try:
        q = create_query(
            flights=[
                FlightQuery(date=outbound, from_airport=origin, to_airport=DESTINATION),
                FlightQuery(date=inbound, from_airport=DESTINATION, to_airport=origin),
            ],
            seat=SEAT,
            trip="round-trip",
            passengers=Passengers(adults=1),
            language=LANGUAGE,
            currency=CURRENCY,
            max_stops=MAX_STOPS,
        )
        result = get_flights(q)
    except Exception as e:
        print(
            f"  [skip] {origin} {outbound}/{inbound}: {type(e).__name__}: {str(e)[:120]}",
            file=sys.stderr,
        )
        return None

    if not result:
        return None

    # Find cheapest option (lowest price)
    cheapest = min(result, key=lambda f: f.price or 999999)

    # Compute total stops (sum across all legs minus number of legs to get connections)
    total_legs = len(cheapest.flights)
    stops = max(total_legs - 2, 0)  # round-trip has 2 main flights, extras are stops

    # Total duration in minutes (sum of all leg durations)
    total_duration_min = sum(f.duration or 0 for f in cheapest.flights)

    return {
        "origin": origin,
        "destination": DESTINATION,
        "outbound_date": outbound,
        "return_date": inbound,
        "trip_days": (date.fromisoformat(inbound) - date.fromisoformat(outbound)).days,
        "airlines": cheapest.airlines,
        "price_eur": cheapest.price,
        "stops": stops,
        "total_legs": total_legs,
        "total_duration_min": total_duration_min,
        "type": cheapest.type,
        "total_offers": len(result),
        # Pre-filled Google Flights URL — clicking it lands on the same search
        "search_url": q.url(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None, help="Optional output file (default: stdout)")
    parser.add_argument("--seed", type=int, default=int(date.today().strftime("%Y%m%d")))
    args = parser.parse_args()

    print(
        f"flight-digests scan: {ORIGINS} -> {DESTINATION}",
        file=sys.stderr,
    )
    print(
        f"window: +{DAYS_OUT_MIN}..+{DAYS_OUT_MAX} days, "
        f"trip {TRIP_LEN_MIN}-{TRIP_LEN_MAX} days, max {MAX_STOPS} stops",
        file=sys.stderr,
    )

    # ── Direct round-trip searches ────────────────────────────────────────
    results = []
    for origin in ORIGINS:
        pairs = date_combinations(seed=args.seed + (hash(origin) % 1000))
        print(f"\n[{origin}] {len(pairs)} date combos", file=sys.stderr)
        for outbound, inbound in pairs:
            print(f"  → {outbound} / {inbound}", file=sys.stderr)
            r = search_one(origin, outbound, inbound)
            if r:
                results.append(r)
                airlines = ", ".join(r["airlines"])
                print(
                    f"    ✓ €{r['price_eur']} | {airlines} | "
                    f"{r['stops']} stops | {r['total_duration_min']}min",
                    file=sys.stderr,
                )
            time.sleep(1.2)  # gentle pacing

    # ── Stopover searches (multi-city decomposed into one-way legs) ───────
    print(f"\n=== Stopover routes ({len(STOPOVER_ROUTES)} routes) ===", file=sys.stderr)
    stopover_results = []
    for route in STOPOVER_ROUTES:
        route_label = (
            f"{route['origin']}→{route['via']}→EZE"
            if route["direction"] == "out"
            else f"EZE→{route['via']}→{route['origin']}"
        )
        # Use a different seed offset so stopover dates don't collide with direct dates
        pairs = date_combinations(seed=args.seed + (hash(route_label) % 1000))[:STOPOVER_COMBOS_PER_ROUTE]
        print(f"\n[{route_label}] {len(pairs)} date combos · stop {route['stop_days']}d", file=sys.stderr)
        for outbound, inbound in pairs:
            print(f"  → {outbound} / {inbound}", file=sys.stderr)
            r = search_stopover(route, outbound, inbound)
            if r:
                stopover_results.append(r)
                airlines_summary = " + ".join(", ".join(leg["airlines"]) for leg in r["legs"])
                print(
                    f"    ✓ €{r['total_price_eur']} (sum) | {airlines_summary}",
                    file=sys.stderr,
                )
            time.sleep(0.8)

    payload = {
        "fetched_at": int(time.time()),
        "fetched_at_iso": date.today().isoformat(),
        "origins": ORIGINS,
        "destination": DESTINATION,
        "currency": CURRENCY,
        "results": results,
        "stopover_results": stopover_results,
        "count": len(results),
        "stopover_count": len(stopover_results),
    }

    output = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w") as f:
            f.write(output)
        print(f"\n[ok] wrote {len(results)} results to {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
