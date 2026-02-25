"""
events_forge.py — Daily OpenClaw event discovery.

Sources: Eventbrite and Luma only.

Restricting to dedicated event platforms (rather than news/social feeds)
eliminates false positives — everything these platforms return is a
genuine event listing, not a news article, blog post, or sponsored content.

Extraction uses schema.org Event JSON-LD, which both platforms embed for SEO.
No LLM, no paid APIs.
"""

import requests
import re
import os
import json
import time
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse, urljoin
from supabase import create_client, Client as SupabaseClient
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(), override=True)

SUPABASE_URL         = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
_supabase: "SupabaseClient | None" = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    print("⚠️  SUPABASE credentials not set — DB writes disabled.")

KEYWORD = "openclaw"

# Browser-like headers to reduce bot-detection blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Eventbrite: search for "openclaw" on virtual and general listings
EVENTBRITE_SEARCHES = [
    "https://www.eventbrite.com/d/online/openclaw/",
    "https://www.eventbrite.com/d/united-states/openclaw/",
    "https://www.eventbrite.com/d/canada/openclaw/",
    "https://www.eventbrite.com/d/united-kingdom/openclaw/",
]

# Luma: attempt their search page (may be JS-rendered; handled gracefully)
LUMA_SEARCHES = [
    "https://lu.ma/search?q=openclaw",
]

EVENT_SCHEMA_TYPES = {
    "Event", "MusicEvent", "EducationEvent", "SocialEvent",
    "BusinessEvent", "Hackathon", "ExhibitionEvent", "CourseInstance",
}


# ---------------------------------------------------------------------------
# HTML fetch
# ---------------------------------------------------------------------------

def fetch_html(url: str, timeout: int = 12) -> tuple[BeautifulSoup | None, str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser"), resp.text
        print(f"  ⚠️  HTTP {resp.status_code} for {url}")
    except Exception as ex:
        print(f"  ⚠️  Fetch error for {url}: {ex}")
    return None, ""


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------

def extract_json_ld(soup: BeautifulSoup) -> list:
    blocks = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            blocks.append(json.loads(script.string or ""))
        except Exception:
            pass
    return blocks


def find_event_schemas(blocks: list) -> list:
    """Recursively pull out schema.org Event objects from JSON-LD."""
    events = []
    for block in blocks:
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict) and item.get("@type") in EVENT_SCHEMA_TYPES:
                    events.append(item)
        elif isinstance(block, dict):
            if block.get("@type") in EVENT_SCHEMA_TYPES:
                events.append(block)
            # ItemList (Eventbrite search results embed events this way)
            for elem in block.get("itemListElement", []):
                if isinstance(elem, dict):
                    inner = elem.get("item", elem)
                    if isinstance(inner, dict) and inner.get("@type") in EVENT_SCHEMA_TYPES:
                        events.append(inner)
            # @graph (used by some CMS platforms)
            for node in block.get("@graph", []):
                if isinstance(node, dict) and node.get("@type") in EVENT_SCHEMA_TYPES:
                    events.append(node)
    return events


# ---------------------------------------------------------------------------
# Structured data parsing
# ---------------------------------------------------------------------------

def parse_iso_date(raw: str) -> str:
    """ISO 8601 → MM/DD/YYYY."""
    if not raw:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt[:len(fmt)]).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return ""


def detect_event_type(schema: dict) -> str:
    mode = str(schema.get("eventAttendanceMode", "")).lower()
    if "online" in mode:
        return "virtual"
    if "offline" in mode or "inperson" in mode:
        return "in-person"
    loc = schema.get("location", {})
    if isinstance(loc, dict):
        if loc.get("@type") == "VirtualLocation":
            return "virtual"
        if loc.get("@type") == "Place":
            return "in-person"
    return "unknown"


def extract_location(schema: dict) -> tuple[str, str, str]:
    loc = schema.get("location", {})
    if isinstance(loc, dict) and loc.get("@type") == "Place":
        addr = loc.get("address", {})
        if isinstance(addr, dict):
            return (
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
                addr.get("addressCountry", ""),
            )
        if isinstance(addr, str) and addr:
            parts = [p.strip() for p in addr.split(",")]
            return (
                parts[0] if len(parts) > 0 else "",
                parts[1] if len(parts) > 1 else "",
                parts[2] if len(parts) > 2 else "",
            )
    return "", "", ""


def clean_text(raw: str, max_sentences: int = 3) -> str:
    text = BeautifulSoup(raw or "", "html.parser").get_text(separator=" ", strip=True)
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:max_sentences]).strip()


def schema_to_event(schema: dict, fallback_url: str) -> dict | None:
    title = schema.get("name", "").replace("\n", " ").strip()
    if not title:
        return None

    url = schema.get("url", fallback_url) or fallback_url
    if not url:
        return None

    start_date = parse_iso_date(schema.get("startDate", ""))
    end_date   = parse_iso_date(schema.get("endDate", "")) or start_date

    event_type = detect_event_type(schema)
    city, state, country = ("", "", "") if event_type == "virtual" else extract_location(schema)

    org = schema.get("organizer", {})
    if isinstance(org, dict):
        organizer = org.get("name", "")
    elif isinstance(org, str):
        organizer = org
    else:
        organizer = ""
    if not organizer:
        try:
            organizer = urlparse(url).netloc.lstrip("www.").split(".")[0].capitalize()
        except Exception:
            organizer = ""

    description = clean_text(schema.get("description", ""))

    return {
        "url":              url,
        "title":            title,
        "organizer":        organizer,
        "event_type":       event_type,
        "location_city":    city,
        "location_state":   state,
        "location_country": country,
        "start_date":       start_date,
        "end_date":         end_date,
        "description":      description,
    }


