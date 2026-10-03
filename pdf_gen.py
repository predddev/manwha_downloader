"""
pdf_gen.py
~~~~~~~~~~
Ultra High-Speed Parallel Image Downloader & PDF Compiler.
Optimized for 5,000+ manga panels:
- 25 concurrent connection-pooled HTTP streams.
- Direct-to-disk chapter chunking (avoids RAM bloat, zero crash risk).
- Fast PDF merging via pypdf (streams existing PDF byte-streams in seconds).
"""

import os
import io
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
from pypdf import PdfWriter
import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# High-concurrency connection-pooled session all
_session = None

def get_pooled_session(concurrency: int = 25) -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=concurrency + 10,
            pool_maxsize=concurrency + 10,
            max_retries=3,
            pool_block=False
        )
        _session.mount('https://', adapter)
        _session.mount('http://', adapter)
        _session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer': 'https://www.toongod.org/',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        })
    return _session


def download_single_image(url: str, session: requests.Session, retries: int = 3) -> Image.Image | None:
    """Download image binary and convert to standard RGB PIL Image."""
    clean_url = url.strip()
    if not clean_url:
        return None

    for attempt in range(retries):
        try:
            r = session.get(clean_url, timeout=12)
            if r.status_code == 200 and len(r.content) > 500:
                img = Image.open(io.BytesIO(r.content))
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                return img
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.3)
    return None


def download_chapter_images_parallel(image_urls: list, max_workers: int = 25, on_image_done=None) -> list:
    """
    Downloads all images of a chapter using 25 simultaneous streams.
    Preserves exact panel order (page 1, page 2, page 3...).
    """
    session = get_pooled_session(concurrency=max_workers)
    results = [None] * len(image_urls)

    def worker(item):
        idx, url = item
        img = download_single_image(url, session=session)
        if on_image_done:
            on_image_done()
        return idx, img

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(worker, (i, url)) for i, url in enumerate(image_urls)]
        for f in as_completed(futures):
            try:
                idx, img = f.result()
                results[idx] = img
            except Exception:
                pass

    return [img for img in results if img is not None]


def create_chapter_divider(title: str, width: int = 720, height: int = 400) -> Image.Image:
    """Creates a sleek, dark-themed title divider page to separate chapters inside the PDF."""
    img = Image.new('RGB', (width, height), color=(15, 17, 24))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    # Elegant border and accent lines
    draw.rectangle([16, 16, width - 16, height - 16], outline=(42, 45, 64), width=2)
    draw.line([width // 4, height // 2 - 40, 3 * width // 4, height // 2 - 40], fill=(0, 212, 180), width=2)
    draw.text((width // 2, height // 2), title, fill=(240, 240, 245), anchor='mm')
    draw.line([width // 4, height // 2 + 40, 3 * width // 4, height // 2 + 40], fill=(0, 212, 180), width=2)
    return img


def save_images_to_pdf_file(images: list, output_filepath: str, chapter_title: str = None) -> bool:
    """Saves a chapter's images directly to disk as a PDF file, prepending a chapter divider."""
    if not images:
        return False
    try:
        pages = []
        if chapter_title:
            divider = create_chapter_divider(chapter_title)
            pages.append(divider)
        pages.extend(images)

        first = pages[0]
        rest = pages[1:]
        first.save(
            output_filepath,
            format='PDF',
            save_all=True,
            append_images=rest,
            resolution=96
        )
        return True
    except Exception as e:
        logger.error(f"Error saving chapter PDF {output_filepath}: {e}")
        return False


def merge_pdf_chunks(chunk_items: list, final_pdf_path: str) -> bool:
    """
    Merges all chapter PDF chunks into one final master PDF file in seconds.
    Adds clickable PDF outline bookmarks for every chapter and saves a companion .chapters.json file.
    """
    if not chunk_items:
        return False
    try:
        from pypdf import PdfReader
        import json
        writer = PdfWriter()
        page_offset = 0
        chapter_points = []

        for item in chunk_items:
            if isinstance(item, tuple):
                title, p = item
            elif isinstance(item, dict):
                title, p = item.get('title', 'Chapter'), item['path']
            else:
                title, p = os.path.basename(item), item

            if os.path.exists(p) and os.path.getsize(p) > 0:
                reader = PdfReader(p)
                num_pages = len(reader.pages)
                writer.append(p)
                # Add outline bookmark pointing to the start page of this chapter
                writer.add_outline_item(title, page_offset)
                chapter_points.append({
                    'title': title,
                    'pageNum': page_offset + 1,  # 1-indexed for reader
                    'pages': num_pages
                })
                page_offset += num_pages

        with open(final_pdf_path, 'wb') as f:
            writer.write(f)
        writer.close()

        # Save sidecar chapters JSON
        try:
            json_path = final_pdf_path + ".chapters.json"
            with open(json_path, 'w', encoding='utf-8') as jf:
                json.dump(chapter_points, jf, indent=2)
        except Exception:
            pass

        return True
    except Exception as e:
        logger.error(f"Error merging PDFs: {e}")
        return False
