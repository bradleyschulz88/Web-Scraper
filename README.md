# Web Scraper — Image & Video Collector

A local Python tool that opens a URL in Chromium, loads the whole page (scrolling,
"load more" buttons, lazy images and videos), collects every image/video/audio URL it can see,
and downloads the files. It writes JSON and CSV manifests so you can review what was
found and what failed.

## Quick start (Windows)

Use PowerShell (no `.bat` file needed):

1. Open **Windows PowerShell**.
2. Go to the scraper folder:
   ```powershell
   cd "$env:USERPROFILE\Web-Scraper"
   ```
3. First time only, install the browser and required packages:
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m playwright install chromium
   ```
4. Start the scraper:
   ```powershell
   .\.venv\Scripts\python.exe .\media_url_scraper.py
   ```
5. Paste the page URL. Sign in in the browser if needed, then return to PowerShell
   and press Enter to start scanning.
6. While it scans, press **Enter** in PowerShell or click **Stop & Save All** in
   the small bar at the bottom-right of the browser whenever you are ready. It
   stops looking for more items, saves everything found, and writes the manifests.
   One **Ctrl+C** also asks it to finish saving.
7. Your files appear in `media_output\` (images, videos, audio, and the manifests).


## How it finds media

Web pages hide media in more places than `document.querySelectorAll('img')` can see,
so this tool collects from three independent layers and merges the results:

1. **Network capture** — the browser reports every image/video/audio request it makes,
   including files loaded by JavaScript, files inside iframes, files already removed
   from a virtualised feed, and streams that never appear in the HTML. For images
   and videos up to the capture-size limit, the exact bytes the browser received
   are saved, which also side-steps hot-link protection and short-lived signed URLs.
   Larger videos can still be downloaded in the save-all stage. Video-specific lazy
   attributes and extensionless video URLs are also treated as videos.
2. **DOM scan** — `img`/`video`/`audio`/`source` elements, `srcset` candidates,
   lazy-load attributes (`data-src`, `data-lazy-src`, `data-bg`, …), media links,
   `og:image`/`twitter:image` metadata, `<noscript>` fallbacks, `<template>` blocks,
   elements inside **shadow DOM**, **iframe** content, and background images from
   CSS (including `::before`/`::after` generated content and inline styles).
3. **Rendering pass** — scrolls down *and* back up, scrolls inner scroll containers
   (SPA galleries), clicks "load more"/"show more" buttons, and forces lazy media
   to load until the page stops producing new media. A floating browser control
   shows the current image/video counts and can stop scanning cleanly at any time.

Extra handling:

- `blob:` media (created with `URL.createObjectURL`) is recovered from browser memory
  instead of being listed as undownloadable.
- HLS (`.m3u8`) streams are assembled from their segments into a single file
  (remuxed to `.mp4` when ffmpeg is installed). DASH (`.mpd`) is remuxed with ffmpeg.
- Downloads try the browser's own network stack first, then an in-page fetch, then a
  plain HTTP client — and finally re-load the media in the browser and capture the
  bytes if every direct request is refused.
- Detects `MediaSource`/MSE playback and records its segments.

## Requirements

- Python 3.10+
- Permission to access and download the target media

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

If PowerShell blocks virtual-environment activation, use the environment's Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Optional: install `ffmpeg` and put it on your `PATH` to remux HLS/DASH streams into
`.mp4` files.

## Run

```powershell
python .\media_url_scraper.py
```

1. Paste the full `https://` or `http://` page URL.
2. Use the browser window to sign in if needed and wait for the page to be ready.
3. Return to PowerShell and press Enter to start scanning.
4. Watch the scan and download progress.

Non-interactive use:

```powershell
python .\media_url_scraper.py --url "https://example.com/gallery" --headless
```

Output is written to `media_output/`:

```text
media_output/
├── images/
├── videos/
├── audio/
├── media_manifest.csv
└── media_manifest.json
```

## Command-line options

