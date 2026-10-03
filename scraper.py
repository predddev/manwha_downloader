"""
scraper.py
~~~~~~~~~~
All page fetching is done through a persistent DrissionPage (Chrome) instance
so Cloudflare Turnstile challenges are solved natively by the real browser.
Image binaries (CDN) are downloaded with plain requests — no CF protection there.
"""

import re
import os
import time
import logging
import threading
from bs4 import BeautifulSoup
from urllib.parse import quote
import requests

logger = logging.getLogger(__name__)

# ── Persistent browser singleton ──────────────────────────────────────────────

_browser_lock = threading.Lock()
_browser      = None
_browser_tab  = None

CHROME_PATHS = [
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
]


def _launch_browser():
    """Create a brand-new ChromiumPage instance and return (browser, tab)."""
    from DrissionPage import ChromiumPage, ChromiumOptions
    co = ChromiumOptions()
    for p in CHROME_PATHS:
        if os.path.exists(p):
            co.set_browser_path(p)
            break
    co.set_argument('--window-position=50,50')
    co.set_argument('--window-size=1280,900')
    co.set_argument('--disable-notifications')
    co.set_argument('--mute-audio')
    browser = ChromiumPage(co)
    tab = browser.latest_tab
    logger.info('[Browser] Chrome started (visible and accessible at position 50,50).')
    return browser, tab


def _get_browser():
    """Return (browser, tab), restarting Chrome if it has crashed."""
    global _browser, _browser_tab
    if _browser is None:
        _browser, _browser_tab = _launch_browser()
    else:
        # Health-check: try a quick attribute access; if it raises, browser is dead
        try:
            _ = _browser_tab.title
        except Exception:
            logger.warning('[Browser] Detected dead browser — restarting…')
            try:
                _browser.quit()
            except Exception:
                pass
            _browser, _browser_tab = _launch_browser()
    return _browser, _browser_tab


def _navigate_and_wait(url: str, timeout: int = 60, scroll: bool = False) -> str:
    """
    Navigate to *url* in the persistent Chrome tab and return the fully-rendered HTML.
    If scroll=True, incrementally scrolls down to trigger lazy loading.
    If scroll=False, returns quickly once the page and CF challenge resolve.
    """
    with _browser_lock:
        _, tab = _get_browser()
        tab.get(url)

        # ── Phase 1: wait for Cloudflare to clear ────────────────────────────
        deadline = time.time() + timeout
        cf_logged = False
        while time.time() < deadline:
            title   = (tab.title or '').strip()
            url_now = tab.url or ''
            if 'Just a moment' in title or 'Cloudflare' in title:
                if not cf_logged:
                    logger.info('[Browser] Cloudflare challenge active — browser window is open for manual verification.')
                    cf_logged = True
            elif (title
                    and 'Just a moment' not in title
                    and title != 'about:blank'
                    and 'neterror' not in url_now
                    and 'chrome-error' not in url_now):
                break
            time.sleep(0.4)

        if not scroll:
            time.sleep(0.6)
            return tab.html

        # ── Phase 2: scroll to bottom in steps to trigger lazy loading ────────
        time.sleep(1.2)
        try:
            total_height = tab.run_js('return document.body.scrollHeight') or 3000
            step = max(total_height // 4, 800)
            current = 0
            while current < total_height:
                current = min(current + step, total_height)
                tab.run_js(f'window.scrollTo(0, {current})')
                time.sleep(0.5)

            # Settle and check if more content loaded
            time.sleep(0.6)
            new_height = tab.run_js('return document.body.scrollHeight') or total_height
            if new_height > total_height:
                tab.run_js(f'window.scrollTo(0, {new_height})')
                time.sleep(0.6)

            tab.run_js('window.scrollTo(0, 0)')
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f'[Browser] Scroll error (non-fatal): {e}')
            time.sleep(0.8)

        return tab.html


# ── Public scraping functions ─────────────────────────────────────────────────

def search_toongod(query: str) -> list:
    """Search ToonGod. Returns list of {title, url, thumb, latest, rating}."""
    query = query.strip()
    # If the user pasted a direct webtoon URL
    if query.startswith('http://') or query.startswith('https://'):
        clean_url = query.rstrip('/') + '/'
        slug = [s for s in clean_url.split('/') if s][-1]
        title = slug.replace('-', ' ').title()
        return [{'title': title, 'url': clean_url, 'thumb': '', 'latest': 'Direct URL', 'rating': ''}]

    search_url = f"https://www.toongod.org/?s={quote(query)}&post_type=wp-manga"
    html = _navigate_and_wait(search_url, scroll=True)
    soup = BeautifulSoup(html, 'html.parser')

    results = []

    # The Madara theme renders results in .c-tabs-item__content rows
    items = soup.select('.c-tabs-item__content')
    if not items:
        # Wider fallback: any block containing a .post-title link
        items = soup.select('.c-tabs-item, .page-item-detail')

    for item in items:
        title_el = (item.select_one('.post-title a')
                    or item.select_one('h3 a')
                    or item.select_one('h4 a'))
        if not title_el:
            continue

        title = title_el.text.strip()
        url   = title_el.get('href', '').strip()

        img_el = item.select_one('.tab-thumb img, img')
        thumb  = ''
        if img_el:
            thumb = (img_el.get('data-src')
                     or img_el.get('src')
                     or img_el.get('data-lazy-src') or '').strip()

        latest_el = item.select_one(
            '.font-meta.chapter a, .font-meta.chapter, .latest-chap')
        latest = latest_el.text.strip() if latest_el else ''

        rating_el = item.select_one('.score')
        rating    = rating_el.text.strip() if rating_el else ''

        results.append({'title': title, 'url': url, 'thumb': thumb,
                        'latest': latest, 'rating': rating})

    return results


