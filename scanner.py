#!/usr/bin/env python3
"""
IPTV Link Scanner - Python version
Đọc linkworks.txt, kiểm tra từng link có phát được không.
Link nào PHÁT ĐƯỢC đầu tiên (từ trên xuống) sẽ được ghi đè vào M3U.
"""

import os
import re
import sys
import time
import argparse
from pathlib import Path
from urllib.parse import urljoin

import requests
from requests.exceptions import RequestException, Timeout, ConnectionError

try:
    import m3u8
    HAS_M3U8 = True
except ImportError:
    HAS_M3U8 = False
    print("⚠ Cài đặt: pip install m3u8", file=sys.stderr)

# ============================================================
# MÀU - TẮT KHI CHẠY TRÊN CI
# ============================================================
IS_CI = bool(os.environ.get("CI") or not sys.stdout.isatty())

try:
    from colorama import init, Fore, Style
    if IS_CI:
        class Fore:
            GREEN = RED = YELLOW = CYAN = MAGENTA = RESET = ""
        class Style:
            BRIGHT = RESET_ALL = ""
    else:
        init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False
    class Fore:
        GREEN = RED = YELLOW = CYAN = MAGENTA = RESET = ""
    class Style:
        BRIGHT = RESET_ALL = ""

# ============================================================
# CẤU HÌNH
# ============================================================
import os
M3U_URL = os.environ.get(
    "M3U_URL",
    "https://raw.githubusercontent.com/thanhthovn/iOSPremium/refs/heads/main/TRUYENHINHCAPVIETNAM.m3u"
)
LINKWORKS_FILE = "linkworks.txt"
OUTPUT_M3U = "TRUYENHINHCAPVIETNAM_checked.m3u"

TIMEOUT_MANIFEST = 10
TIMEOUT_SEGMENT = 10
SEGMENT_MIN_SIZE = 1024

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
}

# ============================================================
# LOG
# ============================================================
def log_info(msg): print(f"{Fore.CYAN}ℹ {msg}{Style.RESET_ALL}")
def log_ok(msg):   print(f"{Fore.GREEN}✅ {msg}{Style.RESET_ALL}")
def log_warn(msg): print(f"{Fore.YELLOW}⚠ {msg}{Style.RESET_ALL}")
def log_err(msg):  print(f"{Fore.RED}❌ {msg}{Style.RESET_ALL}")
def log_step(msg): print(f"{Fore.MAGENTA}▶ {msg}{Style.RESET_ALL}")

# ============================================================
# CHUẨN HÓA
# ============================================================
def normalize_tvg_id(value):
    return re.sub(r'[^a-z0-9]', '', str(value or "").strip().lower())

def normalize_loose(value):
    return re.sub(r'(hd|fhd|uhd|sd|4k)$', '', normalize_tvg_id(value))

# ============================================================
# PARSE LINKWORKS.TXT
# ============================================================
def parse_linkworks(text):
    groups = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            name = line[1:].strip()
            name = re.sub(r'^tvg-(id|name)\s*[:=]?\s*', '', name, flags=re.IGNORECASE)
            name = re.sub(r'^[:=]\s*', '', name)
            name = name.strip('"\'').strip()
            if name:
                current = {"id": name, "urls": []}
                groups.append(current)
            continue
        if re.match(r'^https?://', line, re.IGNORECASE):
            if current is None:
                current = {"id": "Chưa phân nhóm", "urls": []}
                groups.append(current)
            current["urls"].append(line)
    return [g for g in groups if g["urls"]]

# ============================================================
# FETCH
# ============================================================
def fetch(url, timeout=10, stream=False):
    try:
        return requests.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            stream=stream,
            allow_redirects=True,
            verify=False,
        )
    except (Timeout, ConnectionError, RequestException):
        return None

