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
TRIP_LEN_MIN = 14
TRIP_LEN_MAX = 30
COMBOS_PER_ORIGIN = 6
MAX_STOPS = 2
SEAT = "economy"
LANGUAGE = "en"
CURRENCY = "EUR"


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

    payload = {
        "fetched_at": int(time.time()),
        "fetched_at_iso": date.today().isoformat(),
        "origins": ORIGINS,
        "destination": DESTINATION,
        "currency": CURRENCY,
        "results": results,
        "count": len(results),
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
