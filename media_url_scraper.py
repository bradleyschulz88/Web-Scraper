"""
Media URL Collector — scrolls a page to trigger lazy loading, then collects and
optionally downloads accessible image/video files.

Use only on pages and media you own or are authorised to access.
This does not bypass DRM, paywalls, access controls, or private-site restrictions.
"""

import asyncio
import csv
import json
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from playwright.async_api import async_playwright

OUTPUT_DIR = Path("media_output")
IMAGE_DIR = OUTPUT_DIR / "images"
VIDEO_DIR = OUTPUT_DIR / "videos"
MANIFEST_JSON = OUTPUT_DIR / "media_manifest.json"
MANIFEST_CSV = OUTPUT_DIR / "media_manifest.csv"

# Scrolling/loading behaviour
SCROLL_STEP_PX = 850
PAUSE_AFTER_SCROLL_SECONDS = 1.2
MAX_STABLE_ROUNDS = 5       # Stop after this many rounds with no new media/page height
MAX_SCROLL_ROUNDS = 500     # Safety limit for very long/infinite feeds
NAVIGATION_TIMEOUT_MS = 60000
DOWNLOAD_FILES = True
HEADLESS = False            # Visible browser lets you sign in manually if needed


COLLECT_MEDIA_JS = r"""
() => {
  const found = [];
  const add = (url, kind, element, note='') => {
    if (!url || typeof url !== 'string') return;
    url = url.trim();
    if (!url || url.startsWith('data:') || url.startsWith('blob:')) {
      if (url.startsWith('blob:')) {
        found.push({url, kind, note: 'blob URL; usually not directly downloadable'});
      }
      return;
    }
    try {
      found.push({
        url: new URL(url, document.baseURI).href,
        kind,
        alt: element?.getAttribute?.('alt') || '',
        title: element?.getAttribute?.('title') || '',
        note
      });
    } catch (_) {}
  };

  document.querySelectorAll('img').forEach(el => {
    add(el.currentSrc, 'image', el, 'currentSrc');
    add(el.src, 'image', el, 'src');
    const srcset = el.getAttribute('srcset');
    if (srcset) {
      srcset.split(',').forEach(part => {
        const candidate = part.trim().split(/\s+/)[0];
        add(candidate, 'image', el, 'srcset');
      });
    }
    ['data-src', 'data-original', 'data-lazy-src', 'data-url'].forEach(attr => {
      add(el.getAttribute(attr), 'image', el, attr);
    });
  });

  document.querySelectorAll('picture source, video source, audio source').forEach(el => {
    const parent = el.parentElement;
    const kind = parent?.tagName?.toLowerCase() === 'picture' ? 'image' : 'video';
    add(el.src || el.getAttribute('src'), kind, el, 'source element');
    const srcset = el.getAttribute('srcset');
    if (srcset && kind === 'image') {
      srcset.split(',').forEach(part => add(part.trim().split(/\s+/)[0], 'image', el, 'source srcset'));
    }
  });

  document.querySelectorAll('video').forEach(el => {
    add(el.currentSrc, 'video', el, 'video currentSrc');
    add(el.src, 'video', el, 'video src');
    add(el.getAttribute('poster'), 'image', el, 'video poster');
    ['data-src', 'data-video-src', 'data-original'].forEach(attr => {
      add(el.getAttribute(attr), 'video', el, attr);
    });
  });

  // Common media links and embedded media URLs.
  document.querySelectorAll('a[href]').forEach(el => {
    const href = el.href || '';
    if (/\.(jpe?g|png|gif|webp|avif|bmp|tiff?|mp4|webm|mov|m4v|m3u8|mpd)(?:$|[?#])/i.test(href)) {
      add(href, /\.(mp4|webm|mov|m4v|m3u8|mpd)(?:$|[?#])/i.test(href) ? 'video' : 'image', el, 'media link');
    }
  });

  return found;
}
"""


def safe_filename(name: str) -> str:
    name = unquote(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:150] or "media"


def extension_for(url: str, content_type: str | None, kind: str) -> str:
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext and len(path_ext) <= 8:
        return path_ext
    ctype = (content_type or "").split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(ctype) if ctype else None
    if ext:
        return ".jpg" if ext == ".jpe" else ext
    return ".mp4" if kind == "video" else ".jpg"


