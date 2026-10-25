"""
Media URL Collector — finds, downloads and catalogues the images and
videos a web page actually loads.

How it works (three independent collection layers, merged into one manifest):

1. Network capture — the browser itself tells us every image/video/audio file it
   requests, including files loaded by JavaScript, files inside iframes, files
   already removed from a virtualised feed, and HLS/DASH streams that never
   appear in the DOM.  For images (and small videos) the exact bytes the browser
   received are saved, which also side-steps hot-link protection and expiring
   signed URLs.
2. DOM scan — every `img`/`video`/`audio`/`source`, `srcset`, lazy-load
   attribute, media link, `og:`/`twitter:` metadata tag, `<noscript>` fallback,
   `<template>` block, background image in CSS, and every shadow root.
3. Scroll automation — walks the page down *and* back up (plus any inner
   scroll containers), clicks "load more" buttons, and forces lazy images to
   load, until the page stops producing new media.

Optional download stage then saves anything that was not already captured,
using the browser's own network stack first (shares cookies/TLS fingerprint)
and a plain HTTP client as a fallback.  HLS playlists can be assembled into a
single file; DASH manifests are recorded (and remuxed with ffmpeg if present).

Use only on pages and media you own or are authorised to access.
This does not bypass DRM, paywalls, access controls, or private-site restrictions.
"""

import argparse
import asyncio
import base64
import csv
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from playwright.async_api import async_playwright

# --------------------------------------------------------------------------- #
# Configuration — edit these constants, use CLI flags, or set the matching
# environment variable (SCRAPER_HEADLESS / SCRAPER_CHROMIUM / SCRAPER_OUTPUT).
# --------------------------------------------------------------------------- #

OUTPUT_DIR = Path(os.environ.get("SCRAPER_OUTPUT", "media_output"))

# Scrolling / loading behaviour
SCROLL_STEP_PX = 1000
PAUSE_AFTER_SCROLL_SECONDS = 1.2   # Delay after each scroll step
MAX_STABLE_ROUNDS = 3              # Stop after N full passes with nothing new
MAX_SCROLL_ROUNDS = 300            # Safety cap for long/infinite feeds
NAVIGATION_TIMEOUT_MS = 60000

DOWNLOAD_FILES = True
HEADLESS = os.environ.get("SCRAPER_HEADLESS") == "1"   # Visible browser lets you sign in
CHROMIUM_EXECUTABLE = os.environ.get("SCRAPER_CHROMIUM") or None

# Collection layers
CAPTURE_NETWORK_MEDIA = True       # Save media the browser itself loads (recommended)
RECOVER_BLOB_MEDIA = True          # Recover images/videos living in blob: URLs
SCAN_IFRAMES = True                # Scan media inside iframes
CLICK_LOAD_MORE = True             # Click "load more"/"show more" buttons
MAX_LOAD_MORE_CLICKS = 250

# Size caps (bytes) for in-flight capture and blob recovery
MAX_IMAGE_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_VIDEO_CAPTURE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_CAPTURE_BYTES = 32 * 1024 * 1024 * 1024
MAX_BLOB_RECOVERY_BYTES = 16 * 1024 * 1024

# Streams
DOWNLOAD_HLS = True                # Assemble unencrypted HLS (.m3u8) streams
MAX_HLS_SEGMENTS = 20000
REMUX_WITH_FFMPEG = True           # Use ffmpeg (if installed) to make .mp4 files

REQUEST_TIMEOUT_S = (15, 120)      # (connect, read) for the requests fallback
DOWNLOAD_RETRIES = 2

MEDIA_EXT_RE = re.compile(
    r"\.(?:jpe?g|jpe|jfif|pjpeg|png|apng|gif|webp|avif|svg|bmp|tiff?|ico|heic|heif"
    r"|mp4|m4v|webm|ogv|mov|mkv|m3u8|mpd|ts|m4s"
    r"|mp3|m4a|m4b|aac|ogg|oga|opus|wav|flac)(?:$|[?#])",
    re.IGNORECASE,
)
IMAGE_EXT_RE = re.compile(
    r"\.(?:jpe?g|jpe|jfif|pjpeg|png|apng|gif|webp|avif|svg|bmp|tiff?|ico|heic|heif)(?:$|[?#])",
    re.IGNORECASE,
)
VIDEO_EXT_RE = re.compile(
    r"\.(?:mp4|m4v|webm|ogv|mov|mkv|m3u8|mpd|ism)(?:$|[?#])", re.IGNORECASE
)
AUDIO_EXT_RE = re.compile(
    r"\.(?:mp3|m4a|m4b|aac|ogg|oga|opus|wav|flac)(?:$|[?#])", re.IGNORECASE
)
# Stream segments: not media files on their own, but always video in this context.
SEGMENT_EXT_RE = re.compile(r"\.(?:ts|m4s)(?:$|[?#])", re.IGNORECASE)
HLS_CONTENT_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl")
DASH_CONTENT_TYPES = ("application/dash+xml",)

# --------------------------------------------------------------------------- #
# Injected JavaScript
# --------------------------------------------------------------------------- #

# Installed in every frame before any page script runs. Keeps a registry of
# blob: URLs (and the Blob objects behind them) so JS-created media can be
# recovered even though it never exists as a normal URL.
INIT_SCRIPT_JS = r"""
(() => {
  if (window.__scraperHooksInstalled) return;
  window.__scraperHooksInstalled = true;
  const registry = (window.__scraperBlobs = window.__scraperBlobs || {});
  const MAX_KEPT = 400;
  try {
    const origCreate = URL.createObjectURL.bind(URL);
    URL.createObjectURL = function (obj) {
      const url = origCreate(obj);
      try {
        const isMediaSource = typeof MediaSource !== 'undefined' && obj instanceof MediaSource;
        registry[url] = {
          kind: isMediaSource ? 'mediasource' : 'blob',
          size: (obj && obj.size) || 0,
          type: (obj && obj.type) || '',
          created: Date.now(),
          blob: isMediaSource ? null : obj,
        };
        const keys = Object.keys(registry);
        if (keys.length > MAX_KEPT) {
          for (const k of keys) {
            const rec = registry[k];
            if (rec && rec.kind !== 'mediasource' && !rec.pinned && registry[k]) delete registry[k];
            if (Object.keys(registry).length <= MAX_KEPT) break;
          }
        }
      } catch (e) {}
      return url;
    };
    const origRevoke = URL.revokeObjectURL.bind(URL);
    URL.revokeObjectURL = function (u) {
      try { if (registry[u]) registry[u].pinned = true; } catch (e) {}
      return origRevoke(u);
    };
  } catch (e) {}
})();
"""

# Finds scrollable containers (SPA galleries often scroll a div, not the window).
FIND_SCROLLERS_JS = r"""
() => {
  const scrollers = [];
  let nodes;
  try { nodes = document.querySelectorAll('*'); } catch (e) { return []; }
  for (const el of nodes) {
    try {
      if (el.scrollHeight - el.clientHeight > 300 && el.clientHeight > 200) {
        const st = getComputedStyle(el);
        if (/(auto|scroll|overlay)/.test((st.overflowY || '') + (st.overflow || ''))) scrollers.push(el);
      }
    } catch (e) {}
  }
  scrollers.sort((a, b) => (b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth));
  window.__scraperScrollers = scrollers.slice(0, 4);
  return window.__scraperScrollers.map(el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''));
}
"""

SCROLL_BY_JS = r"""
(step) => {
  let moved = 0;
  const before = window.scrollY;
  window.scrollBy(0, step);
  moved += Math.abs(window.scrollY - before);
  for (const el of (window.__scraperScrollers || [])) {
    try {
      const b = el.scrollTop;
      el.scrollTop = Math.min(el.scrollTop + step, el.scrollHeight);
      moved += Math.abs(el.scrollTop - b);
    } catch (e) {}
  }
  return moved;
}
"""

SCROLL_TO_JS = r"""
(where) => {
  const target = where === 'top' ? 0 : 1e9;
  window.scrollTo(0, where === 'top' ? 0 : Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
  for (const el of (window.__scraperScrollers || [])) {
    try { el.scrollTop = where === 'top' ? 0 : el.scrollHeight; } catch (e) {}
  }
  return target;
}
"""

PAGE_HEIGHT_JS = r"""
() => {
  let h = Math.max(document.body ? document.body.scrollHeight : 0,
                   document.documentElement ? document.documentElement.scrollHeight : 0,
                   window.scrollY + window.innerHeight);
  for (const el of (window.__scraperScrollers || [])) {
    try { h = Math.max(h, el.scrollHeight); } catch (e) {}
  }
  return h;
}
"""