# ---------------------------------------------------------------------------
# Platform scanners
# ---------------------------------------------------------------------------

def scan_eventbrite() -> list[dict]:
    """
    Fetch Eventbrite keyword search pages.
    Primary: extract Event schema from the search results page JSON-LD.
    Fallback: follow individual /e/ event links and extract from detail pages.
    """
    found = []
    for search_url in EVENTBRITE_SEARCHES:
        print(f"  📅 Eventbrite: {search_url}")
        soup, _ = fetch_html(search_url)
        if not soup:
            time.sleep(2)
            continue

        schemas = find_event_schemas(extract_json_ld(soup))
        if schemas:
            print(f"     {len(schemas)} event schema(s) on search page.")
            for s in schemas:
                e = schema_to_event(s, search_url)
                if e:
                    found.append(e)
        else:
            # No JSON-LD on search page — collect individual event page links
            event_links: set[str] = set()
            for a in soup.find_all("a", href=True):
                href = str(a["href"])
                # Eventbrite event URLs contain /e/ followed by a slug
                if re.search(r"eventbrite\.com/e/", href):
                    clean = href.split("?")[0].split("#")[0]
                    if not clean.startswith("http"):
                        clean = urljoin("https://www.eventbrite.com", clean)
                    event_links.add(clean)

            print(f"     No JSON-LD on search page; visiting {len(event_links)} event link(s).")
            for link in list(event_links)[:10]:
                time.sleep(1.5)
                esoup, _ = fetch_html(link)
                if not esoup:
                    continue
                for s in find_event_schemas(extract_json_ld(esoup)):
                    e = schema_to_event(s, link)
                    if e:
                        found.append(e)

        time.sleep(2)
    return found


def scan_luma() -> list[dict]:
    """
    Fetch Luma search page for OpenClaw events.
    Luma is often JS-rendered; this extracts whatever is available server-side.
    Falls back to Next.js __NEXT_DATA__ if present.
    """
    found = []
    for search_url in LUMA_SEARCHES:
        print(f"  📅 Luma: {search_url}")
        soup, raw = fetch_html(search_url)
        if not soup:
            time.sleep(2)
            continue

        # Try standard JSON-LD first
        schemas = find_event_schemas(extract_json_ld(soup))
        if schemas:
            print(f"     {len(schemas)} event schema(s) found.")
            for s in schemas:
                e = schema_to_event(s, search_url)
                if e:
                    found.append(e)
        else:
            # Try extracting event URLs from Next.js data (Luma uses Next.js)
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.DOTALL)
            if m:
                try:
                    next_data = json.loads(m.group(1))
                    # Walk the tree looking for event-like objects with a url/name/startAt
                    event_urls: set[str] = set()
                    raw_str = json.dumps(next_data)
                    # lu.ma URLs in the data
                    for match in re.finditer(r'"url"\s*:\s*"(https://lu\.ma/[^"]+)"', raw_str):
                        event_urls.add(match.group(1))
                    print(f"     Next.js data found; visiting {len(event_urls)} Luma event link(s).")
                    for link in list(event_urls)[:10]:
                        time.sleep(1.5)
                        esoup, _ = fetch_html(link)
                        if not esoup:
                            continue
                        for s in find_event_schemas(extract_json_ld(esoup)):
                            e = schema_to_event(s, link)
                            if e:
                                found.append(e)
                except Exception as ex:
                    print(f"     Could not parse Next.js data: {ex}")
            else:
                print(f"     No usable data from Luma (likely fully JS-rendered).")

        time.sleep(2)
    return found


# ---------------------------------------------------------------------------
# Supabase I/O
# ---------------------------------------------------------------------------

def load_existing_urls() -> set[str]:
    if not _supabase:
        return set()
    try:
        resp = _supabase.table("events").select("url").execute()
        return {r["url"] for r in (resp.data or [])}
    except Exception as ex:
        print(f"  ⚠️  Could not load existing events: {ex}")
        return set()


def save_events(events: list[dict]) -> None:
    if not _supabase or not events:
        return
    try:
        records = [{
            "url":              e["url"],
            "title":            e["title"],
            "organizer":        e.get("organizer", ""),
            "event_type":       e.get("event_type", "unknown"),
            "location_city":    e.get("location_city", ""),
            "location_state":   e.get("location_state", ""),
            "location_country": e.get("location_country", ""),
            "start_date":       e.get("start_date", ""),
            "end_date":         e.get("end_date", ""),
            "description":      e.get("description", ""),
        } for e in events]
        _supabase.table("events").upsert(records).execute()
        print(f"✅ Upserted {len(records)} event(s).")
    except Exception as ex:
        print(f"❌ Event save failed: {ex}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("🗓️  Events Forge — scanning Eventbrite & Luma for OpenClaw events...")

    existing_urls = load_existing_urls()

    raw_events: list[dict] = scan_eventbrite() + scan_luma()

    # Deduplicate by URL
    seen: set[str] = set()
    unique_events: list[dict] = []
    for e in raw_events:
        if e["url"] not in seen:
            seen.add(e["url"])
            unique_events.append(e)

    new_events = [e for e in unique_events if e["url"] not in existing_urls]
    print(f"🔍 {len(unique_events)} unique event(s) found, {len(new_events)} new.")

    if new_events:
        for e in new_events:
            print(f"  ✅ {e['title'][:60]} [{e['event_type']}] {e['start_date']}")
        save_events(new_events)
    else:
        print("ℹ️  No new events found.")

    print("✅ Events forge complete.")
