"""
Fetch BDO market sublist data for all items with grade > 4.

Runs entirely in GitHub Actions — no local PC required.

Uses requests directly instead of the bdomarket library
(the published PyPI version has syntax errors incompatible with Python < 3.12).

Output: arsha_items.json in the repository root.
"""

import json
import sys
import time
from datetime import datetime, timezone

import requests


# ============================================================================
# CONFIGURATION
# ============================================================================

BASE_URL     = "https://api.arsha.io"
REGION       = "eu"
API_VERSION  = "v2"
LANG         = "en"

MIN_GRADE    = 4          # keep items with grade > this value
BATCH_SIZE   = 300        # API maximum
BATCH_DELAY  = 2.0        # seconds between sublist requests
MAX_RETRIES  = 3          # per batch
RETRY_DELAY  = 5.0        # seconds between retries

OUTPUT_FILE  = "arsha_items.json"

HTTP_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent":   "BlackDesert"      # REQUIRED — bypasses Imperva
}

OUTPUT_HEADERS = [
    "Id", "MinEnhance", "MaxEnhance", "BasePrice",
    "AmountListed", "TotalTrades", "PriceMin", "PriceMax",
    "LastSoldPrice", "LastSoldTime"
]


# ============================================================================
# HTTP HELPERS
# ============================================================================

def api_get(endpoint, params=None, timeout=60):
    """GET request against the Arsha API with proper headers."""
    url = f"{BASE_URL}/{endpoint}"
    response = requests.get(
        url,
        params=params,
        headers=HTTP_HEADERS,
        timeout=timeout
    )
    response.raise_for_status()
    return response.json()


# ============================================================================
# DATA FETCHERS
# ============================================================================

def fetch_market_list():
    """Returns a list of all item IDs currently on the market."""
    print("Fetching market list...")
    data = api_get(
        f"{API_VERSION}/{REGION}/GetWorldMarketList",
        {"lang": LANG}
    )

    ids = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                item_id = item.get("id")
                if item_id is not None:
                    try:
                        ids.append(int(item_id))
                    except (ValueError, TypeError):
                        pass

    # Deduplicate, preserve order
    return list(dict.fromkeys(ids))


def fetch_item_grades():
    """Returns dict {item_id: grade} from the Arsha item database."""
    print("Fetching item database...")
    data = api_get("util/db", {"lang": LANG})

    grades = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            grade   = item.get("grade")
            if item_id is None or grade is None:
                continue
            try:
                grades[int(item_id)] = int(grade)
            except (ValueError, TypeError):
                pass

    return grades


def fetch_sublist_batch(batch):
    """Fetch one batch of sublist data. Returns parsed JSON or None on failure."""
    ids_str = ",".join(str(i) for i in batch)

    endpoint = f"{API_VERSION}/{REGION}/GetWorldMarketSubList"
    url      = f"{BASE_URL}/{endpoint}"
    params   = {"id": ids_str, "lang": LANG}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=HTTP_HEADERS,
                timeout=60
            )

            if response.status_code == 200:
                return response.json()

            print(f"  Attempt {attempt}: HTTP {response.status_code}")
            if response.status_code == 500:
                print(f"    Body: {response.text[:150]}")

        except Exception as e:
            print(f"  Attempt {attempt}: exception — {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY * attempt)

    return None


# ============================================================================
# PARSING
# ============================================================================

def parse_sublist(data):
    """Parse GetWorldMarketSubList JSON into flat rows."""
    rows = []

    if not isinstance(data, list):
        return rows

    for group in data:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            rows.append([
                item.get("id", ""),
                item.get("minEnhance", ""),
                item.get("maxEnhance", ""),
                item.get("basePrice", ""),
                item.get("currentStock", ""),
                item.get("totalTrades", ""),
                item.get("priceMin", ""),
                item.get("priceMax", ""),
                item.get("lastSoldPrice", ""),
                item.get("lastSoldTime", "")
            ])

    return rows


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print("ARSHA ITEM FETCH — GitHub Actions")
    print("=" * 60)
    print(f"Region:     {REGION}")
    print(f"API:        {API_VERSION}")
    print(f"Min grade:  > {MIN_GRADE}")
    print(f"Batch size: {BATCH_SIZE}")
    print("-" * 60)

    # 1. Market list
    market_ids = fetch_market_list()
    print(f"Market items found: {len(market_ids)}")
    if not market_ids:
        print("No market items — aborting.")
        sys.exit(1)

    # 2. Grade database
    grades = fetch_item_grades()
    print(f"Grade entries loaded: {len(grades)}")

    # 3. Filter by grade
    filtered = [i for i in market_ids if grades.get(i, 0) > MIN_GRADE]
    print(f"Items with grade > {MIN_GRADE}: {len(filtered)}")
    if not filtered:
        print("No items passed the filter — aborting.")
        sys.exit(1)

    # 4. Fetch sublists in batches
    total_batches = (len(filtered) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Total batches: {total_batches}")
    print("-" * 60)

    all_rows = []
    failed_batches = 0

    for b in range(total_batches):
        start = b * BATCH_SIZE
        end   = min(start + BATCH_SIZE, len(filtered))
        batch = filtered[start:end]

        print(f"Batch {b + 1}/{total_batches} ({len(batch)} IDs)...")

        data = fetch_sublist_batch(batch)

        if data is None:
            print(f"  FAILED after {MAX_RETRIES} retries.")
            failed_batches += 1
            continue

        rows = parse_sublist(data)
        print(f"  OK — {len(rows)} rows")
        all_rows.extend(rows)

        if b < total_batches - 1:
            time.sleep(BATCH_DELAY)

    # 5. Write output
    output = {
        "updated":        datetime.now(timezone.utc).isoformat(),
        "region":         REGION,
        "api_version":    API_VERSION,
        "min_grade":      MIN_GRADE,
        "total_ids":      len(filtered),
        "total_batches":  total_batches,
        "failed_batches": failed_batches,
        "total_rows":     len(all_rows),
        "headers":        OUTPUT_HEADERS,
        "rows":           all_rows
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print("-" * 60)
    print(f"DONE — {len(all_rows)} rows in {OUTPUT_FILE}")
    print(f"Failed batches: {failed_batches}")
    print("=" * 60)

    if not all_rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