async def collect_all(page):
    seen = {}
    stable_rounds = 0
    previous_height = 0
    previous_count = 0

    for round_no in range(1, MAX_SCROLL_ROUNDS + 1):
        # Nudge lazy-loaded media into view and give the page time to respond.
        await page.evaluate(
            """step => window.scrollBy({top: step, left: 0, behavior: 'instant'})""",
            SCROLL_STEP_PX,
        )
        await page.wait_for_timeout(int(PAUSE_AFTER_SCROLL_SECONDS * 1000))

        # Ask images currently in the DOM to load where possible.
        await page.evaluate("""() => {
          document.querySelectorAll('img[loading="lazy"]').forEach(img => {
            const r = img.getBoundingClientRect();
            if (r.top < window.innerHeight * 2 && r.bottom > -window.innerHeight) {
              img.loading = 'eager';
            }
          });
        }""")

        items = await page.evaluate(COLLECT_MEDIA_JS)
        for item in items:
            url = item.get("url", "")
            if url:
                if url not in seen:
                    seen[url] = item
                elif item.get("note") and not seen[url].get("note"):
                    seen[url]["note"] = item["note"]

        height = await page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
        count = len(seen)
        print(f"\rScroll {round_no}/{MAX_SCROLL_ROUNDS} | unique media: {count} | page height: {height}px", end="", flush=True)

        if height == previous_height and count == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= MAX_STABLE_ROUNDS:
            break

        previous_height, previous_count = height, count

    # Final pass at the bottom and then at the top, to catch elements whose URLs
    # only become available after they have been rendered.
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1500)
    for item in await page.evaluate(COLLECT_MEDIA_JS):
        if item.get("url"):
            seen.setdefault(item["url"], item)

    print()
    return list(seen.values())


def download_media(items, browser_cookies, user_agent, referer):
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Referer": referer,
    })
    for cookie in browser_cookies:
        kwargs = {"name": cookie["name"], "value": cookie["value"]}
        domain = cookie.get("domain")
        path = cookie.get("path")
        if domain:
            kwargs["domain"] = domain
        if path:
            kwargs["path"] = path
        try:
            session.cookies.set(**kwargs)
        except Exception:
            pass

    results = []
    total = len(items)
    for idx, item in enumerate(items, 1):
        url = item["url"]
        kind = item.get("kind", "image")
        record = dict(item)
        record["download_status"] = "not attempted"
        record["local_path"] = ""

        # Blob URLs are generated inside the browser and aren't normal HTTP URLs.
        if url.startswith("blob:") or url.startswith("data:"):
            record["download_status"] = "browser-only URL; not downloaded"
            results.append(record)
            continue

        folder = VIDEO_DIR if kind == "video" else IMAGE_DIR
        parsed_name = Path(urlparse(url).path).name
        base = safe_filename(parsed_name) if parsed_name else f"{kind}_{idx}"
        try:
            with session.get(url, stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                ctype = response.headers.get("Content-Type", "")
                # Avoid saving an HTML login/error page as a media file.
                if "text/html" in ctype.lower():
                    record["download_status"] = "skipped: server returned HTML, not media"
                    results.append(record)
                    continue

                ext = extension_for(url, ctype, kind)
                if not Path(base).suffix:
                    base += ext
                else:
                    # Keep URL extension when present; infer one only when absent.
                    pass
                target = folder / base
                if target.exists():
                    target = folder / f"{target.stem}_{idx}{target.suffix}"

                with open(target, "wb") as out:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            out.write(chunk)

                record["download_status"] = "downloaded"
                record["local_path"] = str(target)
                record["content_type"] = ctype
                results.append(record)
                print(f"[{idx}/{total}] Downloaded {target}")
        except Exception as exc:
            record["download_status"] = f"failed: {type(exc).__name__}: {exc}"
            results.append(record)
            print(f"[{idx}/{total}] Failed: {url} ({exc})")

    return results


async def main():
    url = input("Paste the URL to scan: ").strip()
    if not url.startswith(("http://", "https://")):
        print("Please enter a full http:// or https:// URL.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000},
            accept_downloads=True,
        )
        page = await context.new_page()
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

        print("\nOpening page. If the site requires your normal login, sign in in the browser.")
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"Navigation notice: {exc}")
        await page.wait_for_timeout(2500)

        input("\nWhen the page is ready (and you're signed in if needed), return here and press Enter to start scrolling...")

        # Re-read the current URL in case login redirected to the target page.
        final_url = page.url
        user_agent = await page.evaluate("navigator.userAgent")
        cookies = await context.cookies()

        items = await collect_all(page)
        print(f"\nFound {len(items)} unique media URLs.")

        if DOWNLOAD_FILES:
            results = download_media(items, cookies, user_agent, final_url)
        else:
            results = [dict(item, download_status="URL collected only", local_path="") for item in items]

        with open(MANIFEST_JSON, "w", encoding="utf-8") as f:
            json.dump({
                "requested_url": url,
                "final_url": final_url,
                "created_at_unix": time.time(),
                "media_count": len(results),
                "items": results,
            }, f, ensure_ascii=False, indent=2)

        fields = ["kind", "url", "alt", "title", "note", "download_status", "local_path"]
        with open(MANIFEST_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)

        await browser.close()

    print("\nFinished.")
    print(f"Manifest JSON: {MANIFEST_JSON.resolve()}")
    print(f"Manifest CSV:  {MANIFEST_CSV.resolve()}")
    print(f"Media folders: {IMAGE_DIR.resolve()} and {VIDEO_DIR.resolve()}")
    print("Note: HLS/DASH streams (such as .m3u8/.mpd), DRM-protected media, and browser blob URLs may need a site-specific, authorised workflow.")


if __name__ == "__main__":
    asyncio.run(main())