# ============================================================
# KIỂM TRA LINK HLS
# ============================================================
def is_hls_playable(url, verbose=False):
    result = {"playable": False, "reason": "", "method": "fail"}

    r = fetch(url, timeout=TIMEOUT_MANIFEST)
    if r is None:
        result["reason"] = "Không kết nối được (timeout/connection error)"
        return result

    if r.status_code != 200:
        result["reason"] = f"HTTP {r.status_code}"
        return result

    content_type = (r.headers.get("content-type") or "").lower()
    body = r.text.strip()

    if not body:
        result["reason"] = "Manifest rỗng"
        return result

    if not body.startswith("#EXTM3U"):
        if "html" in content_type:
            result["reason"] = "Trả về HTML (không phải HLS)"
        elif "json" in content_type:
            result["reason"] = "Trả về JSON (không phải HLS)"
        else:
            result["reason"] = f"Không phải HLS manifest (Content-Type: {content_type or 'rỗng'})"
        return result

    if not HAS_M3U8:
        result["reason"] = "Thiếu thư viện m3u8"
        return result

    try:
        playlist = m3u8.loads(body, uri=r.url)
    except Exception as e:
        result["reason"] = f"Parse HLS lỗi: {e}"
        return result

    segment_url = None
    base_url = r.url

    if playlist.is_variant and playlist.playlists:
        variant = playlist.playlists[0]
        variant_url = urljoin(base_url, variant.uri)
        if verbose:
            log_info(f"Variant đầu tiên: {variant_url}")

        rv = fetch(variant_url, timeout=TIMEOUT_MANIFEST)
        if rv is None or rv.status_code != 200:
            result["reason"] = f"Variant trả về HTTP {rv.status_code if rv else 'timeout'}"
            return result

        try:
            variant_playlist = m3u8.loads(rv.text, uri=rv.url)
        except Exception as e:
            result["reason"] = f"Parse variant lỗi: {e}"
            return result

        if variant_playlist.segments:
            segment_url = urljoin(rv.url, variant_playlist.segments[0].uri)
        else:
            result["reason"] = "Variant không có segment"
            return result
    else:
        if playlist.segments:
            segment_url = urljoin(base_url, playlist.segments[0].uri)
        else:
            result["reason"] = "Manifest không có segment"
            return result

    if segment_url:
        if verbose:
            log_info(f"Segment đầu tiên: {segment_url}")

        rs = fetch(segment_url, timeout=TIMEOUT_SEGMENT, stream=True)
        if rs is None:
            result["reason"] = "Segment đầu tiên không tải được"
            return result

        if rs.status_code != 200:
            result["reason"] = f"Segment trả về HTTP {rs.status_code}"
            return result

        chunk = b""
        try:
            for c in rs.iter_content(chunk_size=1024):
                chunk += c
                if len(chunk) >= 4096:
                    break
            rs.close()
        except Exception as e:
            result["reason"] = f"Lỗi đọc segment: {e}"
            return result

        if len(chunk) < SEGMENT_MIN_SIZE:
            result["reason"] = f"Segment quá nhỏ ({len(chunk)} bytes)"
            return result

        is_ts = chunk[0:1] == b'\x47'
        is_fmp4 = b'ftyp' in chunk[:16] or b'moof' in chunk[:16] or b'styp' in chunk[:16]

        if is_ts or is_fmp4:
            result["playable"] = True
            result["reason"] = f"Segment OK ({'TS' if is_ts else 'fMP4'}, {len(chunk)}+ bytes)"
            result["method"] = "manifest+segment"
            return result
        else:
            if len(chunk) >= SEGMENT_MIN_SIZE:
                result["playable"] = True
                result["reason"] = f"Segment OK ({len(chunk)}+ bytes)"
                result["method"] = "manifest+segment"
                return result
            result["reason"] = "Segment không phải video hợp lệ"
            return result

    result["playable"] = True
    result["reason"] = "Manifest OK"
    result["method"] = "manifest-only"
    return result

# ============================================================
# XUẤT M3U
# ============================================================
def parse_m3u_extinf(line):
    id_m = re.search(r'tvg-id\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))', line, re.IGNORECASE)
    name_m = re.search(r'tvg-name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))', line, re.IGNORECASE)
    tvg_id = (id_m.group(1) or id_m.group(2) or id_m.group(3) or "") if id_m else ""
    tvg_name = (name_m.group(1) or name_m.group(2) or name_m.group(3) or "") if name_m else ""
    comma = line.rfind(",")
    display = line[comma + 1:].strip() if comma >= 0 else ""
    return {"tvg_id": tvg_id, "tvg_name": tvg_name, "display": display}


