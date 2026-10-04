"""
Fetch BDO market sublist data for all items with grade > 4.

Runs entirely in GitHub Actions — no local PC required.

Output: arsha_items.json in the repository root.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone

import bdomarket


# ============================================================================
# CONFIGURATION
# ============================================================================

REGION       = bdomarket.MarketRegion.EU
API_VERSION  = bdomarket.ApiVersion.V2
LANGUAGE     = bdomarket.Locale.English

MIN_GRADE    = 4          # keep items with grade > this value
BATCH_SIZE   = 300        # API maximum
BATCH_DELAY  = 2.0        # seconds between sublist requests
MAX_RETRIES  = 3          # per batch
RETRY_DELAY  = 5.0        # seconds between retries

OUTPUT_FILE  = "arsha_items.json"

HEADERS = [
    "Id", "MinEnhance", "MaxEnhance", "BasePrice",
    "AmountListed", "TotalTrades", "PriceMin", "PriceMax",
    "LastSoldPrice", "LastSoldTime"
]


# ============================================================================
# MAIN
# ============================================================================

async def main():
    print("=" * 60)
    print("ARSHA ITEM FETCH — GitHub Actions")
    print("=" * 60)
    print(f"Region:     {REGION}")
    print(f"API:        {API_VERSION}")
    print(f"Min grade:  > {MIN_GRADE}")
    print("-" * 60)

    async with bdomarket.Market(
        region=REGION,
        apiversion=API_VERSION,
        language=LANGUAGE
    ) as market:

        # --------------------------------------------------------------------
        # 1. Fetch the full market list
        # --------------------------------------------------------------------

        print("Fetching market list...")
        market_response = await market._make_request_async(
            "GET", "GetWorldMarketList",
            params={"lang": "en"}
        )

        if not market_response.success:
            print(f"FAILED to fetch market list: {market_response.message}")
            sys.exit(1)

        market_ids = parse_market_list(market_response.content)
        print(f"Market items found: {len(market_ids)}")

        if not market_ids:
            print("No market items found. Aborting.")
            sys.exit(1)

        # --------------------------------------------------------------------
        # 2. Fetch item database for grade info
        # --------------------------------------------------------------------

        print("Fetching item database for grades...")
        db_response = await market._make_request_async(
            "GET", "util/db",
            params={"lang": "en"}
        )

        if not db_response.success:
            print(f"FAILED to fetch item DB: {db_response.message}")
            sys.exit(1)

        grade_map = parse_db_grades(db_response.content)
        print(f"Grade entries loaded: {len(grade_map)}")

        # --------------------------------------------------------------------
        # 3. Filter by grade > MIN_GRADE
        # --------------------------------------------------------------------

        filtered_ids = [
            item_id for item_id in market_ids
            if grade_map.get(item_id, 0) > MIN_GRADE
        ]

        print(f"Items with grade > {MIN_GRADE}: {len(filtered_ids)}")

        if not filtered_ids:
            print("No items passed the grade filter. Aborting.")
            sys.exit(1)

        # --------------------------------------------------------------------
        # 4. Fetch sublist data in batches
        # --------------------------------------------------------------------

        total_batches = (len(filtered_ids) + BATCH_SIZE - 1) // BATCH_SIZE
        all_rows = []

        for batch_num in range(total_batches):
            start = batch_num * BATCH_SIZE
            end   = min(start + BATCH_SIZE, len(filtered_ids))
            batch = filtered_ids[start:end]

            print(f"Batch {batch_num + 1}/{total_batches} ({len(batch)} IDs)...")

            rows = await fetch_batch_with_retry(market, batch)

            if rows is None:
                print(f"  Batch {batch_num + 1} FAILED after all retries.")
                print("  Continuing with remaining batches...")
                continue

            all_rows.extend(rows)
            print(f"  OK — {len(rows)} rows")

            if batch_num < total_batches - 1:
                await asyncio.sleep(BATCH_DELAY)

        # --------------------------------------------------------------------
        # 5. Write output
        # --------------------------------------------------------------------

        output = {
            "updated":    datetime.now(timezone.utc).isoformat(),
            "region":     str(REGION),
            "api_version": str(API_VERSION),
            "min_grade":  MIN_GRADE,
            "total_ids":  len(filtered_ids),
            "total_rows": len(all_rows),
            "headers":    HEADERS,
            "rows":       all_rows
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)

        print("-" * 60)
        print(f"DONE — {len(all_rows)} rows written to {OUTPUT_FILE}")
        print("=" * 60)


# ============================================================================
# HELPERS
# ============================================================================

def parse_market_list(content):
    """Parse GetWorldMarketList response into a list of item IDs."""

    ids = []

    # V2: parsed JSON (list of dicts)
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and "id" in item:
                try:
                    ids.append(int(item["id"]))
                except (ValueError, TypeError):
                    pass

    # V1: pipe/dash delimited string
    elif isinstance(content, str):
        for entry in content.split("|"):
            parts = entry.split("-")
            if len(parts) >= 2:
                try:
                    ids.append(int(parts[1]))
                except (ValueError, IndexError):
                    pass

    # Remove duplicates while preserving order
    return list(dict.fromkeys(ids))


def parse_db_grades(content):
    """Parse the item database into a dict of {id: grade}."""

    grades = {}

    if not isinstance(content, list):
        return grades

    for item in content:
        if not isinstance(item, dict):
            continue

        item_id = item.get("id")
        grade   = item.get("grade")

        if item_id is not None and grade is not None:
            try:
                grades[int(item_id)] = int(grade)
            except (ValueError, TypeError):
                pass

    return grades


async def fetch_batch_with_retry(market, batch):
    """Fetch one batch of sublist data with retries."""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await market.get_world_market_sub_list(
                ids=[str(i) for i in batch]
            )

            if response.success and response.content:
                rows = parse_sublist_response(response.content)
                if rows:
                    return rows
                print(f"  Attempt {attempt}: no parseable rows")
            else:
                print(f"  Attempt {attempt}: HTTP {response.status_code} — {response.message}")

        except Exception as e:
            print(f"  Attempt {attempt}: exception — {e}")

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None


def parse_sublist_response(content):
    """Parse GetWorldMarketSubList response into rows."""

    rows = []

    # V2: list of lists of dicts
    if isinstance(content, list):
        for group in content:
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

    # V1: pipe/dash delimited string
    elif isinstance(content, str):
        for entry in content.split("|"):
            parts = entry.split("-")
            if len(parts) >= 10:
                rows.append(parts[:10])

    return rows


if __name__ == "__main__":
    asyncio.run(main())