| Option | Purpose |
| --- | --- |
| `--url URL` | Page to scan (otherwise you are prompted). |
| `--output DIR` | Output folder (default `media_output`). |
| `--headless` | Run without a visible browser window. |
| `--browser-path PATH` | Use a specific Chrome/Chromium binary. |
| `--pause SECONDS` | Delay after each scroll step (default 1.2). |
| `--scroll-step PX` | Pixels per scroll step (default 1000). |
| `--max-scroll-rounds N` | Safety cap on full page passes (default 300). |
| `--max-load-more N` | Maximum number of "load more" clicks (default 250). |
| `--stable-rounds N` | Stop after N passes with nothing new (default 3). |
| `--no-download` | Collect URLs only; do not save files. |
| `--no-network-capture` | Disable network-level capture. |
| `--no-blobs` | Do not recover `blob:` media. |
| `--no-iframes` | Do not scan iframe content. |
| `--no-load-more` | Do not click "load more" buttons. |
| `--no-hls` | Do not assemble HLS streams. |
| `--no-login-wait` | Skip the "press Enter when ready" pause. |

## Configuration

The constants at the top of `media_url_scraper.py` set the defaults, and three
environment variables override the machine-specific ones:

- `SCROLL_STEP_PX`, `PAUSE_AFTER_SCROLL_SECONDS`, `MAX_STABLE_ROUNDS`,
  `MAX_SCROLL_ROUNDS`, `CLICK_LOAD_MORE`, `DOWNLOAD_HLS`, `DOWNLOAD_FILES`,
  capture size limits, and so on.
- `SCRAPER_HEADLESS=1` — run headless by default.
- `SCRAPER_CHROMIUM=/path/to/chrome` — use a specific browser binary.
- `SCRAPER_OUTPUT=/path/to/dir` — default output folder.

## Reading the manifest

Each manifest row is one media URL. Useful columns:

- `source` — which layers found it (`dom`, `network`, `blob`, or a combination).
- `download_status` — what happened: `captured from browser network`,
  `downloaded (browser network stack)`, `captured by re-loading the media in the
  browser`, `downloaded HLS stream (N/M segments)`, `recovered from browser blob`,
  `skipped: a saved URL with the same path already covers this file`, or
  `failed: …` with the reason.
- `note` — context such as `css background-image`, `found in page HTML/JS`,
  `HLS stream manifest`, `stream segment`.
- `frame_url` — set when the media was found inside an iframe.
- `request_failed` — set when the browser's own request for the file did not
  complete (for example `net::ERR_ABORTED`); the download stage retries these.

## Limitations

- Generic scraping cannot guarantee completeness on every website. Galleries that
  require clicking each thumbnail, CAPTCHA-gated content, or media rendered into
  `<canvas>` may still need site-specific handling.
- Encrypted HLS (key-protected) and DRM-protected media are recorded but not decrypted.
- DASH streams need `ffmpeg` on `PATH` to be assembled.
- `MediaSource`/MSE video is recorded along with its segments instead of being
  merged into a playable file.
- Browser sessions that are closed, or pages that only reveal media after a manual
  click that this tool does not know about, can still hide items.
- Some servers deny automated requests entirely; failures are recorded in the manifest.

## Troubleshooting

- Missing items on a slow page: increase `PAUSE_AFTER_SCROLL_SECONDS`, or run again
  with a larger `--pause` and `--max-scroll-rounds`.
- Items show `failed: browser request returned HTTP 403`: the download stage already
  retries through the browser; if it still fails, the URL is probably session- or
  time-bound. Sign in first (keep the visible browser) and re-run.
- Nothing is found at all: the site may block automation, or the media may be inside
  a cross-origin frame that requires interaction.
- Chromium does not start: rerun `python -m playwright install chromium` in the
  active virtual environment.
- Review `media_manifest.csv`, especially `download_status`, `note` and
  `request_failed`, to see what happened to each URL.

## Responsible use

Use this tool only on pages and media you own or are authorised to access. Respect
website terms, copyright, privacy, rate limits, and applicable law. Do not use it to
bypass technical access controls.
