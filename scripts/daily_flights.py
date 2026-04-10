"""Daily flight digest → Telegram bot.

Reads search results from a JSON file (produced by search_flights.py in a
separate, secret-less job), compares against price_history.json to detect
drops, calls Groq for a rioplatense analysis, and sends an HTML message via
the Telegram Bot API.

Designed to run in a GitHub Actions job that has access to:
  - FLIGHT_TELEGRAM_BOT_TOKEN
  - FLIGHT_TELEGRAM_CHAT_ID
  - GROQ_API_KEY

Usage:
    python daily_flights.py --results path/to/results.json [--history path/to/history.json]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date

TELEGRAM_BOT_TOKEN = os.environ.get("FLIGHT_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("FLIGHT_TELEGRAM_CHAT_ID", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


# ─── Price history ────────────────────────────────────────────────────────────
def load_history(path: str) -> dict:
    if not os.path.exists(path):
        return {"runs": []}
    with open(path) as f:
        return json.load(f)


def save_history(path: str, history: dict, results: list):
    """Append today's snapshot. Keep last 30 runs."""
    snapshot = {
        "date": date.today().isoformat(),
        "min_by_origin": {},
    }
    for r in results:
        o = r["origin"]
        cur = snapshot["min_by_origin"].get(o)
        if cur is None or r["price_eur"] < cur:
            snapshot["min_by_origin"][o] = r["price_eur"]

    history.setdefault("runs", []).append(snapshot)
    history["runs"] = history["runs"][-30:]

    with open(path, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def previous_min_by_origin(history: dict) -> dict:
    """Return the most recent previous run's min_by_origin (excluding today)."""
    today = date.today().isoformat()
    for run in reversed(history.get("runs", [])):
        if run["date"] != today:
            return run["min_by_origin"]
    return {}


# ─── Groq analysis ────────────────────────────────────────────────────────────
def ai_analysis(results: list, prev_min: dict) -> str:
    if not GROQ_API_KEY:
        return ""

    by_origin = {}
    for r in results:
        by_origin.setdefault(r["origin"], []).append(r)

    summary_lines = []
    for origin, items in by_origin.items():
        items.sort(key=lambda x: x["price_eur"])
        prev = prev_min.get(origin)
        delta = ""
        if prev and items:
            d = items[0]["price_eur"] - prev
            delta = f" (vs ayer {prev}€ → {'+' if d >= 0 else ''}{d}€)"
        summary_lines.append(f"\n{origin} → EZE{delta}:")
        for r in items[:3]:
            airlines = ", ".join(r["airlines"])
            summary_lines.append(
                f"  - €{r['price_eur']} | {r['outbound_date']} → {r['return_date']} "
                f"({r['trip_days']}d) | {airlines} | {r['stops']} escalas"
            )

    prompt = f"""Sos un analista de vuelos que escribe en español argentino (voseo, rioplatense, directo).
Analizá estas búsquedas de vuelos de hoy desde Barcelona/Madrid/Lisboa hacia Buenos Aires (EZE).

Datos:
{"".join(summary_lines)}

Decime, en máximo 200 palabras:
1. Cuál es la MEJOR oferta de hoy y por qué (precio + ruta + fecha)
2. ¿Conviene comprar ya o esperar? Justificá con datos
3. ¿Hay algún patrón interesante? (ej: Madrid sale mucho más barato, ciertas fechas son ventana de oro, etc.)
4. Una recomendación accionable concreta

Sé directo y opinado. Nada de "depende" o "podría". Cero hedging."""

    payload = json.dumps({
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 700,
    }).encode()

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "flight-digests/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  AI analysis failed: {e}", file=sys.stderr)
        return ""


# ─── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] No Telegram config, printing to stdout:\n")
        print(text)
        return

    chunks = []
    while len(text) > 4096:
        split_at = text.rfind("\n", 0, 4096)
        if split_at == -1:
            split_at = 4096
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)

    for i, chunk in enumerate(chunks):
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if result.get("ok"):
                    print(f"  Telegram batch {i+1}/{len(chunks)} sent")
                else:
                    print(f"  Telegram error: {result}", file=sys.stderr)
        except Exception as e:
            print(f"  Telegram send failed: {e}", file=sys.stderr)


