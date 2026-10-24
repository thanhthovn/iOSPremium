from pathlib import Path
from datetime import datetime, timezone
import re
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# IPTV SCANNING CHANNEL is a modal inside iOSPremium, not the separate 1ptvsc4n page.
URL = "https://thanhthovn.github.io/iOSPremium/"
OUTPUT_DIR = Path("output")
MAX_SCAN_SECONDS = 4 * 60 * 60
POLL_SECONDS = 3


def local_timestamp():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_playable_channels(summary: str) -> int:
    """Read the scanner's current summary format, with backward compatibility."""
    patterns = (
        r"kênh\s+phát\s+được\s*:\s*(\d+)",
        r"phát\s+được\s*:\s*(\d+)\s*kênh",
    )
    for pattern in patterns:
        match = re.search(pattern, summary, re.IGNORECASE)
        if match:
            return int(match.group(1))
    raise ValueError(f"Could not parse playable-channel count from scanner summary: {summary!r}")


def validate_m3u(path: Path) -> None:
    """Reject empty, malformed, or URL-less M3U downloads before publishing."""
    content = path.read_text(encoding="utf-8-sig").strip()
    if not content.startswith("#EXTM3U"):
        raise ValueError(f"Exported playlist does not start with #EXTM3U: {path}")
    urls = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not urls:
        raise ValueError(f"Exported playlist contains no stream URLs: {path}")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = local_timestamp()
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(30_000)

        page.on("pageerror", lambda exc: errors.append(f"Page error: {exc}"))
        page.on(
            "console",
            lambda msg: print(f"[browser:{msg.type}] {msg.text}")
            if msg.type == "error" else None,
        )

        try:
            print(f"Opening {URL}")
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)

            # This button opens the scanner modal on the same iOSPremium page.
            scanner_button = page.locator("#cinemaxBtn")
            scanner_button.wait_for(state="visible", timeout=60_000)
            print("Opening IPTV SCANNING CHANNEL modal...")
            scanner_button.click()

            modal = page.locator(".scanner-modal")
            modal.wait_for(state="visible", timeout=30_000)
            summary = modal.locator("#scannerSummary")
            results = modal.locator("#scannerResults")
            load_button = modal.locator("#scannerLoad")
            check_all_button = modal.locator("#scannerTestAll")
            stop_button = modal.locator("#scannerStop")
            export_button = modal.locator("#scannerExportM3U")

            # The modal automatically tries to load linkworks.txt. Retry once if needed.
            deadline = time.monotonic() + 90
            retried_load = False
            while time.monotonic() < deadline:
                status = summary.inner_text().strip()
                rows = results.locator(".scanner-link").count()
                if check_all_button.is_enabled() and rows > 0:
                    break
                if (
                    not retried_load
                    and load_button.is_enabled()
                    and ("Không tải được" in status or "Không đọc được" in status)
                ):
                    load_button.click()
                    retried_load = True
                page.wait_for_timeout(1000)
            else:
                raise TimeoutError(
                    "The scanner modal did not load linkworks.txt within 90 seconds. "
                    f"Last status: {summary.inner_text().strip()}"
                )

            total_links = results.locator(".scanner-link").count()
            print(f"Loaded {total_links} scanner links. Clicking KIỂM TRA TẤT CẢ...")
            check_all_button.click()

            deadline = time.monotonic() + MAX_SCAN_SECONDS
            last_summary = ""
            while time.monotonic() < deadline:
                page.wait_for_timeout(POLL_SECONDS * 1000)
                status = summary.inner_text().strip()
                if status and status != last_summary:
                    print(f"[scanner] {status}")
                    last_summary = status

                # During scanning, "check all" is disabled and "stop" is enabled.
                # This final-state check also handles scans that finish very quickly.
                if check_all_button.is_enabled() and stop_button.is_disabled():
                    break
            else:
                raise TimeoutError(
                    f"Checking all links exceeded {MAX_SCAN_SECONDS // 60} minutes. "
                    f"Last status: {summary.inner_text().strip()}"
                )

            final_summary = summary.inner_text().strip()
            results_text = results.inner_text().strip()
            modal_text = (
                "IPTV SCANNING CHANNEL — TEXT RESULTS\n"
                f"Started: {started}\n"
                f"Finished: {local_timestamp()}\n\n"
                f"SUMMARY\n{final_summary}\n\n"
                f"RESULTS\n{results_text}\n"
            )
            (OUTPUT_DIR / "ket_qua.txt").write_text(modal_text, encoding="utf-8")
            (OUTPUT_DIR / "scanner-summary.txt").write_text(
                f"Started: {started}\n"
                f"Finished: {local_timestamp()}\n"
                f"Total links loaded: {total_links}\n"
                f"Final scanner summary: {final_summary}\n",
                encoding="utf-8",
            )

            playable_channels = parse_playable_channels(final_summary)
            print(f"Checking complete. Playable channels: {playable_channels}")

            if playable_channels > 0:
                print("Clicking XUẤT M3U...")
                with page.expect_download(timeout=60_000) as download_info:
                    export_button.click()

                download = download_info.value
                filename = Path(download.suggested_filename).name
                if not filename.lower().endswith(".m3u"):
                    filename = "TRUYENHINHCAPVIETNAM_checked.m3u"
                # A deterministic name prevents publishing an arbitrary stale file.
                destination = OUTPUT_DIR / "TRUYENHINHCAPVIETNAM_checked.m3u"
                download.save_as(str(destination))
                validate_m3u(destination)
                print(f"M3U saved and validated: {destination}")

                (OUTPUT_DIR / "ket_qua.txt").write_text(
                    modal_text
                    + f"\nPOST-EXPORT STATUS\n{summary.inner_text().strip()}\n",
                    encoding="utf-8",
                )
            else:
                print("No playable channels reported; skipping M3U export.")

        except Exception as exc:
            errors.append(f"Scanner exception: {type(exc).__name__}: {exc}")
            try:
                page.screenshot(path=str(OUTPUT_DIR / "error.png"), full_page=True)
                (OUTPUT_DIR / "error-page.txt").write_text(
                    page.locator("body").inner_text(), encoding="utf-8"
                )
            except Exception as diagnostic_exc:
                errors.append(f"Could not capture error page: {diagnostic_exc}")
            raise
        finally:
            (OUTPUT_DIR / "browser-errors.txt").write_text(
                "\n".join(errors) + ("\n" if errors else "No browser page errors recorded.\n"),
                encoding="utf-8",
            )
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
