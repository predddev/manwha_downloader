import os
import io
import re
import uuid
import shutil
import threading
import logging
import socket
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory, Response

import scraper
import pdf_gen

# ─── App setup ───────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='static', static_url_path='')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── In-memory job store ──────────────────────────────────────────────────────
# jobs[job_id] = {
#   'status': 'pending'|'running'|'done'|'error',
#   'progress': 0-100,
#   'message': str,
#   'filename': str,
#   'error': str,
# }
_jobs: dict = {}
_jobs_lock = threading.Lock()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
TEMP_DIR = os.path.join(BASE_DIR, 'temp_jobs')
HISTORY_FILE = os.path.join(DOWNLOAD_DIR, "reading_history.json")
_history_lock = threading.Lock()

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)


# ─── History & PDF Resolution Helpers ─────────────────────────────────────────

def load_history_dict():
    """Loads reading_history.json safely without touching any PDF binary files."""
    with _history_lock:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Error reading history JSON: {e}")
    return {}


def save_history_entry(entry):
    """Saves or updates a reading history entry in reading_history.json."""
    if not entry or not isinstance(entry, dict):
        return False
    with _history_lock:
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            history = {}
            if os.path.exists(HISTORY_FILE):
                try:
                    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                        history = json.load(f)
                except Exception:
                    history = {}

            key = entry.get("filename") or entry.get("title")
            if not key:
                return False

            if not entry.get("pdf_path"):
                candidate = os.path.join(DOWNLOAD_DIR, key)
                if os.path.exists(candidate):
                    entry["pdf_path"] = candidate

            history[key] = entry
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"Failed to save history entry: {e}")
            return False


def resolve_pdf_path(param):
    """Safely resolves requested PDF filepath on demand without scanning directories."""
    if not param:
        return None

    decoded = urllib.parse.unquote(param).strip()

    # 1. Direct absolute path check
    if os.path.isabs(decoded) and os.path.exists(decoded) and decoded.lower().endswith(".pdf"):
        return os.path.abspath(decoded)

    # 2. Check directly in DOWNLOAD_DIR by filename
    target = os.path.join(DOWNLOAD_DIR, decoded)
    if os.path.exists(target) and target.lower().endswith(".pdf"):
        return os.path.abspath(target)

    # 3. Check base directory
    target_base = os.path.join(BASE_DIR, decoded)
    if os.path.exists(target_base) and target_base.lower().endswith(".pdf"):
        return os.path.abspath(target_base)

    # 4. Check basename in DOWNLOAD_DIR
    base_name = os.path.basename(decoded)
    target_by_base = os.path.join(DOWNLOAD_DIR, base_name)
    if os.path.exists(target_by_base) and target_by_base.lower().endswith(".pdf"):
        return os.path.abspath(target_by_base)

    return None


