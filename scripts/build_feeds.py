#!/usr/bin/env python3
import csv
import datetime
import email.utils
import json
import math
import pathlib
import subprocess
import urllib.request
import xml.dom.minidom
import xml.etree.ElementTree as ET

DATA_DIR = pathlib.Path("data")
DIST_DIR = pathlib.Path("dist")

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)


def to_rfc2822(date_str: str) -> str:
    """Convert ISO 8601 date string to RFC 2822 format required by RSS 2.0."""
    dt = datetime.datetime.fromisoformat(date_str.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return email.utils.format_datetime(dt)


def format_duration(seconds_float: float) -> str:
    """Format duration in seconds to HH:MM:SS or MM:SS for iTunes RSS."""
    total_seconds = max(1, math.ceil(seconds_float))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def fetch_byte_size(url: str) -> str:
    """Fetch Content-Length via HTTP HEAD (with Range GET fallback)."""
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "PodcastRSSBuilder/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            length = resp.headers.get("Content-Length")
            if length:
                return length
    except Exception:
        pass

    req_get = urllib.request.Request(
        url, headers={"User-Agent": "PodcastRSSBuilder/1.0", "Range": "bytes=0-0"}
    )
    with urllib.request.urlopen(req_get, timeout=15) as resp:
        content_range = resp.headers.get("Content-Range", "")
        if "/" in content_range:
            return content_range.split("/")[-1]
        return resp.headers.get("Content-Length", "0")


def fetch_duration(url: str) -> str:
    """Probe remote audio header with ffprobe to extract duration without full download."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-user_agent",
        "PodcastRSSBuilder/1.0",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=30
        )
        raw_val = result.stdout.strip()
        if raw_val and raw_val != "N/A":
            return format_duration(float(raw_val))
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffprobe is not installed. Ensure ffmpeg is available in your build environment."
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to probe duration for {url}: {exc}") from exc

    raise RuntimeError(f"ffprobe returned no duration for {url}")


def add_sub(
    parent: ET.Element,
    tag: str,
    text: str | None = None,
    attrib: dict | None = None,
) -> ET.Element:
    elem = ET.SubElement(parent, tag, attrib or {})
    if text is not None:
        elem.text = str(text)
    return elem


def build_podcast(config_path: pathlib.Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    csv_path = config_path.with_suffix(".csv")
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing CSV file: {csv_path}")

    rss = ET.Element("rss", {"version": "2.0"})
    channel = add_sub(rss, "channel")

    # Standard RSS 2.0 channel tags
    add_sub(channel, "title", config["title"])
    add_sub(channel, "link", config["link"])
    add_sub(channel, "description", config["description"])
    add_sub(channel, "language", config.get("language", "en-us"))
    add_sub(
        channel,
        "lastBuildDate",
        email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc)),
    )
    add_sub(
        channel,
        f"{{{ATOM_NS}}}link",
        attrib={
            "href": config["feed_url"],
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    # Apple Podcasts / iTunes channel tags
    add_sub(channel, f"{{{ITUNES_NS}}}author", config["author"])
    add_sub(channel, f"{{{ITUNES_NS}}}summary", config["description"])
    add_sub(
        channel,
        f"{{{ITUNES_NS}}}explicit",
        str(config.get("explicit", "false")).lower(),
    )
    add_sub(channel, f"{{{ITUNES_NS}}}image", attrib={"href": config["image_url"]})

    owner = add_sub(channel, f"{{{ITUNES_NS}}}owner")
    add_sub(owner, f"{{{ITUNES_NS}}}name", config["owner_name"])
    add_sub(owner, f"{{{ITUNES_NS}}}email", config["owner_email"])

    cat = add_sub(
        channel, f"{{{ITUNES_NS}}}category", attrib={"text": config["category"]}
    )
    if config.get("subcategory"):
        add_sub(
            cat, f"{{{ITUNES_NS}}}category", attrib={"text": config["subcategory"]}
        )

    # Read episodes from CSV and sort newest first
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    rows.sort(
        key=lambda r: datetime.datetime.fromisoformat(r["pub_date"].strip()),
        reverse=True,
    )

    for row in rows:
        item = add_sub(channel, "item")
        audio_url = row["audio_url"].strip()
        guid = row["guid"].strip()

        add_sub(item, "title", row["title"].strip())
        add_sub(item, "guid", guid, {"isPermaLink": "false"})
        add_sub(item, "pubDate", to_rfc2822(row["pub_date"]))
        add_sub(item, "description", row["description"].strip())
        add_sub(item, f"{{{CONTENT_NS}}}encoded", row["description"].strip())

        byte_size = row.get("byte_size", "").strip()
        if not byte_size:
            print(f"[{config['slug']}] Fetching byte size for {guid}...")
            byte_size = fetch_byte_size(audio_url)

        duration = row.get("duration", "").strip()
        if not duration:
            print(f"[{config['slug']}] Probing audio duration for {guid}...")
            duration = fetch_duration(audio_url)

        add_sub(
            item,
            "enclosure",
            attrib={
                "url": audio_url,
                "length": byte_size,
                "type": row.get("mime_type", "audio/mpeg").strip() or "audio/mpeg",
            },
        )

        add_sub(item, f"{{{ITUNES_NS}}}duration", duration)
        add_sub(
            item,
            f"{{{ITUNES_NS}}}explicit",
            row.get("explicit", "false").strip().lower() or "false",
        )

        if row.get("episode_num", "").strip():
            add_sub(item, f"{{{ITUNES_NS}}}episode", row["episode_num"].strip())
        if row.get("season_num", "").strip():
            add_sub(item, f"{{{ITUNES_NS}}}season", row["season_num"].strip())

    # Pretty-print XML and write to dist/<slug>/feed.xml
    raw_xml = ET.tostring(rss, encoding="utf-8")
    pretty_xml = xml.dom.minidom.parseString(raw_xml).toprettyxml(
        indent="  ", encoding="utf-8"
    )

    out_dir = DIST_DIR / config["slug"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "feed.xml"
    out_file.write_bytes(pretty_xml)
    print(f"Generated {out_file} ({len(rows)} episodes)")


def write_cloudflare_headers() -> None:
    headers_content = (
        "/*.xml\n"
        "  Content-Type: application/xml; charset=utf-8\n"
        "  Cache-Control: public, max-age=300\n"
    )
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    (DIST_DIR / "_headers").write_text(headers_content, encoding="utf-8")


def main() -> None:
    configs = sorted(DATA_DIR.glob("*.json"))
    if not configs:
        raise RuntimeError(f"No .json podcast configs found in {DATA_DIR}")

    for cfg in configs:
        build_podcast(cfg)

    write_cloudflare_headers()


if __name__ == "__main__":
    main()