def get_manga_info(manga_url: str) -> dict:
    """Fetch manga overview page and return full info + ordered chapter list."""
    html = _navigate_and_wait(manga_url, scroll=True)
    soup = BeautifulSoup(html, 'html.parser')

    # Title — prefer h1 inside .post-title, fall back to .post-title itself
    title_el = (soup.select_one('.post-title h1')
                or soup.select_one('.post-title'))
    raw_title = title_el.get_text(' ', strip=True) if title_el else 'Unknown'
    # Strip trailing genre badges / Manhwa suffix
    title = re.sub(r'\s*(18\+|Manhwa|Manga|Webtoon)\s*', ' ', raw_title,
                   flags=re.IGNORECASE).strip()
    title = re.sub(r'\s+', ' ', title).strip()

    # Thumbnail
    thumb_el = soup.select_one('.summary_image img, .tab-thumb img')
    thumb = ''
    if thumb_el:
        thumb = (thumb_el.get('data-src') or thumb_el.get('src')
                 or thumb_el.get('data-lazy-src') or '').strip()

    # Summary
    summary_el = soup.select_one(
        '.description-summary .summary__content, '
        '.description-summary, .summary__content')
    summary = summary_el.text.strip() if summary_el else ''

    # Genres
    genres = [g.text.strip() for g in soup.select('.genres-content a')]

    # Read First / Read Last buttons
    read_first_url = read_last_url = ''
    for a in soup.select('a'):
        txt  = a.text.strip().lower()
        href = a.get('href', '').strip()
        if 'read first' in txt:
            read_first_url = href
        elif 'read last' in txt:
            read_last_url = href

    # Chapter list (Madara theme: newest first)
    chapter_elements = soup.select('li.wp-manga-chapter a')
    chapters = []
    seen = set()
    for el in chapter_elements:
        chap_url   = el.get('href', '').strip()
        chap_title = el.text.strip()
        if not chap_url or 'chapter' not in chap_url.lower() or chap_url in seen:
            continue
        seen.add(chap_url)

        m = (re.search(r'chapter[ -]+([\d\.]+)', chap_title, re.IGNORECASE)
             or re.search(r'chapter-([\d\.]+)', chap_url, re.IGNORECASE))
        chap_num = float(m.group(1)) if m else 0.0

        chapters.append({'title': chap_title, 'url': chap_url, 'num': chap_num})

    # Ensure chronological order (Ch 1 → Ch N)
    if len(chapters) > 1 and chapters[0]['num'] > chapters[-1]['num']:
        chapters.reverse()

    return {
        'title':          title,
        'url':            manga_url,
        'thumb':          thumb,
        'summary':        summary,
        'genres':         genres,
        'read_first_url': read_first_url or (chapters[0]['url'] if chapters else ''),
        'read_last_url':  read_last_url  or (chapters[-1]['url'] if chapters else ''),
        'total_chapters': len(chapters),
        'chapters':       chapters,
    }


def get_chapter_images(chapter_url: str) -> list:
    """Navigate to a chapter page and return ordered list of image URLs.
    Tries fast load first (~1s); if fewer than 5 images found, triggers full scroll.
    """
    html = _navigate_and_wait(chapter_url, scroll=False)
    soup = BeautifulSoup(html, 'html.parser')

    img_els = soup.select('.reading-content img, .wp-manga-chapter-img, .entry-content img')
    if len(img_els) < 5:
        # Fallback to full scroll if images were dynamically inserted
        html = _navigate_and_wait(chapter_url, scroll=True)
        soup = BeautifulSoup(html, 'html.parser')
        img_els = soup.select('.reading-content img, .wp-manga-chapter-img, .entry-content img')

    image_urls = []
    for img in img_els:
        src = (img.get('data-src') or img.get('src')
               or img.get('data-lazy-src') or '').strip()
        if (src
                and not src.endswith('.gif')
                and 'logo' not in src.lower()
                and 'placeholder' not in src.lower()
                and src not in image_urls):
            image_urls.append(src)
    return image_urls


def download_image_bytes(url: str) -> bytes | None:
    """Download a manga panel from the CDN (no Cloudflare protection)."""
    headers = {
        'User-Agent':  ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/124.0.0.0 Safari/537.36'),
        'Referer': 'https://www.toongod.org/',
    }
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            return r.content
        except Exception as e:
            if attempt == 2:
                logger.warning(f'[IMG] Failed {url}: {e}')
            time.sleep(1)
    return None