# Main DOM sweep. `deep` also walks computed styles/pseudo-elements and scrapes
# raw HTML/JSON for URLs, which catches carousels and JS-configured images.
COLLECT_MEDIA_JS = r"""
(deep) => {
  const out = new Map();
  const frameUrl = location.href;

  const IMAGE_EXT = /\.(?:jpe?g|jpe|jfif|pjpeg|png|apng|gif|webp|avif|svg|bmp|tiff?|ico|heic|heif)(?:$|[?#])/i;
  const VIDEO_EXT = /\.(?:mp4|m4v|webm|ogv|mov|mkv|m3u8|mpd)(?:$|[?#])/i;
  const AUDIO_EXT = /\.(?:mp3|m4a|m4b|aac|ogg|oga|opus|wav|flac)(?:$|[?#])/i;
  const CSS_URL_RE = /url\(\s*(['"]?)([^'")]+)\1\s*\)/gi;

  const kindFromUrl = (u) => VIDEO_EXT.test(u) ? 'video'
                            : AUDIO_EXT.test(u) ? 'audio'
                            : IMAGE_EXT.test(u) ? 'image'
                            : null;

  const push = (rawUrl, kind, el, note) => {
    if (rawUrl == null) return;
    let url = String(rawUrl).trim();
    if (!url) return;
    if (/^(?:data|javascript|mailto|tel|sms|about|chrome|file|ws|wss):/i.test(url)) return;
    if (url.startsWith('blob:')) {
      const key = 'blob::' + url;
      if (!out.has(key)) {
        out.set(key, {url, kind: kind || 'image', note: note || 'blob URL (recovered from browser memory when possible)',
                      alt: '', title: '', source: 'dom', is_blob: true, frame_url: frameUrl});
      }
      return;
    }
    if (url.startsWith('#')) return;
    try { url = new URL(url, document.baseURI).href; } catch (e) { return; }
    if (!/^https?:/i.test(url)) return;
    const k = kind || kindFromUrl(url) || 'image';
    const key = k + '::' + url;
    if (out.has(key)) return;
    out.set(key, {
      url, kind: k,
      alt: (el && typeof el.alt === 'string' ? el.alt : '') || '',
      title: (el && el.getAttribute ? (el.getAttribute('title') || '') : '') || '',
      note: note || '', source: 'dom', frame_url: frameUrl,
    });
  };

  const pushSrcset = (value, kind, el, note) => {
    if (!value || typeof value !== 'string') return;
    value.split(',').forEach(part => {
      const cand = part.trim().split(/\s+/)[0];
      if (cand) push(cand, kind, el, note);
    });
  };

  const pushCssUrls = (value, el, note, forcedKind) => {
    if (!value || typeof value !== 'string' || value === 'none') return;
    CSS_URL_RE.lastIndex = 0;
    let m;
    while ((m = CSS_URL_RE.exec(value)) !== null) push(m[2], forcedKind || null, el, note);
  };

  const LAZY_ATTRS = [
    'data-src', 'data-lazy-src', 'data-original', 'data-original-src', 'data-url',
    'data-image', 'data-image-src', 'data-image-url', 'data-img', 'data-full',
    'data-full-src', 'data-large', 'data-large-src', 'data-hi-res', 'data-hires',
    'data-thumb', 'data-thumbnail', 'data-background', 'data-background-image', 'data-bg',
    'data-lazyload', 'data-defer-src', 'data-echo', 'data-flickity-lazyload',
    'data-video-src', 'data-video', 'data-video-url', 'data-mp4', 'data-hls', 'data-file',
  ];
  const SRCSET_ATTRS = ['data-srcset', 'data-lazy-srcset', 'data-original-set'];

  const pushLazyAttr = (el, name, forcedKind) => {
    const raw = el.getAttribute(name);
    if (!raw) return;
    const val = String(raw).trim().replace(/^['"]|['"]$/g, '');
    if (!val || val === '#' || /^data:/i.test(val)) return;
    if (/srcset|(?:^|-)set$/.test(name) || /\s+\d+[wx]\s*(,|$)/.test(val)) { pushSrcset(val, forcedKind || null, el, name); return; }
    if (/url\(/i.test(val)) { pushCssUrls(val, el, name, forcedKind); return; }
    if (/\s/.test(val)) { val.split(/\s+/).forEach(part => { if (part && !/^(https?|\/|\.\.?\/)/.test(part)) return; push(part, forcedKind || null, el, name); }); return; }
    push(val, forcedKind || null, el, name);
  };

  const MEDIA_LINK_RE = /\.(?:jpe?g|png|gif|webp|avif|svg|bmp|tiff?|ico|mp4|m4v|webm|mov|mkv|m3u8|mpd|mp3|m4a|aac|ogg|opus|wav|flac)(?:$|[?#])/i;

  const scanElement = (el) => {
    if (!el || el.nodeType !== 1) return;
    const tag = (el.tagName || '').toLowerCase();

    if (tag === 'img') {
      push(el.currentSrc, 'image', el, 'currentSrc');
      push(el.getAttribute('src'), 'image', el, 'src');
      pushSrcset(el.getAttribute('srcset'), 'image', el, 'srcset');
      LAZY_ATTRS.concat(SRCSET_ATTRS).forEach(a => { if (el.hasAttribute(a)) pushLazyAttr(el, a); });
      return;
    }

    if (tag === 'source') {
      const parent = el.parentElement ? (el.parentElement.tagName || '').toLowerCase() : '';
      const kind = parent === 'picture' ? 'image' : parent === 'audio' ? 'audio' : 'video';
      push(el.getAttribute('src'), kind, el, 'source element');
      push(el.getAttribute('srcset'), 'image', el, 'source srcset');
      pushSrcset(el.getAttribute('srcset'), 'image', el, 'source srcset');
      LAZY_ATTRS.forEach(a => { if (el.hasAttribute(a)) pushLazyAttr(el, a, kind); });
      return;
    }

    if (tag === 'video' || tag === 'audio') {
      const kind = tag === 'video' ? 'video' : 'audio';
      push(el.currentSrc, kind, el, tag + ' currentSrc');
      push(el.getAttribute('src'), kind, el, tag + ' src');
      push(el.getAttribute('poster'), 'image', el, tag + ' poster');
      LAZY_ATTRS.forEach(a => { if (el.hasAttribute(a)) pushLazyAttr(el, a, kind); });
      if (el.hasAttribute('data-poster')) pushLazyAttr(el, 'data-poster', 'image');
      return;
    }

    if (tag === 'a' || tag === 'area') {
      const href = el.getAttribute('href');
      if (href && MEDIA_LINK_RE.test(href)) push(href, null, el, 'media link');
      return;
    }

    if (tag === 'link') {
      const href = el.getAttribute('href');
      if (!href) return;
      const rel = (el.getAttribute('rel') || '').toLowerCase();
      const as = (el.getAttribute('as') || '').toLowerCase();
      if (as === 'image' || as === 'video' || as === 'audio') { push(href, as === 'image' ? 'image' : as, el, 'link rel=preload as=' + as); return; }
      if (MEDIA_LINK_RE.test(href) || /icon|image_src/.test(rel)) push(href, null, el, 'link rel=' + rel);
      return;
    }

    if (tag === 'meta') {
      const key = (el.getAttribute('property') || el.getAttribute('name') || el.getAttribute('itemprop') || '').toLowerCase();
      if (/og:(image|image:url|image:secure_url|video|video:url|video:secure_url|audio|audio:url)$/.test(key)
          || /twitter:(image|player|player:stream)$/.test(key)
          || key === 'image' || key === 'thumbnailurl' || key === 'contenturl') {
        const content = el.getAttribute('content');
        if (content) push(content, /video|audio/.test(key) ? 'video' : 'image', el, 'meta ' + key);
      }
      return;
    }

    if (tag === 'object') { push(el.getAttribute('data'), null, el, 'object data'); return; }
    if (tag === 'embed') { push(el.getAttribute('src'), null, el, 'embed src'); return; }
    if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'image') {
      push(el.getAttribute('src'), 'image', el, 'input type=image');
      return;
    }
    if (tag === 'image') {  // SVG <image>
      push(el.getAttribute('href') || el.getAttribute('xlink:href'), 'image', el, 'svg image');
      return;
    }
    if (tag === 'iframe' || tag === 'frame' || tag === 'portal') {
      const src = el.getAttribute('src') || el.getAttribute('data-src');
      if (src && !/^about:blank$/i.test(src.trim())) {
        const key = 'embed::' + src;
        if (!out.has(key)) {
          let abs = src;
          try { abs = new URL(src, document.baseURI).href; } catch (e) {}
          out.set(key, {url: abs, kind: 'embed', note: 'embedded frame (its media is scanned separately)',
                        alt: '', title: '', source: 'dom', frame_url: frameUrl});
        }
      }
      return;
    }

    // Inline background/style URLs on any element.
    const style = el.getAttribute && el.getAttribute('style');
    if (style && style.includes('url(')) pushCssUrls(style, el, 'inline style');
  };

  const scanNoscript = (el) => {
    const html = el.textContent || '';
    if (!html || html.indexOf('<') === -1) return;
    try {
      const doc = new DOMParser().parseFromString(html, 'text/html');
      doc.querySelectorAll('*').forEach(node => {
        const t = (node.tagName || '').toLowerCase();
        if (t === 'source') { pushSrcset(node.getAttribute('srcset'), 'image', node, 'noscript srcset'); }
        scanElement(node);
      });
    } catch (e) {}
  };

  const visitedRoots = new WeakSet();
  const visitRoot = (root, depth) => {
    if (!root || depth > 15 || visitedRoots.has(root)) return;
    visitedRoots.add(root);
    let nodes;
    try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of nodes) {
      try { scanElement(el); } catch (e) {}
      const tag = (el.tagName || '').toLowerCase();
      try { if (el.shadowRoot) visitRoot(el.shadowRoot, depth + 1); } catch (e) {}
      try { if (tag === 'template' && el.content) visitRoot(el.content, depth + 1); } catch (e) {}
      if (tag === 'noscript') scanNoscript(el);
    }
  };

  visitRoot(document, 0);

  if (deep) {
    // Computed styles: CSS background/border/mask/content images are invisible
    // to querySelector but very common for hero banners and thumbnails.
    let nodes;
    try { nodes = document.querySelectorAll('*'); } catch (e) { nodes = []; }
    let count = 0;
    for (const el of nodes) {
      if (++count > 8000) break;
      let cs;
      try { cs = getComputedStyle(el); } catch (e) { continue; }
      if (cs) {
        pushCssUrls(cs.backgroundImage, el, 'css background-image');
        pushCssUrls(cs.borderImageSource, el, 'css border-image');
        pushCssUrls(cs.listStyleImage, el, 'css list-style-image');
        if (cs.maskImage && cs.maskImage !== 'none') pushCssUrls(cs.maskImage, el, 'css mask-image');
        if (cs.content && cs.content.indexOf('url(') !== -1) pushCssUrls(cs.content, el, 'css content');
      }
    }
    let pseudoCount = 0;
    for (const el of nodes) {
      if (++pseudoCount > 5000) break;
      for (const pseudo of ['::before', '::after']) {
        let cs;
        try { cs = getComputedStyle(el, pseudo); } catch (e) { continue; }
        if (!cs) continue;
        if (cs.content && cs.content.indexOf('url(') !== -1) pushCssUrls(cs.content, el, 'css ' + pseudo + ' content');
        if (cs.backgroundImage && cs.backgroundImage !== 'none') pushCssUrls(cs.backgroundImage, el, 'css ' + pseudo + ' background');
      }
    }

    // Raw HTML / inline JSON usually contains image paths that only get used
    // when a slider advances or a modal opens.
    let html = '';
    try { html = document.documentElement.outerHTML; } catch (e) {}
    if (html) {
      html = html.replace(/\\\//g, '/').replace(/\\u002[fF]/g, '/').replace(/&amp;/g, '&');
      const re = /(?:https?:\/\/|\/\/|\/)[^\s"'`<>(){}\\]{0,300}?\.(?:jpe?g|jpe|png|gif|webp|avif|svg|bmp|tiff?|ico|mp4|m4v|webm|mov|mkv|m3u8|mpd|mp3|m4a|aac|ogg|opus|wav|flac)(?:\?[^\s"'`<>(){}\\]{0,300})?/gi;
      let m;
      const seen = new Set();
      while ((m = re.exec(html)) !== null) {
        let u = m[0];
        // JavaScript string concatenation leaves truncated queries behind,
        // e.g. "/img.png?cachebust=' + Date.now()" -> drop the broken query.
        u = u.replace(/[?&][^?&=\s]*=$/, '').replace(/[?&]$/, '');
        if (u.startsWith('//')) u = location.protocol + u;
        if (seen.has(u)) continue;
        seen.add(u);
        push(u, null, null, 'found in page HTML/JS');
      }
    }
  }

  return Array.from(out.values());
}
"""

