#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import os
import re
import sys
import html
import logging
import tempfile

from urllib.parse import urlparse
from playwright.async_api import async_playwright


# ==================================================
# CONFIGURATION
# ==================================================

SOURCE_URL = "https://tvmalay.my/8tv/"

OUTPUT_FILE = "8tv.txt"

CHANNEL_NAME = "8tv"

GENRE_HEADER = "iptv,#genre#"

# Your observed CDN
ALLOWED_CDN_HOSTS = {
    "2bfd741e.b-cdn.net"
}

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
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ==================================================
# URL FILTER
# ==================================================

def is_stream_url(url):

    try:

        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.path.lower().endswith(".m3u8"):
            return False

        host = (parsed.hostname or "").lower()

        if host not in ALLOWED_CDN_HOSTS:
            return False

        return True

    except Exception:
        return False


def safe_url(url):

    parsed = urlparse(url)

    # Never print signed tokens into GitHub logs
    return f"{parsed.hostname}{parsed.path}"


# ==================================================
# PLAYLIST VALIDATION
# ==================================================

def is_valid_playlist(data, content_type):

    if not data:
        return False

    text = data.decode(
        "utf-8",
        errors="replace"
    ).lstrip("\ufeff \r\n\t")

    if text.startswith("#EXTM3U"):
        return True

    if "mpegurl" in content_type.lower():
        return True

    return False


# ==================================================
# BROWSER COLLECTION
# ==================================================

async def collect_stream():

    candidates = {}

    pending_tasks = []

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
                "--no-sandbox"
            ]
        )

        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={
                "width": 1366,
                "height": 768
            },
            java_script_enabled=True
        )

        page = await context.new_page()

        page.set_default_timeout(
            BROWSER_TIMEOUT
        )

        # ------------------------------------------
        # Capture actual network requests
        # ------------------------------------------

        def on_request(request):

            url = request.url

            if is_stream_url(url):

                candidates.setdefault(
                    url,
                    {
                        "valid": False,
                        "status": None,
                        "source": "request"
                    }
                )

                logging.info(
                    "M3U8 request detected: %s",
                    safe_url(url)
                )

        # ------------------------------------------
        # Capture network responses
        # ------------------------------------------

        async def on_response(response):

            url = response.url

            if not is_stream_url(url):
                return

            item = candidates.setdefault(
                url,
                {
                    "valid": False,
                    "status": None,
                    "source": "response"
                }
            )

            item["status"] = response.status

            try:

                if not (200 <= response.status < 300):
                    return

                data = await response.body()

                content_type = (
                    response.headers.get(
                        "content-type",
                        ""
                    )
                )

                if is_valid_playlist(
                    data,
                    content_type
                ):

                    item["valid"] = True

                    logging.info(
                        "Valid HLS playlist detected: %s",
                        safe_url(url)
                    )

            except Exception as error:

                logging.warning(
                    "Response inspection failed: %s",
                    error
                )

        def response_listener(response):

            task = asyncio.create_task(
                on_response(response)
            )

            pending_tasks.append(task)

        page.on("request", on_request)

        page.on("response", response_listener)

        # ------------------------------------------
        # Open source page
        # ------------------------------------------

        logging.info("Opening 8TV source page")

        try:

            await page.goto(
                SOURCE_URL,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT
            )

        except Exception as error:

            logging.warning(
                "Page navigation warning: %s",
                str(error)[:200]
            )

        # ------------------------------------------
        # Wait for JavaScript and iframe loading
        # ------------------------------------------

        logging.info(
            "Waiting for player initialization..."
        )

        await page.wait_for_timeout(
            WAIT_AFTER_LOAD
        )

        # ------------------------------------------
        # Attempt to start video playback
        # ------------------------------------------

        try:

            await page.evaluate("""
                async () => {
                    const videos =
                        document.querySelectorAll('video');

                    for (const video of videos) {
                        video.muted = true;

                        try {
                            await video.play();
                        } catch (e) {}
                    }
                }
            """)

        except Exception:
            pass

        # Try common player play buttons
        selectors = [
            ".vjs-big-play-button",
            ".plyr__control--overlaid",
            "button[aria-label*='play' i]"
        ]

        for selector in selectors:

            try:

                button = page.locator(
                    selector
                ).first

                if await button.is_visible(
                    timeout=1000
                ):

                    await button.click(
                        timeout=2000
                    )

                    logging.info(
                        "Player play button clicked"
                    )

                    break

            except Exception:
                continue

        # ------------------------------------------
        # Wait for CDN requests
        # ------------------------------------------

        logging.info(
            "Waiting for CDN M3U8 requests..."
        )

        await page.wait_for_timeout(
            WAIT_AFTER_PLAY
        )

        # ------------------------------------------
        # Inspect iframe pages
        # ------------------------------------------

        for frame in page.frames:

            logging.info(
                "Frame inspected: %s",
                frame.url[:180]
            )

        # Finish pending response inspections
        if pending_tasks:

            await asyncio.gather(
                *pending_tasks,
                return_exceptions=True
            )

        await browser.close()

    # ----------------------------------------------
    # Return valid playlists only
    # ----------------------------------------------

    valid_urls = [
        url
        for url, info in candidates.items()
        if info["valid"]
    ]

    logging.info(
        "Total M3U8 requests: %d",
        len(candidates)
    )

    logging.info(
        "Valid playlists: %d",
        len(valid_urls)
    )

    return valid_urls


# ==================================================
# SAVE FILE ATOMICALLY
# ==================================================

def save_output(stream_url):

    content = (
        f"{GENRE_HEADER}\n"
        f"{CHANNEL_NAME},{stream_url}\n"
    )

    output_dir = os.path.dirname(
        os.path.abspath(OUTPUT_FILE)
    )

    fd, temp_path = tempfile.mkstemp(
        dir=output_dir,
        prefix=".8tv_",
        suffix=".tmp"
    )

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
            newline="\n"
        ) as file:

            file.write(content)

        os.replace(
            temp_path,
            OUTPUT_FILE
        )

        logging.info(
            "8TV playlist updated successfully"
        )

    except Exception:

        if os.path.exists(temp_path):
            os.remove(temp_path)

        raise


# ==================================================
# MAIN
# ==================================================

async def main():

    logging.info("=" * 50)

    logging.info(
        "8TV IPTV Dynamic Collector Started"
    )

    logging.info(
        "Source: %s",
        SOURCE_URL
    )

    logging.info("=" * 50)

    try:

        urls = await asyncio.wait_for(
            collect_stream(),
            timeout=MAX_RUNTIME
        )

        if not urls:

            raise RuntimeError(
                "No valid M3U8 playlist captured"
            )

        # Select first valid playlist
        stream_url = urls[0]

        logging.info(
            "Selected: %s",
            safe_url(stream_url)
        )

        save_output(stream_url)

        logging.info(
            "Collection completed successfully"
        )

        return 0

    except asyncio.TimeoutError:

        logging.error(
            "Browser collection timed out"
        )

        return 1

    except Exception as error:

        logging.error(
            "Collection failed: %s",
            error
        )

        logging.warning(
            "Old 8tv.txt has been preserved"
        )

        return 1


if __name__ == "__main__":

    sys.exit(
        asyncio.run(main())
        )
