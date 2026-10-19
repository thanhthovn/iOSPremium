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
            saw_running = False
            last_summary = ""
            while time.monotonic() < deadline:
                page.wait_for_timeout(POLL_SECONDS * 1000)
                status = summary.inner_text().strip()
                if status and status != last_summary:
                    print(f"[scanner] {status}")
                    last_summary = status

                if check_all_button.is_disabled():
                    saw_running = True
                elif saw_running:
                    # The site re-enables this button after the full scan completes.
                    break
            else:
                raise TimeoutError(
                    f"Checking all links exceeded {MAX_SCAN_SECONDS // 60} minutes."
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

            match = re.search(
                r"PHÁT ĐƯỢC:\s*(\d+)\s*kênh",
                final_summary,
                re.IGNORECASE,
            )
            playable_channels = int(match.group(1)) if match else 0
            print(f"Checking complete. Playable channels: {playable_channels}")

            if playable_channels > 0:
                print("Clicking XUẤT M3U...")
                with page.expect_download(timeout=60_000) as download_info:
                    export_button.click()

                download = download_info.value
                filename = Path(download.suggested_filename).name
                if not filename.lower().endswith(".m3u"):
                    filename = "TRUYENHINHCAPVIETNAM_checked.m3u"
                destination = OUTPUT_DIR / filename
                download.save_as(str(destination))
                print(f"M3U saved to {destination}")

                (OUTPUT_DIR / "ket_qua.txt").write_text(
                    modal_text
                    + f"\nPOST-EXPORT STATUS\n{summary.inner_text().strip()}\n",
                    encoding="utf-8",
                )
            else:
                print("No playable channels reported; skipping M3U export.")

            (OUTPUT_DIR / "browser-errors.txt").write_text(
                "\n".join(errors) if errors else "No browser page errors recorded.\n",
                encoding="utf-8",
            )

        except Exception:
            try:
                page.screenshot(path=str(OUTPUT_DIR / "error.png"), full_page=True)
                (OUTPUT_DIR / "error-page.txt").write_text(
                    page.locator("body").inner_text(), encoding="utf-8"
                )
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