def export_m3u(playable_map, source_url, output_path):
    log_step(f"Tải playlist gốc: {source_url}")
    r = fetch(source_url, timeout=30)
    if r is None or r.status_code != 200:
        log_err(f"Không tải được playlist gốc (HTTP {r.status_code if r else 'timeout'})")
        return 0

    original = r.text
    if not original.lstrip().startswith("#EXTM3U"):
        log_err("Nội dung nguồn không phải M3U hợp lệ")
        return 0

    line_ending = "\r\n" if "\r\n" in original else "\n"
    lines = original.split(line_ending) if line_ending == "\r\n" else original.split("\n")

    def find_next_url(start_idx):
        for i in range(start_idx + 1, len(lines)):
            t = lines[i].strip()
            if not t:
                continue
            if t.startswith("#"):
                continue
            return i
        return -1

    replacements = 0
    for i, line in enumerate(lines):
        if not line.strip().upper().startswith("#EXTINF"):
            continue

        info = parse_m3u_extinf(line)
        key_id = normalize_tvg_id(info["tvg_id"])
        key_name = normalize_tvg_id(info["tvg_name"])
        key_display = normalize_tvg_id(info["display"])

        winner = None
        matched_by = ""

        for k, label in [(key_id, "tvg-id"), (key_name, "tvg-name"), (key_display, "display-name")]:
            if k and k in playable_map:
                winner = playable_map[k]
                matched_by = label
                break

        if not winner:
            for loose_k, loose_label in [
                (normalize_loose(key_id), "loose-id"),
                (normalize_loose(key_name), "loose-name"),
                (normalize_loose(key_display), "loose-display"),
            ]:
                if not loose_k:
                    continue
                for k, v in playable_map.items():
                    if normalize_loose(k) == loose_k:
                        winner = v
                        matched_by = loose_label
                        break
                if winner:
                    break

        if winner:
            url_idx = find_next_url(i)
            if url_idx != -1:
                old_line = lines[url_idx]
                leading = re.match(r'^\s*', old_line).group(0)
                trailing = re.search(r'\s*$', old_line).group(0)
                lines[url_idx] = f"{leading}{winner}{trailing}"
                replacements += 1
                log_ok(f"Ghi đè [{matched_by}] '{info['display'] or info['tvg_id']}'")
        else:
            log_warn(f"Không match: id='{key_id}' name='{key_name}' display='{key_display}'")

    final_text = line_ending.join(lines)
    Path(output_path).write_text(final_text, encoding="utf-8")
    log_ok(f"Đã lưu: {output_path} ({replacements} kênh ghi đè)")
    return replacements

# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="IPTV Link Scanner (Python)")
    parser.add_argument("--linkworks", default=LINKWORKS_FILE)
    parser.add_argument("--m3u-url", default=M3U_URL)
    parser.add_argument("--output", default=OUTPUT_M3U)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-export", action="store_true")
    args = parser.parse_args()

    linkworks_path = Path(args.linkworks)
    if not linkworks_path.exists():
        log_err(f"Không tìm thấy file: {linkworks_path}")
        sys.exit(1)

    log_step(f"Đọc file: {linkworks_path}")
    groups = parse_linkworks(linkworks_path.read_text(encoding="utf-8"))
    total_links = sum(len(g["urls"]) for g in groups)
    log_info(f"Tìm thấy {len(groups)} kênh, {total_links} link")

    playable_map = {}
    stats = {"tested": 0, "playable": 0, "failed": 0}
    start_time = time.time()

    for group in groups:
        group_id = group["id"]
        group_key = normalize_tvg_id(group_id)
        if not group_key:
            continue

        print()
        log_step(f"Kênh #{group_id} ({len(group['urls'])} link)")

        for i, url in enumerate(group["urls"], 1):
            if group_key in playable_map:
                log_warn(f"  [{i}] Bỏ qua — kênh đã có link phát được")
                continue

            stats["tested"] += 1
            log_info(f"  [{i}] Test: {url}")

            result = is_hls_playable(url, verbose=args.verbose)

            if result["playable"]:
                stats["playable"] += 1
                log_ok(f"  → PHÁT ĐƯỢC [{result['method']}] {result['reason']}")
                playable_map[group_key] = url
                break
            else:
                stats["failed"] += 1
                log_err(f"  → KHÔNG PHÁT ĐƯỢC: {result['reason']}")

    elapsed = time.time() - start_time

    print()
    print("=" * 60)
    log_info("Tổng kết:")
    print(f"  Kênh có link phát được: {len(playable_map)}/{len(groups)}")
    print(f"  Đã test: {stats['tested']} link")
    print(f"  Phát được: {stats['playable']}")
    print(f"  Không phát được: {stats['failed']}")
    print(f"  Thời gian: {elapsed:.1f} giây")
    print("=" * 60)

    if not args.no_export and playable_map:
        print()
        export_m3u(playable_map, args.m3u_url, args.output)
    elif not playable_map:
        log_warn("Không có link nào phát được → không xuất M3U")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    main()