# Turns placeholder/lazy media into real loading media so the network layer
# fetches them (and we capture the bytes).
FORCE_LOAD_JS = r"""
() => {
  let changed = 0;
  const imgAttrs = ['data-src', 'data-lazy-src', 'data-original', 'data-original-src', 'data-url',
                    'data-image', 'data-image-src', 'data-img', 'data-full', 'data-full-src',
                    'data-large', 'data-hi-res', 'data-lazyload', 'data-defer-src', 'data-echo'];
  document.querySelectorAll('img').forEach(img => {
    try { img.loading = 'eager'; img.decoding = 'sync'; } catch (e) {}
    let cur = img.getAttribute('src') || '';
    const placeholder = !cur || /^(data:|about:blank)/i.test(cur) ||
      /(blank|placeholder|spacer|pixel|transparent|loading|lazy|1x1|grey|gray)[^/]*\.(gif|png|jpe?g|svg|webp)/i.test(cur);
    if (placeholder) {
      for (const a of imgAttrs) {
        const v = img.getAttribute(a);
        if (v && !/^data:/i.test(v) && !/\s/.test(v.trim())) { try { img.src = v; changed++; break; } catch (e) {} }
      }
    }
    const ss = img.getAttribute('data-srcset');
    if (ss && !img.getAttribute('srcset')) { try { img.setAttribute('srcset', ss); changed++; } catch (e) {} }
  });
  document.querySelectorAll('video').forEach(v => {
    try { v.preload = 'auto'; } catch (e) {}
    const cur = v.getAttribute('src') || '';
    if (!cur || /^blob:/i.test(cur)) {
      for (const a of ['data-src', 'data-video-src', 'data-video', 'data-file', 'data-mp4', 'data-hls', 'data-url', 'data-video-url']) {
        const val = v.getAttribute(a);
        // Video-specific attributes can contain signed or extensionless URLs.
        if (val && !/^data:/i.test(val)) { try { v.src = val; changed++; break; } catch (e) {} }
      }
    }
    try { if (v.currentSrc || v.getAttribute('src')) v.load(); } catch (e) {}
  });
  document.querySelectorAll('source').forEach(s => {
    const a = s.getAttribute('data-src');
    if (a && !s.getAttribute('src')) { try { s.src = a; changed++; } catch (e) {} }
  });
  document.querySelectorAll('[data-bg],[data-background],[data-background-image]').forEach(el => {
    for (const a of ['data-bg', 'data-background', 'data-background-image']) {
      const v = el.getAttribute(a);
      if (v) {
        let bg = '';
        try { bg = getComputedStyle(el).backgroundImage || ''; } catch (e) {}
        if (bg.indexOf('url(') === -1) { try { el.style.backgroundImage = /url\(/i.test(v) ? v : ('url("' + v + '")'); changed++; } catch (e) {} }
      }
    }
  });
  return changed;
}
"""

CLICK_LOAD_MORE_JS = r"""
() => {
  const words = ['load more', 'show more', 'see more', 'view more', 'more items', 'more results',
                 'load additional', 'show all', 'view all', 'see all', 'load more images'];
  const nodes = document.querySelectorAll('button, a, div[role="button"], span[role="button"], [class*="load-more"], [class*="loadMore"], [class*="show-more"], [data-testid*="more"]');
  for (const el of nodes) {
    let txt = '';
    try { txt = (el.textContent || '').trim().toLowerCase(); } catch (e) { continue; }
    if (!txt || txt.length > 40) continue;
    if (!words.some(w => txt.indexOf(w) !== -1)) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    let r = { width: 0, height: 0 };
    try { r = el.getBoundingClientRect(); } catch (e) {}
    if (r.width <= 0 || r.height <= 0) continue;
    let style;
    try { style = getComputedStyle(el); } catch (e) { continue; }
    if (style && (style.display === 'none' || style.visibility === 'hidden')) continue;
    try { el.click(); return txt; } catch (e) {}
  }
  return null;
}
"""

# Last-resort fetch executed *inside* the page, so the request carries the
# page's cookies, referer and origin (defeats most hot-link protection).
PAGE_FETCH_JS = r"""
async ({url, maxBytes}) => {
  const out = {ok: false, status: 0, type: '', data: '', error: ''};
  try {
    const response = await fetch(url, {credentials: 'include'});
    out.status = response.status;
    out.type = response.headers.get('content-type') || '';
    if (!response.ok) return out;
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength > maxBytes) { out.error = 'too large (' + buffer.byteLength + ' bytes)'; return out; }
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    out.data = btoa(binary);
    out.ok = true;
    return out;
  } catch (error) {
    out.error = String(error);
    return out;
  }
}
"""

# Re-load a piece of media through a real media element: the browser then issues
# a genuine image/video request (correct Sec-Fetch-* headers, referer, cookies)
# and our network listener can capture the bytes.
PAGE_MEDIA_RELOAD_JS = r"""
async ({url, tag, timeoutMs}) => {
  return await new Promise(resolve => {
    try {
      const el = document.createElement(tag);
      el.style.position = 'fixed';
      el.style.left = '-10000px';
      el.style.width = '2px';
      el.style.height = '2px';
      if (tag === 'img') {
        el.referrerPolicy = 'no-referrer-when-downgrade';
      } else {
        el.preload = 'auto';
        el.muted = true;
      }
      let settled = false;
      const finish = (result) => { if (!settled) { settled = true; resolve(result); } };
      el.onload = () => finish('loaded');
      el.oncanplay = () => finish('loaded');
      el.onloadeddata = () => finish('loaded');
      el.onerror = () => finish('error');
      el.src = url;
      document.body.appendChild(el);
      if (tag !== 'img') { try { el.load(); } catch (e) {} }
      setTimeout(() => finish('timeout'), timeoutMs || 20000);
    } catch (error) {
      resolve('exception');
    }
  });
}
"""

# Reads back blobs the page created via URL.createObjectURL().
BLOB_SWEEP_JS = r"""
async (maxBytes) => {
  const registry = window.__scraperBlobs || {};
  const out = [];
  const entries = Object.entries(registry);
  for (const [url, rec] of entries) {
    if (!rec) continue;
    const type = rec.type || '';
    const kind = rec.kind === 'mediasource' ? 'video'
               : type.indexOf('video') === 0 ? 'video'
               : type.indexOf('audio') === 0 ? 'audio' : 'image';
    if (rec.kind === 'mediasource') {
      out.push({url, kind: 'video', note: 'MediaSource (MSE) stream — fetched as segments over the network', data: ''});
      continue;
    }
    const blob = rec.blob;
    if (!blob) { out.push({url, kind, note: 'blob URL (source object released by the page)', data: ''}); continue; }
    if (blob.size > maxBytes) {
      out.push({url, kind, note: 'blob too large to recover (' + blob.size + ' bytes)', data: ''});
      continue;
    }
    const data = await new Promise(resolve => {
      try {
        const fr = new FileReader();
        fr.onload = () => resolve(typeof fr.result === 'string' ? fr.result : '');
        fr.onerror = () => resolve('');
        fr.readAsDataURL(blob);
      } catch (e) { resolve(''); }
    });
    out.push({url, kind, note: data ? 'recovered from browser blob' : 'blob could not be read',
              type, size: blob.size, data: data});
  }
  return out;
}
"""

