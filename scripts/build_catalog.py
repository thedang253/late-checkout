#!/usr/bin/env python3
"""Build data/catalog.json: the list of Fine Hotels + Resorts and Hotel Collection hotels.

Source: the FHR/THC property files in the MIT-licensed travel-hacking-toolkit
repository (github.com/borski/travel-hacking-toolkit), which are themselves
generated from the US Card Guide community map. See NOTICE.md.

Each hotel in the source carries a Google Hotels link. The token at the end of
that link is what the price updater sends to the price API, so no search is
spent on finding hotels.

Usage:
  python3 scripts/build_catalog.py                 # download and rebuild
  python3 scripts/build_catalog.py --from-dir DIR  # read fhr-properties.json / thc-properties.json from DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "catalog.json"
BASE = "https://raw.githubusercontent.com/borski/travel-hacking-toolkit/main/data/"
FILES = ["fhr-properties.json", "thc-properties.json"]

SUFFIX_COUNTRY = {"US": "United States", "CA": "Canada", "MX": "Mexico"}
RENAME = {"England": "United Kingdom", "Scotland": "United Kingdom", "Wales": "United Kingdom",
          "The Bahamas": "Bahamas", "Antigua": "Antigua and Barbuda"}

REGIONS = {
    "United States and Canada": ["United States", "Canada", "Puerto Rico", "U.S. Virgin Islands", "Guam"],
    "Mexico and Caribbean": ["Mexico", "Bahamas", "Dominican Republic", "Saint Barthelemy", "Turks and Caicos Islands",
                             "Anguilla", "Saint Lucia", "Jamaica", "Cayman Islands", "Bermuda", "Antigua and Barbuda",
                             "Saint Kitts and Nevis", "Grenada", "Aruba", "Curacao", "British Virgin Islands", "Barbados",
                             "Dominica", "Saint Martin", "Saint Vincent and The Grenadines"],
    "Central and South America": ["Brazil", "Costa Rica", "Argentina", "Colombia", "Peru", "Chile", "Panama", "Ecuador",
                                  "Uruguay", "Belize", "Honduras", "Nicaragua", "Guatemala"],
    "Europe": ["Italy", "France", "Spain", "United Kingdom", "Greece", "Switzerland", "Portugal", "Germany", "Turkey",
               "Austria", "Netherlands", "Ireland", "Croatia", "Hungary", "Czech Republic", "Sweden", "Belgium",
               "Denmark", "Monaco", "Norway", "Finland", "Malta", "Poland", "Cyprus", "Montenegro", "Iceland",
               "Romania", "Slovenia", "Bulgaria", "Serbia", "Liechtenstein", "Azerbaijan"],
    "Middle East and Africa": ["United Arab Emirates", "Morocco", "Israel", "Qatar", "South Africa", "Egypt",
                               "Saudi Arabia", "Seychelles", "Oman", "Mauritius", "Tanzania", "Kenya", "Bahrain",
                               "Kuwait", "Jordan", "Rwanda", "Tunisia", "Mozambique", "Zambia"],
    "Asia": ["China", "Japan", "Thailand", "India", "Indonesia", "Singapore", "Maldives", "Vietnam", "South Korea",
             "Malaysia", "Taiwan", "Cambodia", "Philippines", "Sri Lanka", "Bhutan", "Laos", "Mongolia", "Kazakhstan",
             "Hong Kong", "Macau"],
    "Australia and Pacific": ["Australia", "New Zealand", "French Polynesia", "Fiji"],
}
COUNTRY_REGION = {c: r for r, cs in REGIONS.items() for c in cs}


def demojibake(s: str) -> str:
    """Repair text that was UTF-8 but decoded as Latin-1 (e.g. 'QuÃ©bec')."""
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def words(slug: str) -> str:
    return demojibake(unquote(slug)).replace("-", " ").strip()


def place_from_url(url: str) -> tuple[str, str, str]:
    """Return (city, state, country) from an Amex property link."""
    m = re.search(r"/property/([^/?#]+)/([^/?#]+)/[^/?#]+", url or "")
    if not m:
        return "", "", ""
    area, city = demojibake(unquote(m.group(1))), words(m.group(2))
    state = ""
    sm = re.match(r"^(.*)-([A-Z]{2})$", area)
    if sm and sm.group(2) in SUFFIX_COUNTRY:
        state, country = sm.group(1).replace("-", " "), SUFFIX_COUNTRY[sm.group(2)]
    else:
        country = area.replace("-", " ")
    country = RENAME.get(country, country)
    return city, state, country


def credit_from_text(text: str) -> tuple[int, str]:
    t = (text or "").lower()
    m = re.search(r"\$\s*(\d{2,4})", t)
    amount = int(m.group(1)) if m else 100
    if "spa" in t and "property" not in t and "food" not in t:
        kind = "Spa"
    elif ("food" in t or "beverage" in t) and "property" not in t:
        kind = "Food and beverage"
    else:
        kind = "Property"
    return amount, kind


def load_source(from_dir: str | None) -> list[dict]:
    out: list[dict] = []
    for name in FILES:
        if from_dir:
            raw = (Path(from_dir) / name).read_text(encoding="utf-8")
        else:
            req = urllib.request.Request(BASE + name, headers={"User-Agent": "late-checkout/build-catalog"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
        out.extend(json.loads(raw).get("properties", []))
    return out


def build(props: list[dict]) -> list[dict]:
    hotels, seen = [], set()
    for p in props:
        program = (p.get("program") or "").upper()
        name = (p.get("name") or "").strip()
        if program not in ("FHR", "THC") or not name:
            continue
        url = p.get("amex_reservation") or ""
        hid = program[0].lower() + hashlib.sha1((url or name).encode("utf-8")).hexdigest()[:8]
        if hid in seen:
            continue
        seen.add(hid)
        city, state, country = place_from_url(url)
        tok = re.search(r"/entity/([A-Za-z0-9_\-]+)", p.get("price_calendar") or "")
        amount, kind = credit_from_text(p.get("credit") or "")
        breakfast = "breakfast for two" in ((p.get("benefits") or {}).get("free_breakfast") or "").lower()
        coords = p.get("coordinates") or {}
        hotels.append({
            "id": hid, "name": name, "program": program,
            "city": city, "state": state, "country": country,
            "region": COUNTRY_REGION.get(country, "Other"),
            "credit_amount": amount, "credit_type": kind, "breakfast": breakfast,
            "amex_url": url, "google_token": tok.group(1) if tok else None,
            "lat": coords.get("lat"), "lng": coords.get("lng"),
        })
    hotels.sort(key=lambda h: (h["country"], h["city"], h["name"]))
    return hotels


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-dir", help="read the source files from a local folder instead of downloading")
    args = ap.parse_args()

    hotels = build(load_source(args.from_dir))
    previous = 0
    if OUT.exists():
        try:
            previous = len(json.loads(OUT.read_text(encoding="utf-8")).get("hotels", []))
        except (ValueError, OSError):
            previous = 0
    if len(hotels) < 500 or (previous and len(hotels) < previous * 0.7):
        print(f"Refusing to replace the hotel list: got {len(hotels)} hotels, had {previous}. "
              "The source may be broken; keeping the current list.", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(OUT.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"source": "github.com/borski/travel-hacking-toolkit (MIT), from the US Card Guide map",
                   "hotels": hotels}, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    no_token = sum(1 for h in hotels if not h["google_token"])
    other = sorted({h["country"] for h in hotels if h["region"] == "Other"})
    print(f"Wrote {len(hotels)} hotels ({sum(h['program'] == 'FHR' for h in hotels)} FHR, "
          f"{sum(h['program'] == 'THC' for h in hotels)} THC); {no_token} without a Google Hotels link.")
    if other:
        print("Countries without a region:", ", ".join(c or "(blank)" for c in other))
    return 0


if __name__ == "__main__":
    sys.exit(main())
