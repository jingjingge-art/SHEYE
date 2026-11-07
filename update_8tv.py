#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import sys
import tempfile
from urllib.parse import urlparse

from playwright.async_api import async_playwright

SOURCE_URL = "https://www.shanghaieye.cn/Live"
OUTPUT_FILE = "shanghaieye.txt"
CHANNEL_NAME = "shanghaieye"
GENRE_HEADER = "iptv,#genre#"

WAIT_AFTER_LOAD = 12000
WAIT_AFTER_PLAY = 10000
BROWSER_TIMEOUT = 60000
MAX_RUNTIME = 90

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def is_stream_url(url):
    """Recognize HLS URLs without assuming the stream CDN never changes."""
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme in ("http", "https")
            and parsed.path.lower().endswith(".m3u8")
        )
    except (TypeError, ValueError):
        return False


def safe_url(url):
    """Omit query strings and fragments, which may contain signed tokens."""
    parsed = urlparse(url)
    return f"{parsed.hostname or ''}{parsed.path}"


def is_valid_playlist(data, content_type):
    if not data:
        return False

    text = data.decode("utf-8", errors="replace").lstrip("\ufeff \r\n\t")
    if text.startswith("#EXTM3U"):
        return True

    content_type = (content_type or "").lower()
    return "mpegurl" in content_type and bool(text)


async def collect_stream():
    candidates = {}
    pending_tasks = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
            )
            page = await context.new_page()
            page.set_default_timeout(BROWSER_TIMEOUT)

            def on_request(request):
                url = request.url
                if is_stream_url(url):
                    candidates.setdefault(
                        url, {"valid": False, "status": None}
                    )
                    logging.info("HLS request detected: %s", safe_url(url))

            async def on_response(response):
                url = response.url
                content_type = response.headers.get("content-type", "")
                is_hls_response = "mpegurl" in content_type.lower()

                # The origin/CDN can change, and some HLS endpoints are not
                # named .m3u8. Accept responses explicitly typed as MPEGURL.
                if not is_stream_url(url) and not is_hls_response:
                    return

                item = candidates.setdefault(
                    url, {"valid": False, "status": None}
                )
                item["status"] = response.status

                if not 200 <= response.status < 300:
                    return

                try:
                    body = await response.body()
                    if is_valid_playlist(body, content_type):
                        item["valid"] = True
                        logging.info("Valid HLS playlist detected: %s", safe_url(url))
                except Exception as error:
                    logging.warning(
                        "Could not inspect HLS response %s: %s",
                        safe_url(url),
                        error,
                    )

            def response_listener(response):
                task = asyncio.create_task(on_response(response))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

            page.on("request", on_request)
            page.on("response", response_listener)

            logging.info("Opening ShanghaiEye source page")
            try:
                await page.goto(
                    SOURCE_URL,
                    wait_until="domcontentloaded",
                    timeout=BROWSER_TIMEOUT,
                )
            except Exception as error:
                logging.warning("Page navigation warning: %s", str(error)[:200])

            logging.info("Waiting for player initialization...")
            await page.wait_for_timeout(WAIT_AFTER_LOAD)

            try:
                await page.evaluate("""
                    async () => {
                        for (const video of document.querySelectorAll('video')) {
                            video.muted = true;
                            try { await video.play(); } catch (e) {}
                        }
                    }
                """)
            except Exception as error:
                logging.info("Video autoplay was unavailable: %s", str(error)[:160])

            for selector in (
                ".vjs-big-play-button",
                ".plyr__control--overlaid",
                "button[aria-label*='play' i]",
            ):
                try:
                    button = page.locator(selector).first
                    if await button.is_visible(timeout=1000):
                        await button.click(timeout=2000)
                        logging.info("Player play button clicked")
                        break
                except Exception:
                    continue

            logging.info("Waiting for HLS requests...")
            await page.wait_for_timeout(WAIT_AFTER_PLAY)

            for frame in page.frames:
                logging.info("Frame inspected: %s", frame.url[:180])

            if pending_tasks:
                await asyncio.gather(*tuple(pending_tasks), return_exceptions=True)
        finally:
            await browser.close()

    valid_urls = [url for url, info in candidates.items() if info["valid"]]
    logging.info("Total HLS candidates: %d", len(candidates))
    logging.info("Valid playlists: %d", len(valid_urls))
    if not valid_urls:
        logging.error(
            "No HLS playlist was observed. The source page may be unavailable, "
            "or its player/stream format may have changed."
        )
    return valid_urls


def save_output(stream_url):
    content = f"{GENRE_HEADER}\n{CHANNEL_NAME},{stream_url}\n"
    output_dir = os.path.dirname(os.path.abspath(OUTPUT_FILE))
    fd, temp_path = tempfile.mkstemp(
        dir=output_dir,
        prefix=".shanghaieye_",
        suffix=".tmp",
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        os.replace(temp_path, OUTPUT_FILE)
        logging.info("ShanghaiEye playlist updated successfully")
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


async def main():
    logging.info("=" * 50)
    logging.info("ShanghaiEye IPTV Dynamic Collector Started")
    logging.info("Source: %s", SOURCE_URL)
    logging.info("=" * 50)

    try:
        urls = await asyncio.wait_for(collect_stream(), timeout=MAX_RUNTIME)
        if not urls:
            logging.error("No valid HLS playlist captured; output file was not changed")
            return 1

        stream_url = urls[0]
        logging.info("Selected: %s", safe_url(stream_url))
        save_output(stream_url)
        logging.info("Collection completed successfully")
        return 0
    except asyncio.TimeoutError:
        logging.error("Browser collection timed out")
        return 1
    except Exception as error:
        logging.error("Collection failed: %s", error)
        logging.warning("Existing %s has been preserved", OUTPUT_FILE)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