# Floating in-page control: the count is refreshed as each gallery pass ends,
# and its button requests a clean stop rather than closing the browser.
INSTALL_STOP_UI_JS = r"""
() => {
  if (!document.body) return false;
  let host = document.getElementById('__media_scraper_stop_host');
  if (host) return true;
  host = document.createElement('div');
  host.id = '__media_scraper_stop_host';
  host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647';
  const root = host.attachShadow ? host.attachShadow({mode:'open'}) : host;
  root.innerHTML = `<style>
    .bar{font:14px/1.35 system-ui,-apple-system,Segoe UI,sans-serif;background:#17202b;color:#fff;
      border:1px solid #526273;border-radius:12px;padding:12px 14px;box-shadow:0 6px 24px #0006;
      display:flex;align-items:center;gap:12px;max-width:min(90vw,520px)}
    .count{white-space:nowrap}.stop{font:inherit;font-weight:700;color:#fff;background:#d83c68;border:0;
      border-radius:999px;padding:9px 14px;cursor:pointer;white-space:nowrap}.stop:hover{background:#b92e55}
    .stop:disabled{background:#59636e;cursor:default}.hint{font-size:11px;color:#d2d8df;margin-top:3px}
  </style><div class="bar"><div><div class="count">Found: 0 images · 0 videos</div>
    <div class="hint">Scanning…</div></div><button class="stop" type="button">Stop &amp; Save All</button></div>`;
  const button = root.querySelector('button');
  button.addEventListener('click', () => {
    button.disabled = true;
    button.textContent = 'Stopping…';
    const hint = root.querySelector('.hint');
    if (hint) hint.textContent = 'Saving everything found so far…';
    if (window.__mediaScraperStopAndSave) window.__mediaScraperStopAndSave();
  });
  document.body.appendChild(host);
  return true;
}
"""

UPDATE_STOP_UI_JS = r"""
({images, videos, audio, stopping}) => {
  let host = document.getElementById('__media_scraper_stop_host');
  if (!host) return false;
  const root = host.shadowRoot || host;
  const count = root.querySelector('.count');
  const hint = root.querySelector('.hint');
  const button = root.querySelector('button');
  if (count) count.textContent = `Found: ${images.toLocaleString()} images · ${videos.toLocaleString()} videos`;
  if (hint) hint.textContent = stopping ? 'Saving everything found so far…' : `Scanning…${audio ? ` · ${audio.toLocaleString()} audio` : ''}`;
  if (stopping && button) { button.disabled = true; button.textContent = 'Stopping…'; }
  return true;
}
"""

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_IS_TTY = sys.stdout.isatty()


def log(message: str = "") -> None:
    print(message, flush=True)


def progress(message: str) -> None:
    if _IS_TTY:
        print("\r" + message[:180].ljust(120), end="", flush=True)
    else:
        print(message, flush=True)


def kind_from_url(url: str, default: str = "image") -> str:
    if VIDEO_EXT_RE.search(url) or SEGMENT_EXT_RE.search(url):
        return "video"
    if AUDIO_EXT_RE.search(url):
        return "audio"
    if IMAGE_EXT_RE.search(url):
        return "image"
    return default


def looks_like_media_url(url: str) -> bool:
    return bool(MEDIA_EXT_RE.search(url or ""))


def kind_from_content_type(content_type: str, url: str = "", resource_type: str = "") -> str | None:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not ctype:
        return None
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/") or ctype in ("application/mp4", "application/vnd.ms-sstr+xml"):
        return "video"
    if ctype.startswith("audio/"):
        return "audio"
    if ctype in HLS_CONTENT_TYPES or ctype in DASH_CONTENT_TYPES:
        return "video"
    if ctype in ("application/octet-stream", "binary/octet-stream"):
        if looks_like_media_url(url):
            return kind_from_url(url)
        if resource_type == "media":
            return "video"
    return None


def is_stream_manifest(url: str, content_type: str = "") -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    return bool(re.search(r"\.m3u8(?:$|[?#])", url or "", re.I)) or ctype in HLS_CONTENT_TYPES


def is_dash_manifest(url: str, content_type: str = "") -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    return bool(re.search(r"\.mpd(?:$|[?#])", url or "", re.I)) or ctype in DASH_CONTENT_TYPES