def get_local_ip():
    """Attempts to find the local LAN IP for device access on same Wi-Fi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return app.send_static_file('index.html')


@app.route('/reader')
def reader():
    return app.send_static_file('reader.html')


@app.route('/api/stream_pdf')
def api_stream_pdf():
    file_param = request.args.get('file', '')
    pdf_path = resolve_pdf_path(file_param)

    if not pdf_path or not os.path.exists(pdf_path):
        return jsonify({'error': 'PDF file not found'}), 404

    try:
        file_size = os.path.getsize(pdf_path)
        range_header = request.headers.get("Range")

        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1

                def generate_range():
                    with open(pdf_path, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(remaining, 65536))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk

                resp = Response(generate_range(), status=206, mimetype="application/pdf")
                resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
                resp.headers["Content-Length"] = str(length)
                resp.headers["Accept-Ranges"] = "bytes"
                resp.headers["Access-Control-Allow-Origin"] = "*"
                resp.headers["Access-Control-Expose-Headers"] = "Content-Range, Content-Length, Accept-Ranges"
                return resp

        # Non-range request: stream whole file
        def generate_full():
            with open(pdf_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        resp = Response(generate_full(), status=200, mimetype="application/pdf")
        resp.headers["Content-Length"] = str(file_size)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Range, Content-Length, Accept-Ranges"
        return resp
    except Exception as e:
        logger.exception("Error streaming PDF")
        return jsonify({'error': str(e)}), 500


@app.route('/api/chapters')
def api_chapters():
    file_param = request.args.get('file', '')
    pdf_path = resolve_pdf_path(file_param)
    chapters = []

    if pdf_path:
        json_sidecar = pdf_path + ".chapters.json"
        if os.path.exists(json_sidecar):
            try:
                with open(json_sidecar, "r", encoding="utf-8") as jf:
                    chapters = json.load(jf)
            except Exception:
                chapters = []

    resp = jsonify({"status": "ok", "chapters": chapters})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route('/api/history')
def api_history():
    filename = request.args.get('file', '')
    history = load_history_dict()
    entry = None
    if filename:
        entry = history.get(filename) or history.get(os.path.basename(filename))

    resp = jsonify({"status": "ok", "history": history, "entry": entry})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route('/api/update_history', methods=['POST'])
@app.route('/update_history', methods=['POST'])
def api_update_history():
    try:
        data = request.get_json(force=True)
        filename = data.get("filename")
        if filename and not data.get("pdf_path"):
            candidate = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.exists(candidate):
                data["pdf_path"] = candidate

        save_history_entry(data)
        resp = jsonify({"status": "ok"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as e:
        logger.error(f"Error updating history: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/search')
def api_search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Query parameter "q" is required'}), 400
    try:
        results = scraper.search_toongod(query)
        return jsonify({'results': results})
    except Exception as e:
        logger.exception('Search error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/manga')
def api_manga():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL parameter is required'}), 400
    try:
        info = scraper.get_manga_info(url)
        return jsonify(info)
    except Exception as e:
        logger.exception('Manga info error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/download', methods=['POST'])
def api_download():
    """
    Start a background parallel download job.
    Body JSON: {
      "chapters": [{"title": "...", "url": "..."}, ...],
      "manga_title": "..."
    }
    Returns: {"job_id": "..."}
    """
    data = request.get_json(force=True)
    chapters_to_dl = data.get('chapters', [])
    manga_title = data.get('manga_title', 'Manhwa').strip()

    if not chapters_to_dl:
        return jsonify({'error': 'No chapters selected'}), 400

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            'status': 'pending',
            'progress': 0,
            'message': 'Queued…',
            'filename': '',
            'error': ''
        }

    thread = threading.Thread(
        target=_run_download_job,
        args=(job_id, chapters_to_dl, manga_title),
        daemon=True
    )
    thread.start()
    return jsonify({'job_id': job_id})


def _run_download_job(job_id: str, chapters: list, manga_title: str):
    """
    Multi-Agent / Multi-Chunk Pipelined Downloader:
    - Splits chapters into 10-chapter chunks (e.g. 1-10, 11-20, 21-30...).
    - Runs 3 chunk workers concurrently.
    - Each worker downloads chapter images using parallel HTTP connections.
    - Immediately saves each chapter chunk directly to disk and frees RAM.
    - Merges all sequential chunks into one single master PDF.
    """
    job_temp_dir = os.path.join(TEMP_DIR, job_id)
    os.makedirs(job_temp_dir, exist_ok=True)

    def _update(status=None, progress=None, message=None, error=None):
        with _jobs_lock:
            if job_id not in _jobs:
                return
            if status:
                _jobs[job_id]['status'] = status
            if progress is not None:
                _jobs[job_id]['progress'] = progress
            if message:
                _jobs[job_id]['message'] = message
            if error:
                _jobs[job_id]['error'] = error

    _update(status='running', progress=1, message='Initializing 3 parallel chunk agents (10 chapters per chunk)…')

    total_chapters = len(chapters)
    CHUNK_SIZE = 10
    MAX_CONCURRENT_CHUNKS = 3

    # Group chapters into chunks of 10 with their original global indices
    chunks = []
    for start_idx in range(0, total_chapters, CHUNK_SIZE):
        chunk_slice = [
            (i, chapters[i]) for i in range(start_idx, min(start_idx + CHUNK_SIZE, total_chapters))
        ]
        chunk_num = len(chunks) + 1
        chunks.append((chunk_num, chunk_slice))

    import queue
    agent_slot_queue = queue.Queue()
    for slot_id in range(1, MAX_CONCURRENT_CHUNKS + 1):
        agent_slot_queue.put(slot_id)

    progress_lock = threading.Lock()
    completed_chapters = [0]
    total_downloaded_images = [0]
    saved_chapter_files = {}

    def process_chunk(chunk_data):
        chunk_num, items = chunk_data
        agent_id = agent_slot_queue.get()
        chunk_range_str = f"{items[0][1]['title']} → {items[-1][1]['title']}"

        try:
            with progress_lock:
                if 'agents' not in _jobs[job_id]:
                    _jobs[job_id]['agents'] = {}
                _jobs[job_id]['agents'][agent_id] = {
                    'agent_id': agent_id,
                    'chunk_num': chunk_num,
                    'chunk_range': chunk_range_str,
                    'current_chap': 'Starting chunk…',
                    'chap_progress': f'0/{len(items)}',
                    'status': 'Fetching chapter images…',
                    'pct': 5,
                    'active': True
                }

            for item_idx, (global_idx, chap) in enumerate(items):
                chap_display = chap.get('title', f'Chapter {global_idx + 1}')

                with progress_lock:
                    _jobs[job_id]['agents'][agent_id].update({
                        'current_chap': chap_display,
                        'chap_progress': f'{item_idx + 1}/{len(items)}',
                        'status': f'Fetching {chap_display} images…',
                        'pct': int(((item_idx) / len(items)) * 100),
                        'active': True
                    })

                # 1. Scrape image URLs for this chapter
                try:
                    img_urls = scraper.get_chapter_images(chap['url'])
                except Exception as e:
                    logger.warning(f"Error scraping {chap_display}: {e}")
                    img_urls = []

                if not img_urls:
                    with progress_lock:
                        completed_chapters[0] += 1
                    continue

                with progress_lock:
                    _jobs[job_id]['agents'][agent_id].update({
                        'status': f'Downloading {len(img_urls)} images (6 parallel threads)…',
                    })

                # 2. Download chapter images concurrently with 6 worker threads
                images = pdf_gen.download_chapter_images_parallel(
                    img_urls,
                    max_workers=6
                )

                if images:
                    # 3. Save directly to disk as a PDF chunk with chapter separator
                    chap_pdf_file = os.path.join(job_temp_dir, f"chap_{global_idx:04d}.pdf")
                    if pdf_gen.save_images_to_pdf_file(images, chap_pdf_file, chapter_title=chap_display):
                        with progress_lock:
                            saved_chapter_files[global_idx] = {'title': chap_display, 'path': chap_pdf_file}
                            total_downloaded_images[0] += len(images)
                    del images  # Immediately purge RAM

                # 4. Update overall progress atomically
                with progress_lock:
                    completed_chapters[0] += 1
                    done_chaps = completed_chapters[0]
                    done_imgs = total_downloaded_images[0]
                    pct = int((done_chaps / total_chapters) * 92)

                    _jobs[job_id]['agents'][agent_id].update({
                        'status': f'Chapter {chap_display} saved to PDF',
                        'pct': int(((item_idx + 1) / len(items)) * 100),
                    })

                _update(
                    progress=min(pct, 93),
                    message=f'[3 Parallel Agents Active] Completed {done_chaps}/{total_chapters} chapters ({done_imgs} images downloaded)…'
                )

            with progress_lock:
                _jobs[job_id]['agents'][agent_id].update({
                    'status': 'Chunk completed!',
                    'pct': 100,
                    'active': False
                })

        finally:
            agent_slot_queue.put(agent_id)

    try:
        # Run up to 3 chunks simultaneously (e.g. 1-10, 11-20, 21-30 concurrently)
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHUNKS) as chunk_executor:
            futures = [chunk_executor.submit(process_chunk, c) for c in chunks]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    logger.error(f"Chunk execution error: {e}")

        if not saved_chapter_files:
            _update(status='error', message='Download failed: No chapters could be saved.', error='No images downloaded')
            shutil.rmtree(job_temp_dir, ignore_errors=True)
            return

        # Sort all completed chapter PDFs in strict chronological order (0, 1, 2...)
        sorted_indices = sorted(saved_chapter_files.keys())
        ordered_pdf_items = [saved_chapter_files[i] for i in sorted_indices]

        # Merge all chunks into one single master PDF in seconds with chapter bookmarks
        _update(progress=95, message=f'Merging all {len(ordered_pdf_items)} chapter chunks into single Master PDF with bookmarks…')

        first_title = chapters[0]['title']
        last_title = chapters[-1]['title']
        name_part = first_title if len(chapters) == 1 else f"{first_title} to {last_title}"

        clean_manga = _safe_filename(manga_title)
        clean_name = _safe_filename(f"{clean_manga} - {name_part}")
        final_filename = f"{clean_name}.pdf"
        final_filepath = os.path.join(DOWNLOAD_DIR, final_filename)

        success = pdf_gen.merge_pdf_chunks(ordered_pdf_items, final_filepath)

        # Cleanup temporary files
        shutil.rmtree(job_temp_dir, ignore_errors=True)

        if not success or not os.path.exists(final_filepath):
            _update(status='error', message='Failed to merge final master PDF.')
            return

        file_size_mb = os.path.getsize(final_filepath) / (1024 * 1024)

        with _jobs_lock:
            _jobs[job_id]['filename'] = final_filename

        _update(
            status='done',
            progress=100,
            message=f'Complete! All {len(ordered_pdf_items)} chapters ({total_downloaded_images[0]} images) merged into Master PDF ({file_size_mb:.1f} MB).'
        )

        # Register in reading_history.json so it immediately appears in Continue Reading
        try:
            import datetime
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            save_history_entry({
                "filename": final_filename,
                "title": manga_title,
                "last_chapter_title": first_title,
                "last_chapter_index": 1,
                "last_page": 1,
                "total_pages": len(ordered_pdf_items),
                "progress_percent": 0,
                "updated_at": now_iso,
                "pdf_path": final_filepath
            })
        except Exception as e:
            logger.warning(f"Could not register history entry: {e}")

        logger.info(f"[Job {job_id}] Successfully generated {final_filename} ({file_size_mb:.1f} MB)")

    except Exception as e:
        logger.exception(f"[Job {job_id}] Unexpected error")
        shutil.rmtree(job_temp_dir, ignore_errors=True)
        _update(status='error', message=f'Error: {str(e)}', error=str(e))


@app.route('/api/status/<job_id>')
def api_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    agents_list = sorted(
        job.get('agents', {}).values(),
        key=lambda x: x.get('agent_id', 0)
    )

    return jsonify({
        'status':   job['status'],
        'progress': job['progress'],
        'message':  job['message'],
        'filename': job['filename'] if job['status'] == 'done' else '',
        'agents':   agents_list,
    })


@app.route('/api/download_pdf/<job_id>')
def api_download_pdf(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'done' or not job['filename']:
        return jsonify({'error': 'PDF not ready yet'}), 400

    filename = job['filename']
    return send_from_directory(
        DOWNLOAD_DIR,
        filename,
        as_attachment=True,
        download_name=filename
    )


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', '_', name).strip('. ')
    return name[:100]


if __name__ == '__main__':
    port = 5000
    local_ip = get_local_ip()
    print("=" * 65)
    print(" [*] MANHWAGOD DOWNLOADER & WEB READER SERVER STARTED")
    print("=" * 65)
    print(f" [+] Downloader:  http://localhost:{port}")
    print(f" [+] Web Reader:  http://localhost:{port}/reader")
    print(f" [+] Network LAN: http://{local_ip}:{port} (Phone/Tablet)")
    print(" [*] Mode: Direct On-Demand Streaming (Zero Heavy Folder Scanning)")
    print("=" * 65)
    # Pre-warm browser in background
    threading.Thread(target=scraper._get_browser, daemon=True).start()
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)
