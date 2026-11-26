#!/usr/bin/env python3
import csv
import datetime
import email.utils
import json
import pathlib
import shutil
import urllib.parse
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

    # Fallback if storage host blocks HEAD requests
    req_get = urllib.request.Request(
        url, headers={"User-Agent": "PodcastRSSBuilder/1.0", "Range": "bytes=0-0"}
    )
    with urllib.request.urlopen(req_get, timeout=15) as resp:
        content_range = resp.headers.get("Content-Range", "")
        if "/" in content_range:
            return content_range.split("/")[-1]
        return resp.headers.get("Content-Length", "0")


def resolve_cover_source(config: dict, config_path: pathlib.Path) -> pathlib.Path:
    """Locate the local cover art file specified in config or matching the slug."""
    cover_entry = config.get("cover_file", "").strip()
    if cover_entry:
        candidate = pathlib.Path(cover_entry)
        if candidate.exists():
            return candidate
        in_data = DATA_DIR / cover_entry
        if in_data.exists():
            return in_data
        raise FileNotFoundError(f"Cover art not found: {cover_entry}")

    # Auto-detect data/<slug>.(jpg|jpeg|png) if cover_file is omitted
    for ext in (".jpg", ".jpeg", ".png"):
        auto_path = config_path.with_suffix(ext)
        if auto_path.exists():
            return auto_path

    raise FileNotFoundError(
        f"No cover art found for {config['slug']}. Set 'cover_file' in {config_path.name}."
    )


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

    out_dir = DIST_DIR / config["slug"]
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy cover art photo into dist/<slug>/
    cover_src = resolve_cover_source(config, config_path)
    cover_dest = out_dir / cover_src.name
    shutil.copy2(cover_src, cover_dest)

    # Derive public image_url from feed_url base if not explicitly overridden
    image_url = config.get("image_url", "").strip()
    if not image_url:
        image_url = urllib.parse.urljoin(config["feed_url"], cover_src.name)

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
    add_sub(channel, f"{{{ITUNES_NS}}}image", attrib={"href": image_url})

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
        add_sub(item, "title", row["title"].strip())
        add_sub(item, "guid", row["guid"].strip(), {"isPermaLink": "false"})
        add_sub(item, "pubDate", to_rfc2822(row["pub_date"]))
        add_sub(item, "description", row["description"].strip())
        add_sub(item, f"{{{CONTENT_NS}}}encoded", row["description"].strip())

        byte_size = row.get("byte_size", "").strip()
        if not byte_size:
            print(f"[{config['slug']}] Fetching byte size for {row['guid']}...")
            byte_size = fetch_byte_size(row["audio_url"].strip())

        add_sub(
            item,
            "enclosure",
            attrib={
                "url": row["audio_url"].strip(),
                "length": byte_size,
                "type": row.get("mime_type", "audio/mpeg").strip() or "audio/mpeg",
            },
        )

        add_sub(item, f"{{{ITUNES_NS}}}duration", row["duration"].strip())
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

    out_file = out_dir / "feed.xml"
    out_file.write_bytes(pretty_xml)
    print(f"Generated {out_file} ({len(rows)} episodes) + copied {cover_dest.name}")


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