def safe_filename(name: str) -> str:
    name = unquote(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip("._") or "media"
    return name[:150]


def extension_for(url: str, content_type: str | None, kind: str) -> str:
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext and len(path_ext) <= 8:
        return path_ext
    ctype = (content_type or "").split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(ctype) if ctype else None
    if ext:
        return ".jpg" if ext in (".jpe", ".jfif") else ext
    return {"video": ".mp4", "audio": ".mp3"}.get(kind, ".jpg")


def folder_for(kind: str, cfg: "Config") -> Path:
    if kind == "video":
        return cfg.videos_dir
    if kind == "audio":
        return cfg.audio_dir
    return cfg.images_dir


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    output_dir: Path = OUTPUT_DIR
    headless: bool = HEADLESS
    browser_executable: str | None = CHROMIUM_EXECUTABLE
    scroll_step_px: int = SCROLL_STEP_PX
    pause_after_scroll: float = PAUSE_AFTER_SCROLL_SECONDS
    max_stable_rounds: int = MAX_STABLE_ROUNDS
    max_scroll_rounds: int = MAX_SCROLL_ROUNDS
    navigation_timeout_ms: int = NAVIGATION_TIMEOUT_MS
    download_files: bool = DOWNLOAD_FILES
    capture_network_media: bool = CAPTURE_NETWORK_MEDIA
    recover_blob_media: bool = RECOVER_BLOB_MEDIA
    scan_iframes: bool = SCAN_IFRAMES
    click_load_more: bool = CLICK_LOAD_MORE
    max_load_more_clicks: int = MAX_LOAD_MORE_CLICKS
    max_image_capture_bytes: int = MAX_IMAGE_CAPTURE_BYTES
    max_video_capture_bytes: int = MAX_VIDEO_CAPTURE_BYTES
    max_total_capture_bytes: int = MAX_TOTAL_CAPTURE_BYTES
    max_blob_recovery_bytes: int = MAX_BLOB_RECOVERY_BYTES
    download_hls: bool = DOWNLOAD_HLS
    max_hls_segments: int = MAX_HLS_SEGMENTS
    remux_with_ffmpeg: bool = REMUX_WITH_FFMPEG
    download_retries: int = DOWNLOAD_RETRIES
    wait_for_login: bool = True
    settle_seconds: float = 2.5

    @property
    def images_dir(self) -> Path:
        return self.output_dir / "images"

    @property
    def videos_dir(self) -> Path:
        return self.output_dir / "videos"

    @property
    def audio_dir(self) -> Path:
        return self.output_dir / "audio"

    @property
    def manifest_json(self) -> Path:
        return self.output_dir / "media_manifest.json"

    @property
    def manifest_csv(self) -> Path:
        return self.output_dir / "media_manifest.csv"


class MediaState:
    """Union of everything the DOM, the network and the blob sweep reported."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.by_url: dict[str, list[tuple[str, str]]] = {}
        self.pending: int = 0
        self.capture_bytes: int = 0
        self.captured_urls: set[str] = set()
        self.capture_reserved: set[str] = set()   # URLs already being written to disk
        self.capture_tasks: set[asyncio.Task] = set()
        self.used_paths: set[str] = set()

    # -- recording -------------------------------------------------------- #
    def add(self, record: dict) -> tuple[bool, dict]:
        """Merge a record in.

        Returns (was_new, canonical_record) — always update the canonical
        record, because a duplicate URL is merged into the existing entry.
        """
        url = record.get("url") or ""
        if not url:
            return False, record
        expected_kind = record.get("kind") or kind_from_url(url)
        record["kind"] = expected_kind

        key = (expected_kind, url)
        # A URL first guessed as "image" from the DOM may turn out to be a video
        # once the network tells us its content type. Re-home it.
        if record.get("source") == "network" and record.get("content_type"):
            for other_key in list(self.by_url.get(url, [])):
                cur = self.items.get(other_key)
                if cur and other_key[0] != expected_kind and not cur.get("content_type"):
                    del self.items[other_key]
                    self.by_url[url].remove(other_key)
                    cur["kind"] = expected_kind
                    new_key = (expected_kind, url)
                    self.items[new_key] = cur
                    self.by_url.setdefault(url, []).append(new_key)

        if key not in self.items:
            self.items[key] = record
            self.by_url.setdefault(url, []).append(key)
            return True, record

        cur = self.items[key]
        sources = {s for s in (cur.get("source", "") + "+" + record.get("source", "")).split("+") if s}
        cur["source"] = "+".join(sorted(sources))
        for field_name in ("alt", "title", "note", "content_type", "frame_url",
                           "http_status", "page_url", "request_failed"):
            if not cur.get(field_name) and record.get(field_name):
                cur[field_name] = record[field_name]
        return False, cur

    def all(self) -> list[dict]:
        return list(self.items.values())

    def download_candidates(self) -> list[dict]:
        return [r for r in self.items.values() if not r.get("local_path") and r.get("kind") != "embed"]

    # -- files ------------------------------------------------------------ #
    def unique_path(self, folder: Path, base_name: str) -> Path:
        candidate = folder / base_name
        counter = 2
        while str(candidate) in self.used_paths or candidate.exists():
            candidate = folder / f"{Path(base_name).stem}_{counter}{Path(base_name).suffix}"
            counter += 1
        self.used_paths.add(str(candidate))
        return candidate


# --------------------------------------------------------------------------- #
# Browser collection
# --------------------------------------------------------------------------- #


def _body_capture_allowed(state: MediaState, cfg: Config, kind: str, content_length: int | None) -> bool:
    if not cfg.capture_network_media or not cfg.download_files:
        return False
    if state.capture_bytes >= cfg.max_total_capture_bytes:
        return False
    if kind == "image":
        if content_length is not None and content_length > cfg.max_image_capture_bytes:
            return False
        return True
    if content_length is None:
        return False  # don't buffer unbounded streams
    cap = cfg.max_video_capture_bytes if kind == "video" else cfg.max_image_capture_bytes
    return content_length <= cap


def _response_is_complete(status: int, headers: dict) -> bool:
    """A 206 is only worth saving when it happens to cover the whole file."""
    if status == 200:
        return True
    if status != 206:
        return False
    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", headers.get("content-range", "") or "")
    if not match:
        return False
    start, end, total = int(match.group(1)), int(match.group(2)), match.group(3)
    return start == 0 and total != "*" and end == int(total) - 1


async def _capture_response_body(state: MediaState, cfg: Config, record: dict, response) -> None:
    """Save the exact bytes the browser received (avoids 403s and URL expiry)."""
    url = record["url"]
    if record.get("local_path") or url in state.captured_urls:
        return
    # The caller reserves the URL before scheduling this task, so a second
    # response for the same file (page + iframe) cannot write it twice.
    try:
        body = await response.body()
    except Exception as exc:  # body may already be evicted
        record["note"] = (record.get("note", "") + f"; in-flight capture failed ({type(exc).__name__})").strip("; ")
        return
    if not body:
        return
    folder = folder_for(record["kind"], cfg)
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(url).path).name
    ext = extension_for(url, record.get("content_type"), record["kind"])
    if not name or not Path(name).suffix:
        name = f"media_{abs(hash(url)) % 10 ** 8}{ext}"
    target = state.unique_path(folder, safe_filename(name))
    try:
        await asyncio.to_thread(target.write_bytes, body)
    except OSError as exc:
        record["note"] = (record.get("note", "") + f"; could not write capture ({exc})").strip("; ")
        return
    state.capture_bytes += len(body)
    state.captured_urls.add(url)
    record["local_path"] = str(target)
    record["file_size"] = len(body)
    record["download_status"] = "captured from browser network"


def _attach_network_listeners(page, state: MediaState, cfg: Config) -> None:
    """Record every media request/response the browser makes (all frames)."""
    if not cfg.capture_network_media:
        return
    inflight: set = set()

    def on_request(request) -> None:
        try:
            rtype = request.resource_type
            is_media = rtype in ("image", "media") or looks_like_media_url(request.url)
            if not is_media:
                return
            inflight.add(request)
            state.pending += 1
        except Exception:
            pass

    def on_request_finished(request) -> None:
        try:
            if request in inflight:
                inflight.discard(request)
                state.pending = max(0, state.pending - 1)
        except Exception:
            pass

    def on_request_failed(request) -> None:
        try:
            if request in inflight:
                inflight.discard(request)
                state.pending = max(0, state.pending - 1)
            url = request.url
            if looks_like_media_url(url) or request.resource_type in ("image", "media"):
                failure = ""
                try:
                    failure = request.failure or ""
                except Exception:
                    pass
                state.add({
                    "url": url,
                    "kind": kind_from_url(url, "image"),
                    "source": "network",
                    "note": "the browser's own request for this file did not complete",
                    "request_failed": failure,
                    "download_status": "not attempted",
                    "local_path": "",
                })
        except Exception:
            pass

    def on_response(response) -> None:
        try:
            request = response.request
            if request in inflight:
                inflight.discard(request)
                state.pending = max(0, state.pending - 1)

            url = response.url
            if url.startswith("blob:"):
                return  # blob: media is recovered from browser memory, not the network
            headers = {}
            try:
                headers = response.headers or {}
            except Exception:
                pass
            content_type = headers.get("content-type", "")
            resource_type = ""
            try:
                resource_type = response.request.resource_type
            except Exception:
                pass
            kind = kind_from_content_type(content_type, url, resource_type)
            if kind is None and resource_type == "media":
                kind = "video"
            if kind is None and looks_like_media_url(url) and not url.lower().startswith("data:"):
                kind = kind_from_url(url)
            if kind is None:
                return

            status = 0
            try:
                status = response.status
            except Exception:
                pass
            content_length = None
            try:
                if headers.get("content-length"):
                    content_length = int(headers["content-length"])
            except Exception:
                content_length = None

            frame_url = ""
            try:
                frame_url = response.frame.url if response.frame else ""
            except Exception:
                pass

            record = {
                "url": url,
                "kind": kind,
                "source": "network",
                "content_type": (content_type or "").split(";")[0].strip(),
                "http_status": status,
                "frame_url": frame_url,
                "note": "",
                "download_status": "not attempted",
                "local_path": "",
            }
            is_manifest = is_stream_manifest(url, content_type) or is_dash_manifest(url, content_type)
            if is_stream_manifest(url, content_type):
                record["note"] = "HLS stream manifest (assembled in the download stage)"
            elif is_dash_manifest(url, content_type):
                record["note"] = "DASH stream manifest (assembled with ffmpeg when available)"
            elif SEGMENT_EXT_RE.search(url):
                record["note"] = "stream segment (part of an HLS/MSE stream)"
            if status >= 400:
                record["note"] = (record["note"] + f"; HTTP {status}").strip("; ")

            _added, canonical = state.add(record)

            # Save the bytes while the response is still alive. Playlists are
            # left to the stream downloader instead of being saved as text files.
            if (not is_manifest and _response_is_complete(status, headers) and cfg.download_files
                    and url not in state.captured_urls and url not in state.capture_reserved
                    and _body_capture_allowed(state, cfg, kind, content_length)):
                state.capture_reserved.add(url)   # reserve before the first await
                task = asyncio.ensure_future(_capture_response_body(state, cfg, canonical, response))
                state.capture_tasks.add(task)
                task.add_done_callback(state.capture_tasks.discard)
        except Exception:
            pass

    page.on("request", on_request)
    page.on("requestfinished", on_request_finished)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)


async def _wait_for_media_idle(state: MediaState, timeout_s: float = 10.0,
                               stop_event: asyncio.Event | None = None) -> None:
    """Let in-flight image/video requests finish (bounded, stop-aware wait)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while state.pending > 0 and asyncio.get_running_loop().time() < deadline:
        if stop_event and stop_event.is_set():
            break
        await asyncio.sleep(0.25)


async def _collect_frame(frame, state: MediaState, cfg: Config, deep: bool) -> int:
    try:
        items = await frame.evaluate(COLLECT_MEDIA_JS, deep)
    except Exception:
        return 0
    added = 0
    frame_url = frame.url
    for item in items or []:
        if not item.get("url"):
            continue
        item.setdefault("download_status", "not attempted")
        item.setdefault("local_path", "")
        if frame_url and not item.get("frame_url"):
            item["frame_url"] = frame_url
        was_new, _canonical = state.add(item)
        if was_new:
            added += 1
    return added


async def _collect_page(page, state: MediaState, cfg: Config, deep: bool) -> int:
    added = await _collect_frame(page.main_frame, state, cfg, deep)
    if cfg.scan_iframes:
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            added += await _collect_frame(frame, state, cfg, deep)
    return added


async def _force_load_media(page, state: MediaState, cfg: Config) -> int:
    changed = 0
    for frame in ([page.main_frame] + (list(page.frames) if cfg.scan_iframes else [])):
        if frame is None:
            continue
        try:
            changed += await frame.evaluate(FORCE_LOAD_JS)
        except Exception:
            continue
    return changed


async def _click_load_more(page, state: MediaState, cfg: Config,
                          stop_event: asyncio.Event | None = None) -> str | None:
    if not cfg.click_load_more:
        return None
    try:
        clicked = await page.main_frame.evaluate(CLICK_LOAD_MORE_JS)
    except Exception:
        return None
    if clicked:
        await _pause_or_stop(stop_event, max(0.8, cfg.pause_after_scroll))
    return clicked


async def _recover_blobs(page, state: MediaState, cfg: Config) -> int:
    """Read blob: media back out of the browser and save it."""
    if not cfg.recover_blob_media:
        return 0
    recovered = 0
    frames = [page.main_frame] + (list(page.frames) if cfg.scan_iframes else [])
    seen_blob_urls: set[str] = set()
    for frame in frames:
        try:
            results = await frame.evaluate(BLOB_SWEEP_JS, cfg.max_blob_recovery_bytes)
        except Exception:
            continue
        for entry in results or []:
            url = entry.get("url")
            if not url or url in seen_blob_urls:
                continue
            seen_blob_urls.add(url)
            record = {
                "url": url,
                "kind": entry.get("kind") or "image",
                "source": "blob",
                "note": entry.get("note", ""),
                "content_type": entry.get("type", ""),
                "download_status": "not attempted",
                "local_path": "",
                "is_blob": True,
            }
            _added, record = state.add(record)
            data = entry.get("data") or ""
            if not data or not cfg.download_files:
                note = entry.get("note", "") or ""
                if "MediaSource" in note:
                    record["download_status"] = "MSE stream (its segments are fetched over the network)"
                elif "released" in note:
                    record["download_status"] = "browser-only blob URL (page released the data)"
                elif "too large" in note:
                    record["download_status"] = "browser-only blob URL (too large to recover)"
                else:
                    record["download_status"] = "browser-only blob URL; not downloaded"
                continue
            try:
                _header, b64 = data.split(",", 1)
                raw = base64.b64decode(b64)
            except Exception:
                continue
            ext = (mimetypes.guess_extension(entry.get("type", "").split(";")[0])
                   or extension_for(url, entry.get("type"), record["kind"]))
            folder = folder_for(record["kind"], cfg)
            folder.mkdir(parents=True, exist_ok=True)
            target = state.unique_path(folder, f"blob_{record['kind']}_{len(state.captured_urls) + 1}{ext}")
            await asyncio.to_thread(target.write_bytes, raw)
            record["local_path"] = str(target)
            record["file_size"] = len(raw)
            record["download_status"] = "recovered from browser blob"
            state.captured_urls.add(url)
            recovered += 1
    return recovered


async def _pause_or_stop(stop_event: asyncio.Event | None, seconds: float) -> bool:
    """Wait for a short delay, returning early if the user requested a stop."""
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, seconds))
        return True
    except asyncio.TimeoutError:
        return False


