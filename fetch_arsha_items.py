"""
Fetch BDO market item details for all items with grade > 4.

Routes all Arsha API requests through the Cloudflare Worker to bypass
Imperva's bot detection, which blocks GitHub Actions' Azure IPs.

Endpoints used:
  /v2/{region}/market       — full market list (to get all item IDs)
  /v2/{region}/util/db      — item database (to get grades)
  /v2/{region}/item?id=...  — detailed item data (10 attributes + name/sid)

Output: arsha_items.json in the repository root.
"""

import json
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests


# ============================================================================
# CONFIGURATION
# ============================================================================

ARSHA_BASE_URL = "https://api.arsha.io"
WORKER_URL     = "https://mute-leaf-03b0.sephard.workers.dev"

REGION       = "eu"
API_VERSION  = "v2"
LANG         = "en"

MIN_GRADE    = 4          # keep items with grade > this value
BATCH_SIZE   = 300        # API maximum
BATCH_DELAY  = 2.0        # seconds between sublist requests
MAX_RETRIES  = 3          # per request
RETRY_DELAY  = 5.0        # seconds between retries

OUTPUT_FILE  = "arsha_items.json"

# The 10 required attributes from the item endpoint.
# Extra fields returned by the API (name, sid) are ignored.
OUTPUT_HEADERS = [
    "Id", "MinEnhance", "MaxEnhance", "BasePrice",
    "AmountListed", "TotalTrades", "PriceMin", "PriceMax",
    "LastSoldPrice", "LastSoldTime"
]


# ============================================================================
# WORKER-BASED HTTP CLIENT
# ============================================================================

def arsha_request(endpoint, params=None, timeout=120):
    """
    Fetch an Arsha API endpoint via the Cloudflare Worker.

    Uses POST with a JSON body so there is no URL-length limit.
    Returns (status_code, parsed_json_or_none).
    """
    target = f"{ARSHA_BASE_URL}/{endpoint}"
    if params:
        target += "?" + urlencode(params)

    payload = {"url": target}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                WORKER_URL,
                json=payload,
                timeout=timeout
            )

            if response.status_code == 200:
                try:
                    return response.status_code, response.json()
                except ValueError:
                    print(f"  Worker returned non-JSON: {response.text[:200]}")
                    return response.status_code, None

            print(f"  Attempt {attempt}: Worker HTTP {response.status_code}")
            if response.status_code == 500:
                print(f"    Body: {response.text[:200]}")

        except Exception as e:
            print(f"  Attempt {attempt}: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY * attempt)

    return None, None


# ============================================================================
# DATA FETCHERS
# ============================================================================

def fetch_market_list():
    """Returns a list of all item IDs currently on the market."""
    print("Fetching market list...")
    status, data = arsha_request(
        f"{API_VERSION}/{REGION}/market",
        {"lang": LANG}
    )

    if status != 200 or data is None:
        print(f"  FAILED — status {status}")
        return []

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

    return list(dict.fromkeys(ids))


def fetch_item_grades():
    """Returns dict {item_id: grade} from the Arsha item database."""
    print("Fetching item database...")
    status, data = arsha_request("util/db", {"lang": LANG})

    if status != 200 or data is None:
        print(f"  FAILED — status {status}")
        return {}

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


def fetch_items_batch(batch):
    """
    Fetch item details for one batch of IDs using the /item endpoint.

    Returns parsed JSON (list of lists of dicts) or None on failure.
    """
    ids_str = ",".join(str(i) for i in batch)
    status, data = arsha_request(
        f"{API_VERSION}/{REGION}/item",
        {"id": ids_str, "lang": LANG}
    )

    if status != 200 or data is None:
        return None

    return data


# ============================================================================
# PARSING
# ============================================================================

def parse_items(data):
    """
    Parse /item JSON into flat rows with exactly the 10 required columns.

    The /item endpoint returns:
      [[{...}, {...}], [{...}], ...]
    Each inner list contains the enhancement brackets for one requested ID.
    Extra fields (name, sid) are ignored.
    """
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
    print("ARSHA ITEM FETCH — GitHub Actions (via Cloudflare Worker)")
    print("=" * 60)
    print(f"Worker:     {WORKER_URL}")
    print(f"Region:     {REGION}")
    print(f"API:        {API_VERSION}")
    print(f"Endpoint:   /{API_VERSION}/{REGION}/item")
    print(f"Min grade:  > {MIN_GRADE}")
    print(f"Batch size: {BATCH_SIZE}")
    print("-" * 60)

    # 1. Market list
    market_ids = fetch_market_list()
    print(f"Market items found: {len(market_ids)}")
    if not market_ids:
        print("Aborting — no market items.")
        sys.exit(1)

    # 2. Grade database
    grades = fetch_item_grades()
    print(f"Grade entries loaded: {len(grades)}")

    # 3. Filter by grade
    filtered = [i for i in market_ids if grades.get(i, 0) > MIN_GRADE]
    print(f"Items with grade > {MIN_GRADE}: {len(filtered)}")
    if not filtered:
        print("Aborting — nothing passed the filter.")
        sys.exit(1)

    # 4. Fetch item details in batches
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

        data = fetch_items_batch(batch)

        if data is None:
            print(f"  FAILED after {MAX_RETRIES} retries.")
            failed_batches += 1
            continue

        rows = parse_items(data)
        print(f"  OK — {len(rows)} rows")
        all_rows.extend(rows)

        if b < total_batches - 1:
            time.sleep(BATCH_DELAY)

    # 5. Write output
    output = {
        "updated":        datetime.now(timezone.utc).isoformat(),
        "region":         REGION,
        "api_version":    API_VERSION,
        "endpoint":       f"/{API_VERSION}/{REGION}/item",
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
