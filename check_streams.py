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

def _request_sample(url, headers, limit=256 * 1024, byte_range=None):
    request_headers = dict(headers)
    if byte_range:
        request_headers["Range"] = byte_range
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        status = response.getcode()
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "").lower()
        body = response.read(limit)
    return status, final_url, content_type, body


def _first_media_uri(lines):
    """Return the first media segment URI, including common LL-HLS parts."""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-PART:"):
            match = re.search(r'\bURI="([^"]+)"', line)
            if match:
                return match.group(1)
        if line.startswith("#"):
            continue
        return line
    return None


def _validate_hls(url, body, headers, depth=0):
    """Validate a manifest and fetch one real media segment/part when available."""
    sample = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not sample.startswith(b"#EXTM3U"):
        return False, "response is not an HLS manifest"

    text = sample.decode("utf-8", errors="replace")
    lines = text.splitlines()

    # For master playlists, follow the first listed variant and validate its media.
    if any(line.strip().startswith("#EXT-X-STREAM-INF:") for line in lines):
        if depth >= 2:
            return False, "nested master playlist depth limit reached"
        for i, raw in enumerate(lines):
            if raw.strip().startswith("#EXT-X-STREAM-INF:"):
                for candidate_line in lines[i + 1:]:
                    candidate_line = candidate_line.strip()
                    if not candidate_line:
                        continue
                    if candidate_line.startswith("#"):
                        continue
                    variant_url = urllib.parse.urljoin(url, candidate_line)
                    try:
                        status, final_url, content_type, variant_body = _request_sample(
                            variant_url, headers
                        )
                        if not 200 <= status < 400:
                            continue
                        valid, detail = _validate_hls(
                            final_url, variant_body, headers, depth + 1
                        )
                        if valid:
                            return True, "HLS master + variant + media segment OK"
                    except Exception:
                        continue
                return False, "master playlist found, but no variant passed media validation"

    # Media playlists should contain at least one segment URI or LL-HLS part.
    media_uri = _first_media_uri(lines)
    if not media_uri:
        return False, "HLS manifest has no media segment/part URI"

    segment_url = urllib.parse.urljoin(url, media_uri)
    try:
        status, final_url, content_type, segment = _request_sample(
            segment_url, headers, limit=4096, byte_range="bytes=0-4095"
        )
        if not 200 <= status < 400:
            return False, f"media segment HTTP {status}"
        if not segment:
            return False, "media segment returned an empty body"
        segment_sample = segment.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if segment_sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
            return False, "media segment URL returned HTML instead of media bytes"
        if segment_sample.startswith(b"#extm3u"):
            return False, "media segment URL returned another playlist, not media bytes"
        return True, "HLS manifest + media segment OK"
    except urllib.error.HTTPError as exc:
        return False, f"media segment HTTP {exc.code}"
    except Exception as exc:
        return False, f"media segment check failed ({type(exc).__name__})"


def fetch_candidate(url, user_agent):
    headers = {
        "User-Agent": user_agent or UA_DEFAULT,
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
        "Connection": "close",
    }
    try:
        status, final_url, content_type, body = _request_sample(url, headers)
        if status < 200 or status >= 400:
            return False, f"HTTP {status}", final_url

        sample = body.lstrip(b"\xef\xbb\xbf \t\r\n")
        if sample.startswith(b"#EXTM3U"):
            valid, detail = _validate_hls(final_url, body, headers)
            return valid, f"HTTP {status}; {detail}", final_url

        # Non-HLS direct media URLs can still be valid candidates.
        if content_type.startswith(("video/", "audio/")) and body:
            sample_lower = sample.lower()
            if sample_lower.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                return False, f"HTTP {status}; media URL returned HTML", final_url
            return True, f"HTTP {status}; direct media response", final_url

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
    # Use the playlist entry's existing VLC User-Agent when checking candidates.
    ua_by_id = {}
    for start, end in entries:
        info = "".join(lines[start:end])
        match = re.search(r'\btvg-id="([^"]*)"', info)
        if not match or not match.group(1).strip():
            continue
        ua_match = re.search(r"(?im)^#EXTVLCOPT:http-user-agent=(.+?)\s*$", info)
        if ua_match:
            ua_by_id.setdefault(match.group(1).strip(), ua_match.group(1).strip())
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
            ok, reason, final_url = fetch_candidate(url, ua_by_id.get(tvg_id, UA_DEFAULT))
            candidates_report.append({
                "url": safe_url(url), "ok": ok, "reason": reason,
                "final_url": safe_url(final_url)
            })
            if ok:
                selected = url  # Keep the source URL, not a temporary redirect URL.
                break
        report["channels"][tvg_id] = {
            "selected": safe_url(selected) if selected else None,
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
