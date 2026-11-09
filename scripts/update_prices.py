#!/usr/bin/env python3
"""Refresh hotel prices and write site/data/prices.json.

One search per hotel: SerpApi's Google Hotels Price Calendar returns the best
one-night price for every date in a range, so a single call fills a hotel's
whole calendar. Each run spends a fixed daily budget on the hotels that need
it most:

  1. watchlist hotels older than `watch_refresh_hours`
  2. then half the remaining budget on hotels never priced yet,
     half on the stalest priced hotels (cheap hotels age faster,
     because they are the ones worth using the credit on)

Needs the SERPAPI_KEY environment variable. Standard library only.

  python3 scripts/update_prices.py                 # normal daily run
  python3 scripts/update_prices.py --max-searches 50
  python3 scripts/update_prices.py --dry-run       # show the plan, spend nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "catalog.json"
PRICES = ROOT / "site" / "data" / "prices.json"
CONFIG = ROOT / "config.json"
API = os.environ.get("SERPAPI_BASE", "https://serpapi.com").rstrip("/")

DEFAULTS = {
    "monthly_searches": 250,
    "horizon_days": 240,
    "programs": ["FHR", "THC"],
    "countries": [],
    "regions": [],
    "watchlist": [],
    "max_hotels": 0,
    "focus_max_rate": 400,
    "focus_weight": 3,
    "watch_refresh_hours": 24,
    "currency": "USD",
    "gl": "us",
    "adults": 2,
    "default_tax": 0.15,
}


class StopRun(Exception):
    """Raised when the run should end early but still save what it has."""


class FatalError(Exception):
    """Raised when nothing useful can be done (for example a rejected key)."""


def http_json(url: str, timeout: int = 60) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "late-checkout/update-prices"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {"error": f"HTTP {e.code}"}
        return e.code, body


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    return cfg


def in_scope(h: dict, cfg: dict, watch: set[str]) -> bool:
    if h["id"] in watch:
        return True
    if cfg["programs"] and h["program"] not in cfg["programs"]:
        return False
    if cfg["countries"] and h["country"].lower() not in {c.lower() for c in cfg["countries"]}:
        return False
    if cfg["regions"] and h["region"].lower() not in {r.lower() for r in cfg["regions"]}:
        return False
    return True


def watch_ids(catalog: list[dict], cfg: dict) -> set[str]:
    """Watchlist entries may be hotel ids or (part of) a hotel name."""
    wanted = [str(w).strip().lower() for w in cfg["watchlist"] if str(w).strip()]
    out = set()
    for h in catalog:
        name = h["name"].lower()
        if any(w == h["id"] or w in name for w in wanted):
            out.add(h["id"])
    return out


def shift_to_today(entry: dict, today: dt.date, horizon: int) -> None:
    """Drop nights that are now in the past so index 0 is always today."""
    if not entry.get("r") or not entry.get("s"):
        return
    start = dt.date.fromisoformat(entry["s"])
    gone = (today - start).days
    if gone > 0:
        entry["r"] = entry["r"][gone:]
        entry["s"] = today.isoformat()
    entry["r"] = entry["r"][:horizon]


def min_rate(entry: dict) -> float | None:
    vals = [v for v in entry.get("r") or [] if v is not None]
    return min(vals) if vals else None


def plan(scope: list[dict], state: dict, watch: set[str], cfg: dict, now: float, budget: int) -> list[dict]:
    """Choose which hotels to price this run, most valuable first."""
    usable = []
    for h in scope:
        st = state.get(h["id"], {})
        if not h.get("google_token"):
            continue
        # A hotel that failed three times in a row rests for 30 days.
        if st.get("e", 0) >= 3 and now - st.get("la", 0) < 30 * 86400:
            continue
        usable.append((h, st))

    def age_h(st: dict) -> float:
        return (now - st["t"]) / 3600 if st.get("t") else float("inf")

    first = [h for h, st in usable if h["id"] in watch and age_h(st) >= cfg["watch_refresh_hours"]]
    first.sort(key=lambda h: -min(age_h(state.get(h["id"], {})), 1e9))
    picked = first[:budget]
    taken = {h["id"] for h in picked}
    left = budget - len(picked)
    if left <= 0:
        return picked

    new = [h for h, st in usable if not st.get("t") and h["id"] not in taken]
    new.sort(key=lambda h: (state.get(h["id"], {}).get("la", 0), h["id"][1:]))  # least recently tried first, then a stable shuffle

    def score(item: tuple[dict, dict]) -> float:
        h, st = item
        low = min_rate(st)
        weight = cfg["focus_weight"] if low is not None and low <= cfg["focus_max_rate"] else 1
        return age_h(st) * weight

    old = [(h, st) for h, st in usable if st.get("t") and h["id"] not in taken and age_h(st) >= 20]
    old.sort(key=score, reverse=True)
    old_hotels = [h for h, _ in old]

    n_new = min(len(new), max(left - len(old_hotels), (left + 1) // 2))
    n_old = min(len(old_hotels), left - n_new)
    n_new = min(len(new), left - n_old)
    return picked + new[:n_new] + old_hotels[:n_old]


def fetch_calendar(h: dict, cfg: dict, key: str, today: dt.date) -> tuple[list, float | None]:
    """Return (nightly rates before tax aligned to today, tax-and-fee factor)."""
    horizon = cfg["horizon_days"]
    params = {
        "engine": "google_hotels_price_calendar",
        "property_token": h["google_token"],
        "start_date": today.isoformat(),
        "end_date": (today + dt.timedelta(days=horizon - 1)).isoformat(),
        "adults": cfg["adults"], "gl": cfg["gl"], "hl": "en", "currency": cfg["currency"],
        "api_key": key,
    }
    url = f"{API}/search.json?{urllib.parse.urlencode(params)}"
    last = "no response"
    for attempt in range(2):
        try:
            status, body = http_json(url)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = f"network: {e}"
            time.sleep(3)
            continue
        if status == 401:
            raise FatalError(f"SerpApi rejected the key: {body.get('error', 'unauthorized')}")
        if status == 429:
            raise StopRun(f"SerpApi says stop: {body.get('error', 'too many requests')}")
        if status >= 500:
            last = f"HTTP {status}"
            time.sleep(3)
            continue
        err = body.get("error") or (body.get("search_metadata") or {}).get("error")
        if err and not body.get("prices"):
            low = str(err).lower()
            if "run out of searches" in low or "searches left" in low:
                raise StopRun(f"SerpApi says stop: {err}")
            raise ValueError(str(err))
        rates: list = [None] * horizon
        ratios = []
        for p in body.get("prices") or []:
            try:
                i = (dt.date.fromisoformat(p["date"]) - today).days
            except (KeyError, ValueError):
                continue
            before = p.get("extracted_before_taxes_fees")
            if before is None:
                before = p.get("extracted_display_price")
            if before is None or not (0 <= i < horizon):
                continue
            rates[i] = round(before)
            after = p.get("extracted_after_taxes_fees")
            if after and before and after >= before:
                ratios.append(after / before)
        if all(v is None for v in rates):
            raise ValueError("no prices returned")
        return rates, (round(statistics.median(ratios), 4) if ratios else None)
    raise ValueError(last)


def account(key: str) -> dict:
    try:
        status, body = http_json(f"{API}/account.json?{urllib.parse.urlencode({'api_key': key})}", timeout=30)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {}
    if status == 401:
        raise FatalError(f"SerpApi rejected the key: {body.get('error', 'unauthorized')}")
    return body if status == 200 else {}


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-searches", type=int, default=None, help="searches to spend this run")
    ap.add_argument("--dry-run", action="store_true", help="show the plan without calling the API")
    args = ap.parse_args()

    cfg = load_config()
    key = os.environ.get("SERPAPI_KEY", "").strip()
    now = time.time()
    today = dt.datetime.now(dt.timezone.utc).date()
    horizon = int(cfg["horizon_days"])

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))["hotels"]
    watch = watch_ids(catalog, cfg)
    scope = [h for h in catalog if in_scope(h, cfg, watch)]
    if cfg["max_hotels"] and len(scope) > cfg["max_hotels"]:
        keep = [h for h in scope if h["id"] in watch]
        keep += [h for h in scope if h["id"] not in watch][: max(0, cfg["max_hotels"] - len(keep))]
        scope = keep

    state: dict[str, dict] = {}
    if PRICES.exists():
        try:
            for e in json.loads(PRICES.read_text(encoding="utf-8")).get("hotels", []):
                state[e["id"]] = {k: e[k] for k in ("s", "r", "x", "t", "la", "e") if k in e}
        except (ValueError, OSError, KeyError):
            print("Could not read the existing prices file; starting fresh.", file=sys.stderr)
    for st in state.values():
        shift_to_today(st, today, horizon)

    env_max = os.environ.get("MAX_SEARCHES", "").strip()
    budget = args.max_searches if args.max_searches is not None else (int(env_max) if env_max.isdigit() else max(1, int(cfg["monthly_searches"]) // 31))
    notes = []
    left_after = None

    if not key and not args.dry_run:
        notes.append("No SERPAPI_KEY set, so no prices were fetched.")
        budget = 0
    elif key and not args.dry_run:
        try:
            acct = account(key)
        except FatalError as e:
            print(str(e), file=sys.stderr)
            return 1
        if acct:
            caps = [budget]
            if isinstance(acct.get("total_searches_left"), (int, float)):
                caps.append(int(acct["total_searches_left"]))
            if isinstance(acct.get("account_rate_limit_per_hour"), (int, float)):
                caps.append(max(0, int(acct["account_rate_limit_per_hour"]) - int(acct.get("this_hour_searches") or 0)))
            if min(caps) < budget:
                notes.append(f"Budget cut from {budget} to {min(caps)} by the account's remaining searches or hourly limit.")
            budget = min(caps)

    todo = plan(scope, state, watch, cfg, now, budget)
    priceable = sum(1 for h in scope if h.get("google_token"))
    per_day = max(1, int(cfg["monthly_searches"]) // 31)
    sweep_days = priceable / per_day
    if sweep_days > 14:
        notes.append(f"At {per_day} searches a day, refreshing all {priceable} hotels in scope takes about "
                     f"{sweep_days:.0f} days. Narrow `countries` or `regions` in config.json, or raise `monthly_searches`.")

    if args.dry_run:
        print(f"Would price {len(todo)} of {len(scope)} hotels in scope (budget {budget}):")
        for h in todo[:40]:
            print(f"  {h['program']}  {h['name']} ({h['city']}, {h['country']})")
        for n in notes:
            print("Note:", n)
        return 0

    ok = failed = 0
    streak = 0
    try:
        for h in todo:
            st = state.setdefault(h["id"], {})
            st["la"] = int(time.time())
            try:
                rates, factor = fetch_calendar(h, cfg, key, today)
            except ValueError as e:
                failed += 1
                streak += 1
                st["e"] = st.get("e", 0) + 1
                print(f"  failed: {h['name']}: {e}", file=sys.stderr)
                if streak >= 5:
                    raise StopRun("five failures in a row; stopping so the budget is not wasted")
                continue
            streak = 0
            ok += 1
            st.update({"s": today.isoformat(), "r": rates, "t": int(time.time()), "e": 0})
            if factor:
                st["x"] = factor
            time.sleep(0.4)
    except StopRun as e:
        notes.append(str(e))
    except FatalError as e:
        print(str(e), file=sys.stderr)
        return 1

    if key and (ok or failed):
        left_after = account(key).get("total_searches_left")

    out_hotels = []
    for h in scope:
        e = {"id": h["id"], "n": h["name"], "c": h["city"], "k": h["country"], "g": h["region"], "p": h["program"],
             "ca": h["credit_amount"], "ct": h["credit_type"], "b": 1 if h["breakfast"] else 0, "u": h["amex_url"]}
        st = state.get(h["id"], {})
        if st.get("t") and st.get("r") and any(v is not None for v in st["r"]):
            e.update({k: st[k] for k in ("s", "r", "x", "t") if k in st})
        for k in ("la", "e"):
            if st.get(k):
                e[k] = st[k]
        out_hotels.append(e)
    priced = sum(1 for e in out_hotels if "r" in e)

    write_json(PRICES, {
        "meta": {
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": cfg["currency"], "horizon_days": horizon, "default_tax": cfg["default_tax"],
            "source": "Best one-night price on Google Hotels, via SerpApi",
            "catalog": len(catalog), "in_scope": len(scope), "priced": priced,
            "last_run": {"fetched": ok, "failed": failed, "searches_left": left_after},
        },
        "hotels": out_hotels,
    })

    lines = [f"Priced {ok} hotels this run ({failed} failed). {priced} of {len(scope)} hotels in scope have prices."]
    if left_after is not None:
        lines.append(f"Searches left on the SerpApi account: {left_after}.")
    lines += notes
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Price update\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