async def _update_stop_ui(page, state: MediaState, stopping: bool = False) -> None:
    counts = {kind: 0 for kind in ("image", "video", "audio")}
    for record in state.items.values():
        if record.get("kind") in counts:
            counts[record["kind"]] += 1
    try:
        await page.evaluate(INSTALL_STOP_UI_JS)
        await page.evaluate(UPDATE_STOP_UI_JS, {
            "images": counts["image"], "videos": counts["video"],
            "audio": counts["audio"], "stopping": stopping,
        })
    except Exception:
        # The page may have navigated or closed its document during collection.
        pass


async def _finalize_collection(page, state: MediaState, cfg: Config) -> None:
    """Finish pending captures and recover any page-created blobs before saving."""
    await _wait_for_media_idle(state, 10.0)
    recovered = await _recover_blobs(page, state, cfg)
    if recovered:
        log(f"Recovered {recovered} blob: media item(s) from the browser")
    if state.capture_tasks:
        await asyncio.gather(*list(state.capture_tasks), return_exceptions=True)


async def _scroll_pass(page, state: MediaState, cfg: Config, direction: str,
                       stop_event: asyncio.Event | None = None) -> int:
    """Scroll one full pass down (or back up), collecting at every step."""
    if stop_event and stop_event.is_set():
        return 0
    await page.evaluate(SCROLL_TO_JS, "top" if direction == "down" else "bottom")
    if await _pause_or_stop(stop_event, 0.4):
        return 0
    step = abs(cfg.scroll_step_px) if direction == "down" else -abs(cfg.scroll_step_px)
    moved_total = 0
    stalled_steps = 0
    previous_height = None
    for _step_no in range(1, 5001):  # safety cap for endless feeds
        if stop_event and stop_event.is_set():
            break
        moved = await page.evaluate(SCROLL_BY_JS, step)
        moved = abs(moved or 0)
        moved_total += moved
        if await _pause_or_stop(stop_event, cfg.pause_after_scroll):
            break
        added = await _collect_page(page, state, cfg, deep=False)
        if added:
            await _update_stop_ui(page, state)
        height = await page.evaluate(PAGE_HEIGHT_JS)
        height_stable = previous_height is not None and abs(height - previous_height) < 2
        if moved < 2 and height_stable:
            stalled_steps += 1
            if stalled_steps >= 2:
                break
        else:
            stalled_steps = 0
        previous_height = height
    return moved_total


async def collect_all(page, state: MediaState, cfg: Config,
                      stop_event: asyncio.Event | None = None) -> None:
    """Collect until idle, capped, or the user requests Stop & Save All."""
    try:
        try:
            scrollers = await page.evaluate(FIND_SCROLLERS_JS)
            if scrollers:
                log(f"Inner scroll containers found: {', '.join(scrollers[:4])}")
        except Exception:
            pass

        await _collect_page(page, state, cfg, deep=True)
        log(f"Initial scan: {len(state.items)} media URLs")
        await _update_stop_ui(page, state)

        longest_idle_round = 0
        previous_metrics = None
        load_more_clicks = 0
        for round_no in range(1, cfg.max_scroll_rounds + 1):
            if stop_event and stop_event.is_set():
                break
            before = len(state.items)
            await _scroll_pass(page, state, cfg, "down", stop_event)
            if stop_event and stop_event.is_set():
                break

            clicked = None
            if load_more_clicks < cfg.max_load_more_clicks:
                clicked = await _click_load_more(page, state, cfg, stop_event)
            if clicked:
                load_more_clicks += 1
                await _wait_for_media_idle(state, 6.0, stop_event)
                await _collect_page(page, state, cfg, deep=False)

            if stop_event and stop_event.is_set():
                break
            await _scroll_pass(page, state, cfg, "up", stop_event)
            if stop_event and stop_event.is_set():
                break

            forced = await _force_load_media(page, state, cfg)
            if forced:
                await _wait_for_media_idle(state, 8.0, stop_event)
            await _wait_for_media_idle(state, 6.0, stop_event)
            added = await _collect_page(page, state, cfg, deep=True)

            height = await page.evaluate(PAGE_HEIGHT_JS)
            metrics = (height, len(state.items), state.capture_bytes)
            new_items = len(state.items) - before
            progress(
                f"Round {round_no}/{cfg.max_scroll_rounds} | media: {len(state.items)} "
                f"(+{new_items}) | captured: {len(state.captured_urls)} | "
                f"Load more: {load_more_clicks}/{cfg.max_load_more_clicks} | height: {height}px"
            )
            await _update_stop_ui(page, state)
            if metrics == previous_metrics and added == 0 and new_items == 0:
                longest_idle_round += 1
            else:
                longest_idle_round = 0
            if longest_idle_round >= cfg.max_stable_rounds:
                break
            previous_metrics = metrics
    except asyncio.CancelledError:
        # asyncio.run turns a single Ctrl+C into task cancellation. Consume that
        # request so we can still recover blobs, download the collected media,
        # and write the manifests. A second Ctrl+C can still force-quit.
        if stop_event:
            stop_event.set()
        log("\nStop requested. Finishing the save-all steps…")
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()
    finally:
        await _update_stop_ui(page, state, stopping=bool(stop_event and stop_event.is_set()))
        await _finalize_collection(page, state, cfg)
        if _IS_TTY:
            print()


# --------------------------------------------------------------------------- #
# Download stage
# --------------------------------------------------------------------------- #


def _requests_session(cookies: list[dict], user_agent: str, referer: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Referer": referer})
    for cookie in cookies or []:
        kwargs = {"name": cookie.get("name"), "value": cookie.get("value")}
        if cookie.get("domain"):
            kwargs["domain"] = cookie["domain"]
        if cookie.get("path"):
            kwargs["path"] = cookie["path"]
        if not kwargs["name"]:
            continue
        try:
            session.cookies.set(**kwargs)
        except Exception:
            pass
    return session


def _http_get_sync(session: requests.Session, url: str, headers: dict) -> tuple[bytes | None, str, int, str]:
    """Blocking fallback fetch (run in a worker thread)."""
    try:
        with session.get(url, stream=True, timeout=REQUEST_TIMEOUT_S, headers=headers) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type.lower():
                return None, content_type, response.status_code, "server returned HTML, not media"
            chunks = []
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    chunks.append(chunk)
            if chunks:
                return b"".join(chunks), content_type, response.status_code, "http client"
            return None, content_type, response.status_code, "empty response body"
    except Exception as exc:
        return None, "", 0, f"http client failed ({type(exc).__name__}: {exc})"


MAX_PAGE_FETCH_BYTES = 32 * 1024 * 1024


async def _fetch_via_page(page, url: str) -> tuple[bytes | None, str, int, str]:
    """Fetch a URL from inside the page (page cookies, referer and origin)."""
    if page is None:
        return None, "", 0, "no page available"
    try:
        result = await page.evaluate(PAGE_FETCH_JS, {"url": url, "maxBytes": MAX_PAGE_FETCH_BYTES})
    except Exception as exc:
        return None, "", 0, f"in-page fetch failed ({type(exc).__name__})"
    if not isinstance(result, dict):
        return None, "", 0, "in-page fetch returned no data"
    status = int(result.get("status") or 0)
    content_type = result.get("type") or ""
    if result.get("ok") and result.get("data"):
        try:
            return base64.b64decode(result["data"]), content_type, status, "in-page fetch"
        except Exception:
            return None, content_type, status, "in-page fetch returned undecodable data"
    if result.get("error"):
        return None, content_type, status, f"in-page fetch: {result['error']}"
    return None, content_type, status, f"in-page fetch returned HTTP {status}"


async def _reload_media_in_page(state: MediaState, cfg: Config, page, record: dict) -> bool:
    """Re-request the media through the browser so its bytes can be captured."""
    if page is None or not cfg.capture_network_media:
        return False
    tag = {"image": "img", "video": "video", "audio": "audio"}.get(record.get("kind", "image"), "img")
    try:
        await page.evaluate(PAGE_MEDIA_RELOAD_JS,
                            {"url": record["url"], "tag": tag, "timeoutMs": 20000})
    except Exception:
        return False
    await _wait_for_media_idle(state, 15.0)
    await asyncio.sleep(0.6)
    if record.get("local_path"):
        record["download_status"] = "captured by re-loading the media in the browser"
        return True
    return False


async def _fetch_bytes(context, session, url: str, referer: str, page=None) -> tuple[bytes | None, str, int, str]:
    """Fetch a URL, browser network stack first, plain HTTP client last.

    Order matters: the browser stack re-uses the page's cookies, TLS
    fingerprint and headers, the in-page fetch adds the page's referer and
    origin, and only then does the plain HTTP client get a turn.
    """
    headers = {"Referer": referer, "Accept": "*/*"}
    note = "not attempted"
    try:
        response = await context.request.get(url, headers=headers, timeout=120_000)
        status = response.status
        content_type = (response.headers or {}).get("content-type", "")
        if status in (200, 206):
            body = await response.body()
            if body:
                return body, content_type, status, "browser network stack"
            note = f"browser request returned an empty body (HTTP {status})"
        else:
            note = f"browser request returned HTTP {status}"
    except Exception as exc:
        note = f"browser request failed ({type(exc).__name__})"

    body, content_type, status, page_note = await _fetch_via_page(page, url)
    if body is not None:
        return body, content_type, status, page_note
    note = f"{note}; {page_note}"

    for attempt in range(1, 3):
        body, content_type, status, note = await asyncio.to_thread(_http_get_sync, session, url, headers)
        if body is not None:
            return body, content_type, status, note
        if attempt == 1:
            await asyncio.sleep(1.0)
    return None, "", 0, note


async def _save_bytes(state: MediaState, cfg: Config, record: dict, body: bytes, source: str) -> None:
    folder = folder_for(record["kind"], cfg)
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(record["url"]).path).name
    ext = extension_for(record["url"], record.get("content_type"), record["kind"])
    if not name or not Path(name).suffix:
        name = f"media_{len(state.used_paths) + 1}{ext}"
    target = state.unique_path(folder, safe_filename(name))
    await asyncio.to_thread(target.write_bytes, body)
    record["local_path"] = str(target)
    record["file_size"] = len(body)
    record["download_status"] = f"downloaded ({source})"


