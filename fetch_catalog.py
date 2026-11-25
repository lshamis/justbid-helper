#!/usr/bin/env python3
"""Fetch the active catalog of items from JustBid (https://www.justbid.com).

Usage examples:
  # Fetch the entire active catalog across all locations into JSON
  python3 fetch_catalog.py -o justbid_catalog.json

  # Stream to JSONL or export to CSV
  python3 fetch_catalog.py -o catalog.jsonl
  python3 fetch_catalog.py -o catalog.csv

  # List available pickup locations
  python3 fetch_catalog.py --list-locations

  # Fetch catalog for specific location(s) only (by ID or substring match)
  python3 fetch_catalog.py --location 1 --location 2 -o sacramento.json
  python3 fetch_catalog.py --location "Sacramento" -o sacramento.json

  # Test on the first 5 pages and include item detail fields (condition, ASIN, etc.)
  python3 fetch_catalog.py --max-pages 5 --with-details -o sample.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.justbid.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SORT_CHOICES = (
    "default",
    "newly_posted",
    "highest_bid",
    "lowest_bid",
    "highest_price",
    "lowest_price",
)

KNOWN_CONDITIONS = (
    "Appears New",
    "Open Box",
    "Pre-Owned",
    "Damaged",
    "Used",
    "As-Is",
)

CSV_COLUMNS = [
    "item_id",
    "listing_id",
    "sku",
    "title",
    "url",
    "current_bid",
    "next_bid",
    "bid_count",
    "retail_price",
    "discount_percent",
    "location",
    "start_timestamp",
    "end_timestamp",
    "starts_at",
    "ends_at",
    "image_url",
    "image_count",
    "condition",
    "asin",
    "pickup_address",
]


def parse_money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", text)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d[\d,]*)", text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def ts_to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class JustBidClient:
    def __init__(
        self,
        timeout: float = 20.0,
        retries: int = 3,
        delay: float = 0.0,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self._base_cookies: requests.cookies.RequestsCookieJar = (
            requests.cookies.RequestsCookieJar()
        )
        self._thread_local = threading.local()

    def _get_session(self) -> requests.Session:
        sess = getattr(self._thread_local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": DEFAULT_USER_AGENT})
            sess.cookies.update(self._base_cookies)
            self._thread_local.session = sess
        return sess

    def _request(
        self,
        method: str,
        url: str,
        *,
        allow_redirects: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            if self.delay > 0:
                time.sleep(self.delay)
            try:
                sess = self._get_session()
                resp = sess.request(
                    method,
                    url,
                    timeout=self.timeout,
                    allow_redirects=allow_redirects,
                    **kwargs,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.retries:
                        backoff = min(2.0**attempt, 15.0)
                        time.sleep(backoff)
                        continue
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(min(2.0**attempt, 15.0))
        raise RuntimeError(f"Request failed for {url}: {last_exc}") from last_exc

    def fetch_locations(self) -> list[dict[str, str]]:
        """Fetch all pickup locations from /locations."""
        resp = self._request(
            "GET",
            f"{BASE_URL}/locations",
            headers={"Accept": "text/vnd.turbo-stream.html, text/html"},
        )
        resp.raise_for_status()
        self._base_cookies.update(self._get_session().cookies)

        pattern = re.compile(
            r'maps/place/([^"]+)">([^<]+)</a>.*?name="locations\[selected\]\[(\d+)\]"',
            re.S,
        )
        locations: list[dict[str, str]] = []
        seen: set[str] = set()
        for full_address, street, loc_id in pattern.findall(resp.text):
            if loc_id in seen:
                continue
            seen.add(loc_id)
            locations.append(
                {
                    "id": loc_id,
                    "name": street.strip(),
                    "address": full_address.strip(),
                }
            )
        return locations

    def set_locations(self, selected_ids: set[str], all_locations: list[dict[str, str]]) -> None:
        """Select a subset of pickup locations in the session."""
        resp = self._request(
            "GET",
            f"{BASE_URL}/locations",
            headers={"Accept": "text/vnd.turbo-stream.html, text/html"},
        )
        resp.raise_for_status()
        token_match = re.search(
            r'name="authenticity_token" value="([^"]+)"', resp.text
        )
        if not token_match:
            raise RuntimeError("Could not find authenticity_token on /locations")

        data: dict[str, str] = {"authenticity_token": token_match.group(1)}
        for loc in all_locations:
            loc_id = loc["id"]
            data[f"locations[selected][{loc_id}]"] = (
                "true" if loc_id in selected_ids else "false"
            )

        post_resp = self._request("POST", f"{BASE_URL}/locations", data=data)
        post_resp.raise_for_status()
        self._base_cookies.update(self._get_session().cookies)

    def build_items_url(
        self,
        page: int,
        *,
        sort: str = "default",
        search: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> str:
        params: dict[str, Any] = {"page": page}
        if sort and sort != "default":
            params["sort"] = sort
        if search:
            params["search"] = search
        if min_price is not None:
            params["min_price"] = int(min_price) if min_price.is_integer() else min_price
        if max_price is not None:
            params["max_price"] = int(max_price) if max_price.is_integer() else max_price
        return f"{BASE_URL}/items?{urlencode(params)}"

    def _page_exists(self, page: int, **query_kwargs: Any) -> bool:
        url = self.build_items_url(page, **query_kwargs)
        resp = self._request("GET", url, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            return False
        if resp.status_code != 200:
            return False
        return 'class="' in resp.text and "item-card" in resp.text

    def discover_last_page(self, **query_kwargs: Any) -> int:
        """Find the exact last valid page number via exponential + binary search."""
        if not self._page_exists(1, **query_kwargs):
            return 0

        lo = 1
        hi = 2
        while self._page_exists(hi, **query_kwargs):
            lo = hi
            hi *= 2
            if hi > 100_000:
                break

        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._page_exists(mid, **query_kwargs):
                lo = mid
            else:
                hi = mid - 1
        return lo

    def fetch_page_items(self, page: int, **query_kwargs: Any) -> list[dict[str, Any]]:
        """Fetch and parse all item cards on a single /items page."""
        url = self.build_items_url(page, **query_kwargs)
        resp = self._request("GET", url, allow_redirects=False)
        if resp.status_code != 200:
            return []
        return parse_listing_page(resp.text)

    def fetch_item_details(self, item_url: str) -> dict[str, Any]:
        """Fetch an individual item page to extract condition, ASIN, and pickup address."""
        resp = self._request("GET", item_url)
        if resp.status_code != 200:
            return {}
        return parse_item_detail_page(resp.text)


def parse_listing_page(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict[str, Any]] = []

    for card in soup.select("div.item-card"):
        item_id = card.get("item-id")
        listing_id = card.get("item-listing-id") or card.get(
            "data-item-current-listing-id"
        )

        link = card.select_one('a[href^="/item/"]')
        href = link.get("href", "") if link else ""
        sku = None
        if href:
            parts = href.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "item":
                sku = parts[1]
            if not item_id and len(parts) >= 3:
                item_id = parts[2]

        title = None
        if link:
            title = link.get("title")
            if not title:
                img = link.select_one("img[alt]")
                if img:
                    title = img.get("alt")
            if not title:
                title = link.get_text(" ", strip=True)

        timer = card.select_one("[data-unix-end-timestamp]")
        start_ts = (
            int(timer["data-unix-start-timestamp"])
            if timer and timer.get("data-unix-start-timestamp")
            else None
        )
        end_ts = (
            int(timer["data-unix-end-timestamp"])
            if timer and timer.get("data-unix-end-timestamp")
            else None
        )

        bid_amount_el = (
            card.select_one(f"#item-listing-{listing_id}-bid_amount")
            if listing_id
            else None
        )
        bid_count_el = (
            card.select_one(f"#item-listing-{listing_id}-bid_count")
            if listing_id
            else None
        )
        bid_button_el = (
            card.select_one(f"#item-listing-{listing_id}-bid_button")
            if listing_id
            else None
        )
        retail_pct_el = (
            card.select_one(f"#item-listing-{listing_id}-retail_percentage")
            if listing_id
            else None
        )

        retail_price = None
        if retail_pct_el and retail_pct_el.find_previous_sibling("span"):
            retail_price = parse_money(
                retail_pct_el.find_previous_sibling("span").get_text(strip=True)
            )
        else:
            retail_match = re.search(
                r"Retail\s*\$([\d,]+(?:\.\d+)?)", card.get_text(" ", strip=True)
            )
            if retail_match:
                retail_price = float(retail_match.group(1).replace(",", ""))

        discount_pct = (
            parse_int(retail_pct_el.get_text(strip=True)) if retail_pct_el else None
        )

        location = None
        loc_img = card.select_one('img[src*="location-"]')
        if loc_img:
            loc_span = loc_img.find_next_sibling("span")
            if loc_span:
                location = loc_span.get_text(strip=True)

        images: list[str] = []
        img_array_el = card.select_one("[data-images-value]")
        if img_array_el and img_array_el.get("data-images-value"):
            images = [
                u.strip()
                for u in img_array_el["data-images-value"].split(",")
                if u.strip()
            ]
        elif link:
            img_el = link.select_one("img[src]")
            if img_el and img_el.get("src"):
                images = [img_el["src"]]

        items.append(
            {
                "item_id": item_id,
                "listing_id": listing_id,
                "sku": sku,
                "title": title,
                "url": f"{BASE_URL}{href}" if href else None,
                "current_bid": parse_money(
                    bid_amount_el.get_text(strip=True) if bid_amount_el else None
                ),
                "next_bid": parse_money(
                    bid_button_el.get_text(strip=True) if bid_button_el else None
                ),
                "bid_count": parse_int(
                    bid_count_el.get_text(strip=True) if bid_count_el else None
                ),
                "retail_price": retail_price,
                "discount_percent": discount_pct,
                "location": location,
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
                "starts_at": ts_to_iso(start_ts),
                "ends_at": ts_to_iso(end_ts),
                "image_url": images[0] if images else None,
                "images": images,
            }
        )

    return items


def parse_item_detail_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup.body
    text = main.get_text("\n", strip=True) if main else ""

    details: dict[str, Any] = {}

    cond_match = re.search(r"Condition:\s*([^\n]+)", text, re.I)
    if cond_match:
        details["condition"] = cond_match.group(1).strip()
    else:
        for cond in KNOWN_CONDITIONS:
            if re.search(rf"\b{re.escape(cond)}\b", text, re.I):
                details["condition"] = cond
                break

    asin_match = re.search(r"ASIN\s+([A-Z0-9]{10})\b", text)
    if asin_match:
        details["asin"] = asin_match.group(1)

    map_link = soup.select_one('main a[href*="google.com/maps/place/"]')
    if map_link:
        details["pickup_address"] = map_link.get_text(" ", strip=True)

    return details


def resolve_format(output_path: Path, explicit_format: str | None) -> str:
    if explicit_format:
        return explicit_format
    ext = output_path.suffix.lower()
    if ext == ".csv":
        return "csv"
    if ext in (".jsonl", ".ndjson"):
        return "jsonl"
    return "json"


def write_output(
    items: list[dict[str, Any]], output_path: Path, fmt: str
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "count": len(items),
                    "items": items,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
    elif fmt == "jsonl":
        with output_path.open("w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        with output_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for item in items:
                row = dict(item)
                row["image_count"] = len(item.get("images") or [])
                writer.writerow(row)
    else:
        raise ValueError(f"Unsupported format: {fmt}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the active catalog of items from JustBid.com."
    )
    parser.add_argument(
        "-o",
        "--output",
        default="justbid_catalog.json",
        help="Output file path (default: justbid_catalog.json)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "jsonl", "csv"),
        default=None,
        help="Output format (default: inferred from --output extension, else json)",
    )
    parser.add_argument(
        "--list-locations",
        action="store_true",
        help="List available pickup locations and exit",
    )
    parser.add_argument(
        "--location",
        action="append",
        default=None,
        help='Filter by pickup location ID or substring (repeatable; default: ", CA")',
    )
    parser.add_argument(
        "--all-locations",
        action="store_true",
        help="Include all pickup locations instead of defaulting to CA",
    )
    parser.add_argument(
        "--sort",
        choices=SORT_CHOICES,
        default="default",
        help="Sort order on JustBid (default: default)",
    )
    parser.add_argument("--search", default=None, help="Optional search query")
    parser.add_argument(
        "--min-price",
        type=float,
        default=None,
        help="Optional minimum retail price filter (default: none)",
    )
    parser.add_argument(
        "--max-price", type=float, default=None, help="Optional maximum price filter"
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit the number of catalog pages fetched (default: all active pages)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent HTTP workers (default: 8)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Optional delay in seconds before each HTTP request per worker (default: 0)",
    )
    parser.add_argument(
        "--with-details",
        action="store_true",
        help="Also fetch each item's detail page for condition, ASIN, and full pickup address (slower)",
    )
    args = parser.parse_args()

    client = JustBidClient(delay=args.delay)

    if args.list_locations:
        locations = client.fetch_locations()
        print("Available JustBid pickup locations:")
        for loc in locations:
            print(f"  ID {loc['id']:>2}: {loc['name']} — {loc['address']}")
        return 0

    location_queries = [] if args.all_locations else (args.location or [", CA"])
    if location_queries:
        all_locations = client.fetch_locations()
        selected_ids: set[str] = set()
        for query in location_queries:
            q_lower = query.strip().lower()
            matched = [
                loc["id"]
                for loc in all_locations
                if loc["id"] == q_lower
                or q_lower in loc["name"].lower()
                or q_lower in loc["address"].lower()
            ]
            if not matched:
                print(
                    f"Error: No location matched {query!r}. Run --list-locations to see options.",
                    file=sys.stderr,
                )
                return 1
            selected_ids.update(matched)
        client.set_locations(selected_ids, all_locations)
        names = [
            f"{l['id']} ({l['name']})" for l in all_locations if l["id"] in selected_ids
        ]
        print(f"Filtered to locations: {', '.join(names)}", file=sys.stderr)

    min_price = args.min_price if args.min_price and args.min_price > 0 else None
    query_kwargs = {
        "sort": args.sort,
        "search": args.search,
        "min_price": min_price,
        "max_price": args.max_price,
    }

    print("Discovering total active catalog pages...", file=sys.stderr)
    total_pages = client.discover_last_page(**query_kwargs)
    if total_pages == 0:
        print("No active items found.", file=sys.stderr)
        write_output([], Path(args.output), resolve_format(Path(args.output), args.format))
        return 0

    pages_to_fetch = (
        min(total_pages, args.max_pages) if args.max_pages else total_pages
    )
    print(
        f"Found {total_pages} active pages (~{total_pages * 40:,} items). "
        f"Fetching {pages_to_fetch} page(s) with {args.workers} workers...",
        file=sys.stderr,
    )

    page_results: dict[int, list[dict[str, Any]]] = {}
    completed = 0
    start_time = time.monotonic()

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(client.fetch_page_items, p, **query_kwargs): p
            for p in range(1, pages_to_fetch + 1)
        }
        for future in as_completed(futures):
            p = futures[future]
            try:
                page_results[p] = future.result()
            except Exception as exc:
                print(f"Warning: failed to fetch page {p}: {exc}", file=sys.stderr)
                page_results[p] = []
            completed += 1
            if completed % 50 == 0 or completed == pages_to_fetch:
                elapsed = time.monotonic() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                print(
                    f"  Pages: {completed}/{pages_to_fetch} "
                    f"({completed * 100 / pages_to_fetch:.1f}%, {rate:.1f} pages/s)",
                    file=sys.stderr,
                )

    # Flatten in page order and deduplicate by item_id
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for p in range(1, pages_to_fetch + 1):
        for item in page_results.get(p, []):
            key = item.get("item_id") or item.get("url")
            if key and key in seen_ids:
                continue
            if key:
                seen_ids.add(key)
            items.append(item)

    if args.with_details and items:
        print(
            f"Fetching item detail pages for {len(items):,} items...",
            file=sys.stderr,
        )
        detail_done = 0
        detail_start = time.monotonic()

        def _enrich(item: dict[str, Any]) -> None:
            if item.get("url"):
                item.update(client.fetch_item_details(item["url"]))

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures_d = [pool.submit(_enrich, it) for it in items]
            for fut in as_completed(futures_d):
                try:
                    fut.result()
                except Exception as exc:
                    print(f"Warning: detail fetch error: {exc}", file=sys.stderr)
                detail_done += 1
                if detail_done % 100 == 0 or detail_done == len(items):
                    elapsed = time.monotonic() - detail_start
                    rate = detail_done / elapsed if elapsed > 0 else 0
                    print(
                        f"  Details: {detail_done}/{len(items)} "
                        f"({detail_done * 100 / len(items):.1f}%, {rate:.1f} items/s)",
                        file=sys.stderr,
                    )

    output_path = Path(args.output)
    fmt = resolve_format(output_path, args.format)
    write_output(items, output_path, fmt)
    total_elapsed = time.monotonic() - start_time
    print(
        f"Saved {len(items):,} unique active items to {output_path} ({fmt}) "
        f"in {total_elapsed:.1f}s.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
