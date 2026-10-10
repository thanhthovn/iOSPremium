#!/usr/bin/env python3
"""Check IPTV candidate URLs in linkworks.txt and update matching tvg-id entries."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PLAYLIST = Path(os.environ.get("PLAYLIST_FILE", "TRUYENHINHCAPVIETNAM.m3u"))
SOURCES = Path(os.environ.get("SOURCES_FILE", "linkworks.txt"))
REPORT = Path("stream_status.json")
TIMEOUT = int(os.environ.get("CHECK_TIMEOUT", "12"))
UA_DEFAULT = os.environ.get(
    "STREAM_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

def parse_sources(text):
    groups = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "//")):
            continue
        if line.startswith("#"):
            current = line[1:].strip()
            if current:
                groups.setdefault(current, [])
            continue
        if current and line.lower().startswith(("http://", "https://")):
            groups.setdefault(current, []).append(line)
    return groups

def safe_url(url):
    """Hide token-bearing paths and query strings in committed reports."""
    try:
        parts = urllib.parse.urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/[redacted]"
    except Exception:
        return "[redacted URL]"

def fetch_candidate(url, user_agent):
    headers = {
        "User-Agent": user_agent or UA_DEFAULT,
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
        "Connection": "close",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            status = response.getcode()
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "").lower()
            body = response.read(256 * 1024)
        if status < 200 or status >= 400:
            return False, f"HTTP {status}", final_url
        sample = body.lstrip(b"\xef\xbb\xbf \t\r\n")
        # HLS must return a real playlist, not merely an HTTP 200 error page.
        if sample.startswith(b"#EXTM3U"):
            return True, f"HTTP {status}; HLS playlist", final_url
        # A few providers return a media response rather than a manifest URL.
        if content_type.startswith(("video/", "audio/")) and body:
            return True, f"HTTP {status}; media content-type {content_type}", final_url
        return False, f"HTTP {status}; response is not an HLS playlist/media response", final_url
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", url
    except Exception as exc:
        return False, f"{type(exc).__name__}", url

def parse_playlist(text):
    # Entries begin at EXTINF and continue to the next EXTINF or end of file.
    lines = text.splitlines(keepends=True)
    entries = []
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#EXTINF:"):
            if start is not None:
                entries.append((start, i))
            start = i
    if start is not None:
        entries.append((start, len(lines)))
    return lines, entries

def main():
    if not PLAYLIST.is_file():
        raise SystemExit(f"Playlist not found: {PLAYLIST}")
    if not SOURCES.is_file():
        raise SystemExit(f"Source file not found: {SOURCES}")
    playlist_text = PLAYLIST.read_text(encoding="utf-8-sig")
    sources_text = SOURCES.read_text(encoding="utf-8-sig")
    sources = parse_sources(sources_text)
    if not sources:
        raise SystemExit("linkworks.txt has no #tvg-id groups. Add IDs and URLs first.")

    lines, entries = parse_playlist(playlist_text)
    checks = {}
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "playlist": str(PLAYLIST),
        "source_file": str(SOURCES),
        "channels": {},
        "updated_entries": 0,
        "unchanged_entries": 0,
    }

    # Check each candidate once per tvg-id, in the exact top-to-bottom order.
    for tvg_id, urls in sources.items():
        if not urls:
            report["channels"][tvg_id] = {
                "selected": None, "reason": "no candidate URLs in linkworks.txt",
                "candidates": []
            }
            continue
        candidates_report = []
        selected = None
        for url in urls:
            ok, reason, final_url = fetch_candidate(url, UA_DEFAULT)
            candidates_report.append({
                "url": safe_url(url), "ok": ok, "reason": reason,
                "final_url": safe_url(final_url)
            })
            if ok:
                selected = url  # Keep the source URL, not a temporary redirect URL.
                break
        report["channels"][tvg_id] = {
            "selected": selected,
            "reason": "first working candidate in source order" if selected else "all candidates failed; existing URL retained",
            "candidates": candidates_report,
        }
        checks[tvg_id] = selected

    # Change only the stream URL line in entries whose non-empty tvg-id matches.
    for start, end in entries:
        info = "".join(lines[start:end])
        match = re.search(r'\btvg-id="([^"]*)"', info)
        if not match:
            continue
        tvg_id = match.group(1).strip()
        selected = checks.get(tvg_id)
        if not tvg_id or not selected:
            report["unchanged_entries"] += 1
            continue

        # Respect the entry's existing VLC User-Agent when validating the chosen URL.
        # Recheck with the channel's configured UA if it differs from the default.
        ua_match = re.search(r"(?im)^#EXTVLCOPT:http-user-agent=(.+?)\s*$", info)
        channel_ua = ua_match.group(1).strip() if ua_match else UA_DEFAULT
        old_url_line = None
        for i in range(start + 1, end):
            stripped = lines[i].strip()
            if stripped.lower().startswith(("http://", "https://")):
                old_url_line = i
                break
        if old_url_line is None:
            report["unchanged_entries"] += 1
            continue

        old_url = lines[old_url_line].strip()
        if old_url != selected:
            # If the channel needs a specific UA, ensure the selected source still passes with it.
            if channel_ua != UA_DEFAULT:
                ok, _, _ = fetch_candidate(selected, channel_ua)
                if not ok:
                    report["unchanged_entries"] += 1
                    continue
            ending = "\r\n" if lines[old_url_line].endswith("\r\n") else "\n" if lines[old_url_line].endswith("\n") else ""
            lines[old_url_line] = selected + ending
            report["updated_entries"] += 1
        else:
            report["unchanged_entries"] += 1

    output = "".join(lines)
    if output != playlist_text:
        PLAYLIST.write_text(output, encoding="utf-8", newline="")
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Checked {len(sources)} tvg-id groups; updated {report['updated_entries']} playlist entries.")
    print(f"Unchanged entries: {report['unchanged_entries']}. Report: {REPORT}")

if __name__ == "__main__":
    main()
