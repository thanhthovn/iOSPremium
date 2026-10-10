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
REPORT = Path(os.environ.get("REPORT_FILE", "stream_status.json"))
TIMEOUT = int(os.environ.get("CHECK_TIMEOUT", "12"))
UA_DEFAULT = os.environ.get(
    "STREAM_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

def parse_sources(text):
    """Parse grouped URLs and optional pipe-delimited request metadata."""
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

        parts = [part.strip() for part in line.split("|")]
        if parts[0].lower().startswith(("http://", "https://")):
            tvg_id, url, metadata = current, parts[0], parts[1:]
        elif len(parts) >= 2 and parts[1].lower().startswith(("http://", "https://")):
            tvg_id, url, metadata = parts[0], parts[1], parts[2:]
        else:
            continue
        if not tvg_id or not url:
            continue

        candidate = {"url": url, "referer": None, "origin": None, "user_agent": None}
        for item in metadata:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key, value = key.strip().lower(), value.strip()
            if not value:
                continue
            if key in ("referer", "ref"):
                candidate["referer"] = value
            elif key == "origin":
                candidate["origin"] = value
            elif key in ("ua", "user-agent", "useragent"):
                candidate["user_agent"] = value
        groups.setdefault(tvg_id, []).append(candidate)
    return groups

def safe_url(url):
    """Hide credentials, token-bearing paths, and query strings in reports."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "[redacted host]"
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}/[redacted]"
    except Exception:
        return "[redacted URL]"

def _sanitize_diagnostic_text(value, limit=320):
    """Sanitize response text/header values before writing them to logs or reports."""
    text = str(value or "")
    # Remove complete URLs so query strings and signed paths cannot leak.
    text = re.sub(r"(?i)https?://[^\s\"'<>]+", "[URL redacted]", text)
    # Hide common secret assignments, including JSON-style quoted keys.
    text = re.sub(
        r"""(?i)(["']?(?:authorization|cookie|set-cookie|token|access_token|refresh_token|signature|sig|key|api_key|password|passwd)["']?\s*[:=]\s*["']?)[^\s,;<>}"']+""",
        r"\1[redacted]", text,
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "[empty response body]"


def _safe_error_body(body, limit=320):
    """Return a short, sanitized error-body snippet suitable for CI logs."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body or "")
    return _sanitize_diagnostic_text(text, limit)


def _http_error_detail(exc, context="request"):
    """Capture useful 403 diagnostics without exposing full URLs or credentials."""
    allowed_headers = (
        "Server", "Via", "Content-Type", "WWW-Authenticate", "X-Cache",
        "CF-Ray", "X-Request-ID", "X-Correlation-ID", "X-Deny-Reason",
        "X-Error-Code", "X-Tengine-Error",
    )
    safe_headers = {}
    for name in allowed_headers:
        value = exc.headers.get(name) if exc.headers else None
        if value:
            safe_headers[name] = _sanitize_diagnostic_text(value, 180)
    try:
        body = exc.read(2048)
    except Exception:
        body = b""
    snippet = _safe_error_body(body)
    return f"HTTP {exc.code} ({context}); response_headers={json.dumps(safe_headers, ensure_ascii=False, sort_keys=True)}; response_body={snippet!r}"

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
    sample = body.lstrip(bytes([239, 187, 191, 32, 9, 13, 10]))
    if not sample.startswith(b"#EXTM3U"):
        return False, "response is not an HLS manifest"

    text = sample.decode("utf-8", errors="replace")
    lines = text.splitlines()

    # For master playlists, follow the first listed variant and validate its media.
    if any(line.strip().startswith("#EXT-X-STREAM-INF:") for line in lines):
        if depth >= 2:
            return False, "nested master playlist depth limit reached"
        variant_failures = []
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
                            variant_failures.append(f"variant HTTP {status}")
                            continue
                        valid, detail = _validate_hls(
                            final_url, variant_body, headers, depth + 1
                        )
                        if valid:
                            return True, "HLS master + variant + media segment OK"
                        variant_failures.append(detail)
                    except urllib.error.HTTPError as exc:
                        variant_failures.append(_http_error_detail(exc, "HLS variant"))
                    except Exception as exc:
                        variant_failures.append(type(exc).__name__)
        if variant_failures:
            # Preserve access-denied and transient errors so the caller can mark
            # this candidate as uncertain rather than treating it as dead.
            if any(re.search(r"\bHTTP\s+(401|403|408|425|429|500|501|502|503|504|507|509|520|521|522|523|524)\b", x) for x in variant_failures):
                return False, "master playlist variant could not be verified: " + "; ".join(variant_failures[:3])
            if any(any(k in x.lower() for k in ("timeout", "urlerror", "connection", "sslerror")) for x in variant_failures):
                return False, "master playlist variant check had transient errors: " + "; ".join(variant_failures[:3])
        return False, "master playlist found, but no variant passed media validation"

    # Media playlists should contain at least one segment URI or LL-HLS part.
    media_uri = _first_media_uri(lines)
    if not media_uri:
        return False, "HLS manifest has no media segment/part URI"

    segment_url = urllib.parse.urljoin(url, media_uri)
    # Some CDNs reject HTTP Range requests even though the segment is playable.
    # Try a small ranged read first, then retry without Range before rejecting it.
    last_error = None
    for use_range in (True, False):
        try:
            status, final_url, content_type, segment = _request_sample(
                segment_url,
                headers,
                limit=4096,
                byte_range="bytes=0-4095" if use_range else None,
            )
            if not 200 <= status < 400:
                last_error = f"media segment HTTP {status}"
                continue
            if not segment:
                last_error = "media segment returned an empty body"
                continue
            segment_sample = segment.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
            if segment_sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                return False, "media segment URL returned HTML instead of media bytes"
            if segment_sample.startswith(b"#extm3u"):
                return False, "media segment URL returned another playlist, not media bytes"
            return True, "HLS manifest + media segment OK" + (" (full-read fallback)" if not use_range else "")
        except urllib.error.HTTPError as exc:
            last_error = _http_error_detail(exc, "media segment")
        except Exception as exc:
            last_error = f"media segment check failed ({type(exc).__name__})"
    return False, last_error or "media segment check failed"


def _classify_failure(reason):
    """Return 'unknown' for access/transient errors, otherwise a definitive failure."""
    text = str(reason).lower()
    if re.search(r"\bhttp\s+(401|403|408|425|429|500|501|502|503|504|507|509|520|521|522|523|524)\b", text):
        return "unknown"
    transient_names = (
        "timeout", "urlerror", "connectionerror", "connectionreseterror",
        "remoteDisconnected", "incompleteread", "temporaryfailure",
        "networkisunreachable", "sslerror", "socket.timeout",
    )
    if any(name.lower() in text for name in transient_names):
        return "unknown"
    return "failed"


def fetch_candidate(url, user_agent, referer=None, origin=None):
    """Probe a candidate with alternate request profiles, including nested HLS URLs."""
    base_headers = {
        "User-Agent": user_agent or UA_DEFAULT,
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if referer:
        base_headers["Referer"] = referer
    if origin:
        base_headers["Origin"] = origin

    profiles = [("configured/default headers", dict(base_headers))]

    # Try alternate clients even when 403 happens on a variant or media segment,
    # not only when the initial master-playlist request itself returns 403.
    vlc_headers = dict(base_headers)
    vlc_headers["User-Agent"] = "VLC/3.0.21 LibVLC/3.0.21"
    profiles.append(("VLC User-Agent", vlc_headers))

    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.endswith("fptplay.net") or host.endswith("fptplay.vn"):
        fpt_headers = dict(base_headers)
        fpt_headers["Referer"] = referer or "https://fptplay.vn/"
        fpt_headers["Origin"] = origin or "https://fptplay.vn"
        profiles.append(("FPT Play Referer/Origin", fpt_headers))

    diagnostics = []
    saw_uncertain = False
    for profile_name, headers in profiles:
        try:
            status, final_url, content_type, body = _request_sample(url, headers)
            if status < 200 or status >= 400:
                reason = f"HTTP {status}"
                state = _classify_failure(reason)
                diagnostics.append(f"{profile_name}: {reason}")
                saw_uncertain = saw_uncertain or state == "unknown"
                continue

            sample = body.lstrip(bytes([239, 187, 191, 32, 9, 13, 10]))
            if sample.startswith(b"#EXTM3U"):
                valid, detail = _validate_hls(final_url, body, headers)
                reason = f"HTTP {status} with {profile_name}; {detail}"
                if valid:
                    return True, reason, final_url, "working"
                diagnostics.append(f"{profile_name}: {detail}")
                saw_uncertain = saw_uncertain or _classify_failure(detail) == "unknown"
                continue

            if content_type.startswith(("video/", "audio/")) and body:
                if sample.lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                    diagnostics.append(f"{profile_name}: media URL returned HTML")
                    continue
                return True, f"HTTP {status} with {profile_name}; direct media response", final_url, "working"

            diagnostics.append(f"{profile_name}: HTTP {status}; response is not HLS/media")
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc, profile_name)
            diagnostics.append(detail)
            saw_uncertain = saw_uncertain or _classify_failure(detail) == "unknown"
        except Exception as exc:
            detail = f"{profile_name}: {type(exc).__name__}"
            diagnostics.append(detail)
            saw_uncertain = saw_uncertain or _classify_failure(detail) == "unknown"

    reason = _sanitize_diagnostic_text("; ".join(diagnostics) or "candidate check failed", 500)
    return False, reason, url, "unknown" if saw_uncertain else "failed"

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
    # Remember current playlist URLs so uncertain candidates never force a switch.
    existing_url_by_id = {}
    for start, end in entries:
        info = "".join(lines[start:end])
        match = re.search(r'\btvg-id="([^"]*)"', info)
        if not match or not match.group(1).strip():
            continue
        tvg_id = match.group(1).strip()
        for i in range(start + 1, end):
            candidate_line = lines[i].strip()
            if candidate_line.lower().startswith(("http://", "https://")):
                existing_url_by_id.setdefault(tvg_id, candidate_line)
                break

    checks = {}
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "playlist": str(PLAYLIST),
        "source_file": str(SOURCES),
        "channels": {},
        "updated_entries": 0,
        "unchanged_entries": 0,
        "retained_due_to_uncertainty": 0,
    }

    # Candidates are ordered by preference. A blocked/transient higher-priority
    # URL is UNKNOWN, not dead: never replace the current URL based on that alone.
    for tvg_id, candidate_items in sources.items():
        urls = [item["url"] for item in candidate_items]
        existing_url = existing_url_by_id.get(tvg_id)
        if not urls:
            report["channels"][tvg_id] = {
                "selected": safe_url(existing_url) if existing_url else None,
                "status": "no_candidates",
                "reason": "no candidate URLs in linkworks.txt; existing URL retained",
                "candidates": [],
            }
            checks[tvg_id] = None
            continue

        candidates_report = []
        selected = None
        selected_status = "no_working_candidate"
        uncertain_candidate = None

        # Sequential iteration deliberately preserves source priority.
        for index, candidate in enumerate(candidate_items):
            url = candidate["url"]
            ok, reason, final_url, state = fetch_candidate(
                url,
                candidate.get("user_agent") or ua_by_id.get(tvg_id, UA_DEFAULT),
                referer=candidate.get("referer"),
                origin=candidate.get("origin"),
            )
            candidates_report.append({
                "priority": index + 1,
                "url": safe_url(url),
                "status": state,
                "ok": ok,
                "reason": _sanitize_diagnostic_text(reason, 500),
                "final_url": safe_url(final_url),
            })

            if state == "unknown":
                # This candidate could not be verified from this runner.
                # Keep checking lower-priority candidates; a later verified
                # working URL may be selected as the fallback.
                if uncertain_candidate is None:
                    uncertain_candidate = url
                continue
            if ok:
                # Keep the first verified working candidate by priority, but
                # continue checking the remaining candidates for diagnostics.
                if selected is None:
                    selected = url
                    selected_status = "working"
                continue

        if uncertain_candidate and not selected:
            checks[tvg_id] = None
            report["retained_due_to_uncertainty"] += 1
            report["channels"][tvg_id] = {
                "selected": safe_url(existing_url) if existing_url else None,
                "status": "retained_existing_uncertain",
                "reason": "higher-priority candidate could not be verified; existing URL retained",
                "candidates": candidates_report,
                "not_checked": max(0, len(urls) - len(candidates_report)),
            }
        elif selected:
            checks[tvg_id] = selected
            report["channels"][tvg_id] = {
                "selected": safe_url(selected),
                "status": selected_status,
                "reason": "first verified working candidate in source order",
                "candidates": candidates_report,
                "not_checked": max(0, len(urls) - len(candidates_report)),
            }
        else:
            checks[tvg_id] = None
            report["channels"][tvg_id] = {
                "selected": safe_url(existing_url) if existing_url else None,
                "status": "retained_existing_no_verified_candidate",
                "reason": "all checked candidates failed definitively; existing URL retained",
                "candidates": candidates_report,
                "not_checked": max(0, len(urls) - len(candidates_report)),
            }

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
    # Defensive final pass before the JSON artifact is written.
    for channel in report["channels"].values():
        for candidate in channel.get("candidates", []):
            candidate["reason"] = _sanitize_diagnostic_text(candidate.get("reason", ""), 500)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Checked {len(sources)} tvg-id groups; updated {report['updated_entries']} playlist entries.")
    print(f"Retained due to uncertain access: {report['retained_due_to_uncertainty']} groups.")
    print(f"Unchanged entries: {report['unchanged_entries']}. Report: {REPORT}")

if __name__ == "__main__":
    main()
