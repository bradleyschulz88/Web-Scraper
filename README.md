# Web Scraper — Image & Video Collector

A local Python tool that opens a URL in Chromium, scrolls down the page to trigger lazy loading, collects image/video URLs, and optionally downloads accessible media. It writes JSON and CSV manifests so failed downloads can be reviewed.

## Features

- Uses Playwright/Chromium for JavaScript-rendered pages.
- Scrolls incrementally and waits for the page/media list to stabilise.
- Collects common image/video elements, `srcset` candidates, video posters, lazy-load attributes, and direct media links.
- Deduplicates URLs.
- Attempts downloads using the browser's user agent, referer, and cookies.
- Saves results to CSV and JSON manifests.
- Opens a visible browser so you can manually sign in to a site you are authorised to access.

## Requirements

- Windows 10/11 (other platforms may work)
- Python 3.10+
- Permission to access and download the target media

## Windows installation

Open PowerShell in this repository folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

If PowerShell blocks virtual-environment activation, run the environment's Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Run

```powershell
python .\media_url_scraper.py
```

1. Paste the full `https://` or `http://` page URL.
2. Use the browser window to sign in if needed and wait for the page to be ready.
3. Return to PowerShell and press Enter.
4. Watch the scroll and download progress.

Output is written to `media_output/`:

```text
media_output/
├── images/
├── videos/
├── media_manifest.csv
└── media_manifest.json
```

## Configuration

Edit the constants near the top of `media_url_scraper.py`:

- `SCROLL_STEP_PX`: pixels to scroll per pass.
- `PAUSE_AFTER_SCROLL_SECONDS`: delay after each scroll to let content load.
- `MAX_STABLE_ROUNDS`: stop after this many passes with no new media or page-height growth.
- `MAX_SCROLL_ROUNDS`: safety cap for long or infinite feeds.
- `DOWNLOAD_FILES`: set to `False` to collect URLs only.
- `HEADLESS`: leave `False` to use a visible browser.

## Limitations

- Generic scraping cannot guarantee completeness on every website. Some galleries require clicking thumbnails, opening modals, or site-specific handling.
- Direct-download URLs may expire or require additional headers/cookies.
- `blob:` URLs are recorded but are not downloaded by this script.
- HLS/DASH streams (`.m3u8`/`.mpd`) are identified but not assembled into video files.
- DRM, paywalls, access controls, and private-site restrictions are not bypassed.
- Some servers may deny automated requests; failures are recorded in the manifest.

## Responsible use

Use this tool only on pages and media you own or are authorised to access. Respect website terms, copyright, privacy, rate limits, and applicable law. Do not use it to bypass technical access controls.

## Troubleshooting

- If the page is still loading content, increase `PAUSE_AFTER_SCROLL_SECONDS`.
- For very long pages, increase `MAX_SCROLL_ROUNDS`.
- If Chromium does not start, rerun `python -m playwright install chromium` in the active virtual environment.
- Review `media_manifest.csv` for each URL's download status and failure message.