def _parse_m3u8(text: str, base_url: str) -> dict:
    """Minimal HLS playlist parser: variants, segments, init segment, encryption."""
    variants: list[tuple[dict, str]] = []
    segments: list[str] = []
    init_segment: str | None = None
    encrypted = False
    pending: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[-1]))
            pending = attrs
            continue
        if line.startswith("#EXT-X-KEY"):
            method = re.search(r"METHOD=([A-Z0-9-]+)", line)
            if method and method.group(1).upper() != "NONE":
                encrypted = True
            continue
        if line.startswith("#EXT-X-MAP"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                init_segment = urljoin(base_url, match.group(1))
            continue
        if line.startswith("#"):
            continue
        absolute = urljoin(base_url, line)
        if pending is not None:
            variants.append((pending, absolute))
            pending = None
        else:
            segments.append(absolute)
    return {"variants": variants, "segments": segments, "init": init_segment, "encrypted": encrypted}


def _variant_score(attrs: dict) -> int:
    bandwidth = re.sub(r"\D", "", attrs.get("BANDWIDTH", "") or "") or "0"
    resolution = re.sub(r"\D", "", attrs.get("RESOLUTION", "") or "") or "0"
    return int(bandwidth) + int(resolution)


async def _download_hls(state: MediaState, cfg: Config, record: dict, context, session,
                        referer: str, page=None) -> None:
    """Fetch an unencrypted HLS stream and join its segments into one file."""
    url = record["url"]
    body, content_type, status, source = await _fetch_bytes(context, session, url, referer, page)
    if body is None:
        record["download_status"] = f"failed: {source}"
        return
    text = body.decode("utf-8", errors="replace")
    playlist = _parse_m3u8(text, url)

    if playlist["encrypted"]:
        record["download_status"] = "skipped: playlist is encrypted (key-protected HLS)"
        record["note"] = (record.get("note", "") + "; encrypted HLS playlist").strip("; ")
        return

    manifest_url = url
    if playlist["variants"]:
        attrs, variant_url = max(playlist["variants"], key=lambda pair: _variant_score(pair[0]))
        log(f"    HLS: using variant {variant_url} ({attrs.get('RESOLUTION', 'unknown')})")
        record["hls_variant_url"] = variant_url
        body, content_type, status, source = await _fetch_bytes(context, session, variant_url, referer, page)
        if body is None:
            record["download_status"] = f"failed to fetch variant: {source}"
            return
        manifest_url = variant_url
        playlist = _parse_m3u8(body.decode("utf-8", errors="replace"), variant_url)
        if playlist["encrypted"]:
            record["download_status"] = "skipped: playlist is encrypted (key-protected HLS)"
            return

    segments = playlist["segments"]
    if not segments:
        record["download_status"] = "no downloadable segments in playlist"
        return
    if len(segments) > cfg.max_hls_segments:
        record["download_status"] = f"skipped: {len(segments)} segments exceeds MAX_HLS_SEGMENTS"
        return

    parts: list[str] = []
    if playlist["init"]:
        parts.append(playlist["init"])
    parts.extend(segments)

    cfg.videos_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(Path(urlparse(manifest_url).path).name or "stream")
    if Path(stem).suffix:
        stem = Path(stem).stem
    target = state.unique_path(cfg.videos_dir, f"{stem}.ts")

    downloaded = 0
    failures = 0
    with open(target, "wb") as out:
        for index, segment_url in enumerate(parts, 1):
            body, _ctype, _status, note = await _fetch_bytes(context, session, segment_url, referer, page)
            if body is None:
                failures += 1
                if failures >= 5:
                    record["download_status"] = f"failed: {failures} consecutive segments could not be fetched"
                    break
                continue
            failures = 0
            out.write(body)
            downloaded += 1
            if index % 10 == 0 or index == len(parts):
                progress(f"    HLS {downloaded}/{len(parts)} segments -> {target.name}")

    if downloaded == 0:
        record["download_status"] = "failed: no HLS segments could be fetched"
        try:
            target.unlink()
        except OSError:
            pass
        return

    record["local_path"] = str(target)
    record["file_size"] = target.stat().st_size
    record["download_status"] = f"downloaded HLS stream ({downloaded}/{len(parts)} segments)"
    log(f"    HLS saved: {target} ({downloaded}/{len(parts)} segments)")

    if cfg.remux_with_ffmpeg and shutil.which("ffmpeg"):
        mp4_path = target.with_suffix(".mp4")
        if await _remux_ts(target, mp4_path):
            record["local_path"] = str(mp4_path)
            record["file_size"] = mp4_path.stat().st_size
            record["download_status"] += "; remuxed to .mp4"


async def _remux_ts(ts_path: Path, mp4_path: Path) -> bool:
    for extra in (["-bsf:a", "aac_adtstoasc"], []):
        command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(ts_path), "-c", "copy", *extra, str(mp4_path)]
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            if await process.wait() == 0 and mp4_path.exists() and mp4_path.stat().st_size > 0:
                try:
                    ts_path.unlink()
                except OSError:
                    pass
                return True
        except Exception:
            return False
    return False


async def _download_dash(state: MediaState, cfg: Config, record: dict) -> None:
    """DASH needs a segment-aware downloader; ffmpeg can do it in one step."""
    if cfg.remux_with_ffmpeg and shutil.which("ffmpeg"):
        cfg.videos_dir.mkdir(parents=True, exist_ok=True)
        name = Path(urlparse(record["url"]).path).name or "stream.mpd"
        target = state.unique_path(cfg.videos_dir, safe_filename(Path(name).stem + ".mp4"))
        command = ["ffmpeg", "-y", "-loglevel", "error", "-i", record["url"], "-c", "copy", str(target)]
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            if await process.wait() == 0 and target.exists() and target.stat().st_size > 0:
                record["local_path"] = str(target)
                record["file_size"] = target.stat().st_size
                record["download_status"] = "downloaded DASH stream (ffmpeg)"
                return
        except Exception:
            pass
    record["download_status"] = "manifest recorded; install ffmpeg to assemble DASH"
    record["note"] = (record.get("note", "") + "; DASH stream not assembled").strip("; ")


async def download_all(state: MediaState, cfg: Config, context, user_agent: str,
                       referer: str, page=None) -> None:
    if not cfg.download_files:
        for record in state.items.values():
            if not record.get("local_path"):
                record["download_status"] = "URL collected only"
        return

    for folder in (cfg.images_dir, cfg.videos_dir, cfg.audio_dir):
        folder.mkdir(parents=True, exist_ok=True)

    cookies = await context.cookies()
    session = _requests_session(cookies, user_agent, referer)
    candidates = state.download_candidates()
    # Download real (DOM/network) URLs before URLs merely scraped out of page
    # HTML/JS, so the dedupe rule below can spot their saved equivalents.
    candidates.sort(key=lambda r: 1 if "found in page HTML/JS" in (r.get("note") or "") else 0)
    total = len(candidates)
    log(f"\nDownloading {total} item(s) not already captured...")

    for index, record in enumerate(candidates, 1):
        url = record["url"]
        kind = record["kind"]
        progress(f"[{index}/{total}] {kind}: {url[:110]}")

        if url.startswith("blob:"):
            record["download_status"] = record.get("download_status") or "browser-only blob URL; not downloaded"
            continue
        if url.startswith("data:"):
            record["download_status"] = "inline data URL; not downloaded"
            continue
        if record.get("kind") == "embed":
            record["download_status"] = "embedded frame (scanned separately for its media)"
            continue
        if ("found in page HTML/JS" in (record.get("note") or "")
                and _saved_sibling_exists(state, record)):
            record["download_status"] = "skipped: a saved URL with the same path already covers this file"
            continue
        if is_stream_manifest(url, record.get("content_type", "")):
            if cfg.download_hls:
                await _download_hls(state, cfg, record, context, session, referer, page)
            else:
                record["download_status"] = "HLS manifest recorded (DOWNLOAD_HLS disabled)"
            continue
        if is_dash_manifest(url, record.get("content_type", "")):
            await _download_dash(state, cfg, record)
            continue

        body, content_type, status, note = await _fetch_bytes(context, session, url, referer, page)
        if body is None:
            # Direct fetches were blocked — ask the browser to load the media
            # again and capture the bytes from the resulting request instead.
            if await _reload_media_in_page(state, cfg, page, record):
                continue
            record["download_status"] = f"failed: {note}"
            continue
        if "text/html" in (content_type or "").lower() and record["kind"] != "embed":
            record["download_status"] = "skipped: server returned HTML, not media"
            continue
        record["content_type"] = (content_type or record.get("content_type", "")).split(";")[0].strip()
        record["http_status"] = status
        await _save_bytes(state, cfg, record, body, note)

    if _IS_TTY:
        print()