# ─── Message formatting ───────────────────────────────────────────────────────
ORIGIN_FLAG = {"BCN": "🇪🇸", "MAD": "🇪🇸", "LIS": "🇵🇹"}


def trend_arrow(now: int, prev: int | None) -> str:
    if prev is None:
        return ""
    if now < prev - 5:
        diff = prev - now
        return f" 🟢 −€{diff}"
    if now > prev + 5:
        diff = now - prev
        return f" 🔴 +€{diff}"
    return " ⚪ ="


def fmt_duration(minutes: int) -> str:
    """Format minutes as Xh YYm (e.g. 16h 35m)."""
    if not minutes:
        return ""
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m"


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_offer(r: dict, *, is_winner: bool = False) -> str:
    airlines = html_escape(", ".join(r["airlines"]))
    duration = fmt_duration(r.get("total_duration_min", 0))
    url = r.get("search_url", "")
    price_label = f"€{r['price_eur']}"
    if is_winner:
        price_label = f"🏆 {price_label}"
    if url:
        price_html = f'<a href="{url}"><b>{price_label}</b></a>'
    else:
        price_html = f"<b>{price_label}</b>"
    return (
        f"  • {price_html} · {r['outbound_date']} → {r['return_date']} "
        f"({r['trip_days']}d · {duration}) · {airlines} · {r['stops']} esc"
    )


def build_message(payload: dict, prev_min: dict, analysis: str = "") -> str:
    results = payload["results"]
    if not results:
        return "<b>✈️ Flight Digests</b>\n\nNo se pudo obtener data hoy. Revisá el log del workflow."

    # Find the absolute winner across all origins
    winner = min(results, key=lambda x: x["price_eur"])
    winner_id = (winner["origin"], winner["outbound_date"], winner["return_date"])

    lines = [f"<b>✈️ Flight Digests — {payload['fetched_at_iso']}</b>"]
    lines.append(f"<i>BCN/MAD/LIS → EZE · {payload['count']} búsquedas</i>")

    # Headline with the winner — clickable
    winner_airlines = html_escape(", ".join(winner["airlines"]))
    winner_url = winner.get("search_url", "")
    winner_link = (
        f'<a href="{winner_url}">€{winner["price_eur"]}</a>' if winner_url else f'€{winner["price_eur"]}'
    )
    lines.append(
        f'\n🏆 <b>Mejor del día:</b> {winner_link} desde {winner["origin"]} '
        f'({winner_airlines}, {winner["outbound_date"]} → {winner["return_date"]})'
    )

    by_origin = {}
    for r in results:
        by_origin.setdefault(r["origin"], []).append(r)

    for origin in ["BCN", "MAD", "LIS"]:
        items = by_origin.get(origin, [])
        if not items:
            continue
        items.sort(key=lambda x: x["price_eur"])
        cheapest = items[0]
        flag = ORIGIN_FLAG.get(origin, "")
        arrow = trend_arrow(cheapest["price_eur"], prev_min.get(origin))

        lines.append(f"\n<b>{flag} {origin} → EZE</b>{arrow}")
        for r in items[:3]:
            is_winner = (r["origin"], r["outbound_date"], r["return_date"]) == winner_id
            lines.append(render_offer(r, is_winner=is_winner))

    if analysis:
        lines.append(f"\n\n<b>🤖 Análisis</b>\n\n{html_escape(analysis)}")

    lines.append("\n\n<i>tap any price to open Google Flights · 1 adulto · economy</i>")
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True, help="Path to search results JSON")
    parser.add_argument("--history", default="price_history.json", help="Path to price history JSON")
    args = parser.parse_args()

    with open(args.results) as f:
        payload = json.load(f)

    print(f"[ok] loaded {payload['count']} results from {args.results}")

    history = load_history(args.history)
    prev_min = previous_min_by_origin(history)
    print(f"[ok] previous min by origin: {prev_min}")

    print("[..] running AI analysis")
    analysis = ai_analysis(payload["results"], prev_min)

    print("[..] building message")
    message = build_message(payload, prev_min, analysis)

    print("[..] sending to Telegram")
    send_telegram(message)

    print("[..] saving history")
    save_history(args.history, history, payload["results"])

    print("[ok] done")


if __name__ == "__main__":
    main()