# --------------------------------------------------------------------------- #
# Manifests
# --------------------------------------------------------------------------- #


def _saved_sibling_exists(state: MediaState, record: dict) -> bool:
    """True when the same file path was already saved under a different query.

    Only used for URLs scraped out of page HTML/JS, where the query string is
    often a cache-buster (`?v=123`) rather than a different asset.
    """
    path = urlparse(record.get("url", ""))
    stem_path = f"{path.scheme}://{path.netloc}{path.path}"
    for other in state.items.values():
        if other is record or not other.get("local_path"):
            continue
        other_path = urlparse(other.get("url", ""))
        if f"{other_path.scheme}://{other_path.netloc}{other_path.path}" == stem_path:
            return True
    return False


def write_manifests(state: MediaState, cfg: Config, requested_url: str, final_url: str) -> dict:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    records = sorted(state.all(), key=lambda r: (r.get("kind", ""), r.get("url", "")))

    payload = {
        "requested_url": requested_url,
        "final_url": final_url,
        "created_at_unix": time.time(),
        "media_count": len(records),
        "downloaded_count": sum(1 for r in records if r.get("local_path")),
        "by_kind": {
            kind: sum(1 for r in records if r.get("kind") == kind)
            for kind in ("image", "video", "audio", "embed")
        },
        "items": records,
    }
    with open(cfg.manifest_json, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    fields = ["kind", "url", "source", "alt", "title", "content_type", "http_status",
              "file_size", "local_path", "download_status", "frame_url", "request_failed", "note"]
    with open(cfg.manifest_csv, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    return payload


def print_summary(state: MediaState, cfg: Config, payload: dict) -> None:
    records = payload["items"]
    log("\n" + "-" * 72)
    log(f"Media found: {payload['media_count']}  ({payload['by_kind']})")
    log(f"Files saved: {payload['downloaded_count']}")
    failed = [r for r in records if str(r.get("download_status", "")).startswith("failed")]
    if failed:
        log(f"\nItems needing attention ({len(failed)}):")
        for record in failed[:15]:
            log(f"  - [{record.get('kind')}] {record['url'][:100]}")
            log(f"      {record.get('download_status')}")
        if len(failed) > 15:
            log(f"  ... and {len(failed) - 15} more (see media_manifest.csv)")
    log("-" * 72)


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config()
    if args.output:
        cfg.output_dir = Path(args.output)
    if args.headless is not None:
        cfg.headless = args.headless
    if args.browser_path:
        cfg.browser_executable = args.browser_path
    if args.scroll_step:
        cfg.scroll_step_px = args.scroll_step
    if args.pause is not None:
        cfg.pause_after_scroll = args.pause
    if args.max_scroll_rounds:
        cfg.max_scroll_rounds = args.max_scroll_rounds
    if getattr(args, "max_load_more", None) is not None:
        cfg.max_load_more_clicks = args.max_load_more
    if args.stable_rounds:
        cfg.max_stable_rounds = args.stable_rounds
    if args.no_download:
        cfg.download_files = False
    if args.no_blobs:
        cfg.recover_blob_media = False
    if args.no_iframes:
        cfg.scan_iframes = False
    if args.no_load_more:
        cfg.click_load_more = False
    if args.no_hls:
        cfg.download_hls = False
    if args.no_network_capture:
        cfg.capture_network_media = False
    if args.no_login_wait:
        cfg.wait_for_login = False
    return cfg


def _start_console_stop_listener(loop: asyncio.AbstractEventLoop,
                                 stop_event: asyncio.Event) -> None:
    """Let Enter in the console request a clean Stop & Save All."""
    if not sys.stdin.isatty():
        return

    def wait_for_enter() -> None:
        try:
            sys.stdin.readline()
            loop.call_soon_threadsafe(stop_event.set)
        except (EOFError, OSError, RuntimeError, ValueError):
            pass

    log("Press Enter here at any time to stop scanning and save everything found.")
    listener = threading.Thread(target=wait_for_enter, name="scraper-stop-key", daemon=True)
    listener.start()


async def run(url: str, cfg: Config) -> dict:
    state = MediaState()
    for folder in (cfg.images_dir, cfg.videos_dir, cfg.audio_dir):
        folder.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        launch_kwargs: dict = {
            "headless": cfg.headless,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        }
        if cfg.browser_executable:
            launch_kwargs["executable_path"] = cfg.browser_executable
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1000},
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(INIT_SCRIPT_JS)
        page = await context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout_ms)
        _attach_network_listeners(page, state, cfg)

        log(f"\nOpening {url}")
        if not cfg.headless:
            log("If the site requires your normal login, sign in in the browser window.")
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            log(f"Navigation notice: {exc}")
        await page.wait_for_timeout(int(cfg.settle_seconds * 1000))

        if cfg.wait_for_login and not cfg.headless:
            prompt = "\nWhen the page is ready (and you're signed in if needed), press Enter here to start scanning..."
            await asyncio.get_running_loop().run_in_executor(None, input, prompt)

        final_url = page.url
        user_agent = await page.evaluate("navigator.userAgent")
        stop_event = asyncio.Event()

        def request_stop(_source=None):
            if not stop_event.is_set():
                stop_event.set()
                log("\nStop requested. Saving everything found so far…")

        try:
            await page.expose_binding("__mediaScraperStopAndSave", request_stop)
            await page.evaluate(INSTALL_STOP_UI_JS)
        except Exception:
            pass
        _start_console_stop_listener(asyncio.get_running_loop(), stop_event)
        log("Scanning: scrolling, loading more items, capturing images and videos...")

        await collect_all(page, state, cfg, stop_event)

        log(f"\nCollection finished: {len(state.items)} unique media URL(s) "
            f"({len(state.captured_urls)} file(s) captured in flight)")
        for record in state.all():
            if record.get("local_path"):
                record["download_status"] = record.get("download_status") or "captured from browser network"
            elif record.get("kind") == "embed":
                record["download_status"] = "embedded frame (scanned separately for its media)"
            else:
                record["download_status"] = record.get("download_status") or "not attempted"

        await download_all(state, cfg, context, user_agent, final_url, page)
        await browser.close()

    payload = write_manifests(state, cfg, url, final_url)
    print_summary(state, cfg, payload)
    log(f"Manifest JSON: {cfg.manifest_json.resolve()}")
    log(f"Manifest CSV:  {cfg.manifest_csv.resolve()}")
    log(f"Media folders: {cfg.images_dir.resolve()}  {cfg.videos_dir.resolve()}  {cfg.audio_dir.resolve()}")
    log("Note: encrypted HLS, DASH (without ffmpeg) and DRM-protected media are recorded in the "
        "manifest but not assembled. Only collect media you are authorised to access.")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect and download the images/videos a web page loads.")
    parser.add_argument("--url", help="Page URL to scan. Omit to be prompted.")
    parser.add_argument("--output", help="Output folder (default: media_output)")
    parser.add_argument("--headless", action="store_true", default=None, help="Run without a visible browser")
    parser.add_argument("--browser-path", help="Path to a Chrome/Chromium binary to use")
    parser.add_argument("--scroll-step", type=int, help="Pixels per scroll step")
    parser.add_argument("--pause", type=float, help="Seconds to wait after each scroll step")
    parser.add_argument("--max-scroll-rounds", type=int, help="Safety cap on full page passes (default: 300)")
    parser.add_argument("--max-load-more", type=int, help="Maximum number of load-more button clicks (default: 250)")
    parser.add_argument("--stable-rounds", type=int, help="Stop after N passes with no new media")
    parser.add_argument("--no-download", action="store_true", help="Collect URLs only")
    parser.add_argument("--no-blobs", action="store_true", help="Do not recover blob: media")
    parser.add_argument("--no-iframes", action="store_true", help="Do not scan iframes")
    parser.add_argument("--no-load-more", action="store_true", help="Do not click load-more buttons")
    parser.add_argument("--no-hls", action="store_true", help="Do not assemble HLS streams")
    parser.add_argument("--no-network-capture", action="store_true", help="Disable network-level capture")
    parser.add_argument("--no-login-wait", action="store_true", help="Do not pause for manual sign-in")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    cfg = build_config(args)

    url = (args.url or "").strip()
    if not url:
        url = input("Paste the URL to scan: ").strip()
    if not url.startswith(("http://", "https://")):
        log("Please enter a full http:// or https:// URL.")
        return

    try:
        asyncio.run(run(url, cfg))
    except KeyboardInterrupt:
        log("\nInterrupted during saving. Files already saved remain in the output folder; the manifests may be incomplete.")


if __name__ == "__main__":
    main()
