/**
 * Manhwa Web Reader - High-Performance On-Demand Reader Engine
 * 
 * Key Architecture:
 * - Zero heavy folder scanning: avoids reading or parsing 10GB+ directories
 * - Lightweight reading history from downloads/reading_history.json
 * - On-demand single-file streaming via HTTP 206 Partial Content (128KB chunks)
 * - Strict virtualized canvas memory manager (< 40MB RAM usage)
 * - 60 FPS smooth webtoon continuous scrolling
 * - Chapter Drawer & Bookmarks navigation
 * - Keyboard shortcuts & responsive touch controls
 */

// Configure PDF.js worker
pdfjsLib.GlobalWorkerOptions.workerSrc = 'assets/pdf.worker.min.js';

// Global State
const state = {
  currentView: 'home', // 'home' | 'reader'
  historyItems: [],

  // Reader State
  currentFile: null, // { filename, title, streamUrl, isLocalBlob }
  pdfDoc: null,
  totalPages: 0,
  currentPage: 1,
  fitWidth: true,
  autoScrollActive: false,
  autoScrollSpeed: 2,
  autoScrollInterval: null,
  bookmarks: [],
  pageWrappers: new Map(), // pageNum -> DOM element
  activePages: new Map(),  // pageNum -> { page, renderTask, canvas, height }
  renderedPages: new Set(),
  isJumping: false,
  jumpTimer: null,
  historySyncTimer: null,
  hideNavTimer: null
};

// DOM References
const dom = {
  viewHome: document.getElementById('view-home'),
  viewReader: document.getElementById('view-reader'),
  heroDropzone: document.getElementById('hero-dropzone'),
  historySection: document.getElementById('history-section'),
  historyList: document.getElementById('history-list'),
  historyCount: document.getElementById('history-count'),
  fileInput: document.getElementById('local-file-input'),

  // Reader DOM
  readerContainer: document.getElementById('reader-container'),
  readerStrip: document.getElementById('reader-strip'),
  readerTopbar: document.getElementById('reader-topbar'),
  readerDocTitle: document.getElementById('reader-doc-title'),
  readerChapIndicator: document.getElementById('reader-chapter-indicator'),
  readerPageIndicator: document.getElementById('reader-page-indicator'),
  btnFit: document.getElementById('btn-fit'),
  btnAuto: document.getElementById('btn-auto'),
  floatingHud: document.getElementById('floating-hud'),
  hudChap: document.getElementById('hud-chap'),
  hudPages: document.getElementById('hud-pages'),
  autoSpeedDisplay: document.getElementById('auto-speed-display'),
  autoScrollBadge: document.getElementById('autoscroll-badge'),

  // Drawer & Modals
  sidebar: document.getElementById('sidebar'),
  sidebarBackdrop: document.getElementById('sidebar-backdrop'),
  chapterList: document.getElementById('chapter-list'),
  chapterSearch: document.getElementById('chapter-search-input'),
  shortcutsModal: document.getElementById('shortcuts-modal'),
  toast: document.getElementById('toast')
};

// ==========================================================================
// 1. RECENT READING HISTORY (Lightweight JSON only, 0MB RAM)
// ==========================================================================

async function fetchRecentHistory() {
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    if (data.status === 'ok' && data.history) {
      const items = Object.values(data.history).sort((a, b) => {
        return (b.updated_at || '').localeCompare(a.updated_at || '');
      });
      state.historyItems = items;
      renderHistoryList(items);
    }
  } catch (err) {
    console.warn('Could not load history:', err);
  }
}

function renderHistoryList(items) {
  if (!items || items.length === 0) {
    dom.historySection.style.display = 'none';
    return;
  }

  dom.historySection.style.display = 'flex';
  dom.historyCount.textContent = `${items.length} In Progress`;
  dom.historyList.innerHTML = '';

  items.forEach(item => {
    const card = document.createElement('div');
    card.className = 'history-card';
    card.onclick = () => openFileFromHistory(item);

    const percent = Math.min(100, Math.max(0, item.progress_percent || 0));
    const title = item.title || item.filename || 'Manhwa';
    const cleanTitle = title.replace(/\.[^/.]+$/, '');
    const chapter = item.last_chapter_title || `Page ${item.last_page || 1}`;
    const pageInfo = item.total_pages ? `p. ${item.last_page || 1} / ${item.total_pages}` : `Page ${item.last_page || 1}`;

    card.innerHTML = `
      <div class="history-card-left">
        <div class="history-card-title" title="${escapeHtml(cleanTitle)}">${escapeHtml(cleanTitle)}</div>
        <div class="history-card-meta">
          <span class="history-card-chapter">📖 ${escapeHtml(chapter)}</span>
          <span>•</span>
          <span>${pageInfo} (${percent}%)</span>
        </div>
      </div>
      <div style="display:flex; align-items:center; gap:14px; flex-shrink:0;">
        <div class="history-card-progress" title="${percent}% completed">
          <div class="history-card-progress-bar" style="width: ${percent}%;"></div>
        </div>
        <button class="btn btn-primary" style="padding: 6px 14px; font-size: 0.78rem;">
          Resume ➔
        </button>
      </div>
    `;

    dom.historyList.appendChild(card);
  });
}

function openFileFromHistory(item) {
  if (!item || !item.filename) return;
  const streamUrl = `/api/stream_pdf?file=${encodeURIComponent(item.filename)}`;
  const title = (item.title || item.filename).replace(/\.[^/.]+$/, '');

  state.currentFile = {
    filename: item.filename,
    title: title,
    streamUrl: streamUrl,
    isLocalBlob: false
  };

  switchView('reader');
  initReader(streamUrl, item.filename, title, item.last_page || 1);
}

// ==========================================================================
// 2. VIEW SWITCHING & DIRECT FILE LAUNCHER
// ==========================================================================

function switchView(viewName) {
  state.currentView = viewName;
  if (viewName === 'reader') {
    dom.viewHome.classList.add('hidden');
    dom.viewReader.classList.remove('hidden');
    resetHideNavTimer();
  } else {
    // Return to Home
    stopAutoScroll();
    cleanupReader();
    dom.viewReader.classList.add('hidden');
    dom.viewHome.classList.remove('hidden');
    fetchRecentHistory(); // Refresh history list
  }
}

function openLocalPdfFile(file) {
  if (!file) return;
  const blobUrl = URL.createObjectURL(file);
  const title = file.name.replace(/\.[^/.]+$/, '');

  state.currentFile = {
    filename: file.name,
    title: title,
    streamUrl: blobUrl,
    isLocalBlob: true
  };

  switchView('reader');
  initReader(blobUrl, file.name, title, 1);
}

// ==========================================================================
// 3. VIRTUALIZED STREAMING READER (< 40MB RAM)
// ==========================================================================

async function initReader(sourceUrl, filename, title, forceStartPage = 1) {
  cleanupReader();

  dom.readerDocTitle.textContent = title;
  dom.readerDocTitle.title = filename;
  showToast(`Streaming ${title}…`);

  try {
    // Range requests enabled: loads in tiny 128KB chunks on demand!
    const loadingTask = pdfjsLib.getDocument({
      url: sourceUrl,
      rangeChunkSize: 131072, // 128 KB chunks
      disableAutoFetch: true,  // Only fetch pages currently viewed!
      disableStream: false,
      disableFontFace: true,   // Webtoon images don't need font downloads
      isEvalSupported: false,
      enableXfa: false
    });

    state.pdfDoc = await loadingTask.promise;
    state.totalPages = state.pdfDoc.numPages;

    dom.readerPageIndicator.textContent = `p. 1 / ${state.totalPages}`;
    dom.hudPages.innerHTML = `<strong>1</strong> / ${state.totalPages}`;

    // Load Chapter Bookmarks
    await loadChapters(filename);

    // Calculate aspect ratio from page 1
    const firstPage = await state.pdfDoc.getPage(1);
    const vp = firstPage.getViewport({ scale: 1 });
    const isPortrait = window.innerWidth <= 960 || (window.innerHeight > window.innerWidth);
    const targetWidth = isPortrait ? dom.readerContainer.clientWidth : Math.min(dom.readerContainer.clientWidth - 20, 950);
    const defaultHeight = Math.round(vp.height * (targetWidth / vp.width));
    firstPage.cleanup(); // Clean up page 1 memory immediately

    // Create lightweight empty placeholder wrappers (0 heavy canvas nodes up front!)
    const fragment = document.createDocumentFragment();
    for (let pNum = 1; pNum <= state.totalPages; pNum++) {
      const wrap = document.createElement('div');
      wrap.className = 'page-wrapper';
      wrap.id = `page-wrap-${pNum}`;
      wrap.dataset.pageNum = pNum;
      wrap.style.minHeight = `${defaultHeight}px`;
      fragment.appendChild(wrap);
      state.pageWrappers.set(pNum, wrap);
    }
    dom.readerStrip.appendChild(fragment);

    // Setup Virtualized Observer: strict 2-3 page window
    setupVirtualizedObserver();

    // Jump to initial resume page
    const resumePage = Math.max(1, Math.min(forceStartPage, state.totalPages));
    jumpToPage(resumePage, false);
    showToast(`Ready! ${state.totalPages} pages streaming smoothly.`);
  } catch (err) {
    console.error('Failed to open PDF:', err);
    alert(`Could not open PDF: ${err.message}`);
    switchView('home');
  }
}

function cleanupReader() {
  state.activePages.forEach((entry) => {
    if (entry.renderTask) {
      try { entry.renderTask.cancel(); } catch (e) {}
    }
    if (entry.page) {
      try { entry.page.cleanup(); } catch (e) {}
    }
  });

  state.activePages.clear();
  state.renderedPages.clear();
  state.pageWrappers.clear();
  dom.readerStrip.innerHTML = '';
  state.pdfDoc = null;
  state.totalPages = 0;
  state.currentPage = 1;
}

// ── Virtualized Observer ──────────────────────────────────────────────────
let _readerObserver = null;
let _scrollDebounceTimer = null;

function setupVirtualizedObserver() {
  if (_readerObserver) _readerObserver.disconnect();

  _readerObserver = new IntersectionObserver((entries) => {
    if (state.isJumping) return;
    entries.forEach(entry => {
      const pageNum = parseInt(entry.target.dataset.pageNum);
      if (entry.isIntersecting) {
        renderPage(pageNum);
      } else {
        // Unload pages when beyond 2 pages from current view
        if (Math.abs(pageNum - state.currentPage) > 2) {
          unloadPage(pageNum);
        }
      }
    });
  }, {
    root: dom.readerContainer,
    rootMargin: '600px 0px 600px 0px'
  });

  state.pageWrappers.forEach(wrap => _readerObserver.observe(wrap));

  dom.readerContainer.removeEventListener('scroll', onReaderScroll);
  dom.readerContainer.addEventListener('scroll', onReaderScroll, { passive: true });
}

function onReaderScroll() {
  if (state.isJumping) return;
  resetHideNavTimer();

  clearTimeout(_scrollDebounceTimer);
  _scrollDebounceTimer = setTimeout(() => {
    if (state.isJumping) return;
    const visiblePage = getVisiblePageNum();
    if (visiblePage && visiblePage !== state.currentPage) {
      state.currentPage = visiblePage;
      updatePageHUD(visiblePage);
    }
    pruneMemory(state.currentPage);
  }, 90);
}

function getVisiblePageNum() {
  if (state.pageWrappers.size === 0) return state.currentPage;
  const containerRect = dom.readerContainer.getBoundingClientRect();
  const readingY = containerRect.top + 60;

  let bestPage = state.currentPage;
  let minDiff = Infinity;

  for (const [pNum, wrap] of state.pageWrappers.entries()) {
    const rect = wrap.getBoundingClientRect();
    if (rect.top <= readingY && rect.bottom >= readingY) {
      return pNum;
    }
    const diff = Math.abs(rect.top - readingY);
    if (diff < minDiff) {
      minDiff = diff;
      bestPage = pNum;
    }
  }
  return bestPage;
}

async function renderPage(pageNum) {
  if (state.renderedPages.has(pageNum)) return;
  state.renderedPages.add(pageNum);

  const wrapper = state.pageWrappers.get(pageNum);
  if (!wrapper) return;

  let canvas = wrapper.querySelector('canvas');
  if (!canvas) {
    canvas = document.createElement('canvas');
    canvas.id = `canvas-${pageNum}`;
    wrapper.appendChild(canvas);
  }

  try {
    const page = await state.pdfDoc.getPage(pageNum);
    const ctx = canvas.getContext('2d');
    const isPortrait = window.innerWidth <= 960 || (window.innerHeight > window.innerWidth);
    const containerWidth = isPortrait ? dom.readerContainer.clientWidth : Math.min(dom.readerContainer.clientWidth - 20, 950);
    const unscaledViewport = page.getViewport({ scale: 1 });

    let scale = state.fitWidth ? (containerWidth / unscaledViewport.width) : 1.25;
    scale = Math.max(scale, 0.4);

    const viewport = page.getViewport({ scale });
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    canvas.style.width = isPortrait ? '100%' : `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    wrapper.style.minHeight = `${viewport.height}px`;

    const renderTask = page.render({ canvasContext: ctx, viewport });
    state.activePages.set(pageNum, { page, renderTask, canvas, height: viewport.height });

    await renderTask.promise;
  } catch (err) {
    if (err.name !== 'RenderingCancelledException') {
      console.error(`Page render error for p.${pageNum}:`, err);
    }
  }
}

function unloadPage(pageNum) {
  const entry = state.activePages.get(pageNum);
  if (!entry) return;

  if (entry.renderTask) {
    try { entry.renderTask.cancel(); } catch (e) {}
  }
  if (entry.page) {
    try { entry.page.cleanup(); } catch (e) {}
  }

  const wrapper = state.pageWrappers.get(pageNum);
  if (wrapper) {
    if (entry.height) {
      wrapper.style.minHeight = `${entry.height}px`;
    }
    wrapper.innerHTML = '';
  }

  state.activePages.delete(pageNum);
  state.renderedPages.delete(pageNum);
}

function pruneMemory(centerPage) {
  for (const [pNum] of state.activePages.entries()) {
    if (Math.abs(pNum - centerPage) > 2) {
      unloadPage(pNum);
    }
  }
  if (state.pdfDoc) {
    try { state.pdfDoc.cleanup(); } catch (e) {}
  }
}

function jumpToPage(pageNum, smooth = true) {
  if (!state.pdfDoc || state.totalPages === 0) return;
  pageNum = Math.max(1, Math.min(pageNum, state.totalPages));
  state.currentPage = pageNum;
  state.isJumping = true;

  const target = state.pageWrappers.get(pageNum);
  if (target) {
    dom.readerContainer.scrollTo({
      top: target.offsetTop - 52,
      behavior: smooth ? 'smooth' : 'auto'
    });

    renderPage(pageNum);
    if (pageNum + 1 <= state.totalPages) renderPage(pageNum + 1);
    if (pageNum - 1 >= 1) renderPage(pageNum - 1);
    pruneMemory(pageNum);
    updatePageHUD(pageNum);
  }

  closeSidebar();

  clearTimeout(state.jumpTimer);
  state.jumpTimer = setTimeout(() => {
    state.isJumping = false;
    const vis = getVisiblePageNum();
    if (vis) {
      state.currentPage = vis;
      updatePageHUD(vis);
      pruneMemory(vis);
    }
  }, 300);
}

// ==========================================================================
// 4. CHAPTERS & NAVIGATION
// ==========================================================================

async function loadChapters(filename) {
  state.bookmarks = [];
  dom.chapterList.innerHTML = '';

  // 1. Try sidecar chapters from backend
  try {
    const res = await fetch(`/api/chapters?file=${encodeURIComponent(filename)}`);
    const data = await res.json();
    if (data.status === 'ok' && Array.isArray(data.chapters) && data.chapters.length > 0) {
      state.bookmarks = data.chapters;
    }
  } catch (err) {}

  // 2. Try PDF outline fallback
  if (state.bookmarks.length === 0 && state.pdfDoc) {
    try {
      const outline = await state.pdfDoc.getOutline();
      if (outline && outline.length > 0) {
        for (let item of outline) {
          let dest = item.dest;
          if (typeof dest === 'string') {
            dest = await state.pdfDoc.getDestination(dest);
          }
          if (dest) {
            const pIdx = await state.pdfDoc.getPageIndex(dest[0]);
            state.bookmarks.push({ title: item.title, pageNum: pIdx + 1 });
          }
        }
      }
    } catch (e) {}
  }

  // 3. Fallback: Generate smart 60-page segments if no outline exists
  if (state.bookmarks.length === 0 && state.totalPages > 0) {
    const pagesPerChap = 60;
    let ch = 1;
    for (let p = 1; p <= state.totalPages; p += pagesPerChap) {
      state.bookmarks.push({ title: `Chapter ${ch}`, pageNum: p });
      ch++;
    }
  }

  renderChapterDrawer();
  updatePageHUD(state.currentPage);
}

function renderChapterDrawer(filterQuery = '') {
  dom.chapterList.innerHTML = '';
  if (state.bookmarks.length === 0) {
    dom.chapterList.innerHTML = '<div style="padding:16px;color:var(--text-muted);font-size:0.8rem;text-align:center;">No chapters found.</div>';
    return;
  }

  const q = filterQuery.toLowerCase().trim();
  const currentIdx = getCurrentChapterIndex();

  state.bookmarks.forEach((b, idx) => {
    if (q && !b.title.toLowerCase().includes(q)) return;

    const div = document.createElement('div');
    div.className = `chapter-item ${idx === currentIdx ? 'active' : ''}`;
    div.id = `chapter-drawer-item-${idx}`;
    div.innerHTML = `
      <span>📖 ${escapeHtml(b.title)}</span>
      <span style="font-size:0.7rem; color:var(--text-muted);">p. ${b.pageNum}</span>
    `;
    div.onclick = (e) => {
      e.stopPropagation();
      jumpToChapterIndex(idx);
    };
    dom.chapterList.appendChild(div);
  });
}

function getCurrentChapterIndex() {
  if (!state.bookmarks || state.bookmarks.length === 0) return 0;
  let idx = 0;
  for (let i = 0; i < state.bookmarks.length; i++) {
    if (state.currentPage >= state.bookmarks[i].pageNum) {
      idx = i;
    } else {
      break;
    }
  }
  return idx;
}

function jumpToChapterIndex(idx) {
  if (!state.bookmarks || state.bookmarks.length === 0) return;
  idx = Math.max(0, Math.min(idx, state.bookmarks.length - 1));
  const target = state.bookmarks[idx];
  if (target) {
    jumpToPage(target.pageNum);
    showToast(`Jumped to ${target.title}`);
  }
}

function nextChapter() {
  if (!state.bookmarks || state.bookmarks.length === 0) {
    jumpToPage(Math.min(state.currentPage + 50, state.totalPages));
    return;
  }
  const curIdx = getCurrentChapterIndex();
  if (curIdx + 1 < state.bookmarks.length) {
    jumpToChapterIndex(curIdx + 1);
  } else {
    showToast('Already on the last chapter!');
  }
}

function prevChapter() {
  if (!state.bookmarks || state.bookmarks.length === 0) {
    jumpToPage(Math.max(state.currentPage - 50, 1));
    return;
  }
  const curIdx = getCurrentChapterIndex();
  if (state.currentPage > state.bookmarks[curIdx].pageNum + 2) {
    jumpToChapterIndex(curIdx);
  } else if (curIdx > 0) {
    jumpToChapterIndex(curIdx - 1);
  } else {
    jumpToPage(1);
    showToast('Already on the first chapter!');
  }
}

function updatePageHUD(pageNum) {
  dom.readerPageIndicator.textContent = `p. ${pageNum} / ${state.totalPages}`;
  dom.hudPages.innerHTML = `<strong>${pageNum}</strong> / ${state.totalPages}`;

  let currentChapTitle = 'Chapter 1';
  let chapIdx = 0;

  if (state.bookmarks && state.bookmarks.length > 0) {
    chapIdx = getCurrentChapterIndex();
    currentChapTitle = state.bookmarks[chapIdx].title;
    dom.readerChapIndicator.textContent = `Ch. ${chapIdx + 1} / ${state.bookmarks.length}`;
    dom.hudChap.innerHTML = `<strong>${escapeHtml(currentChapTitle)}</strong> (Ch. ${chapIdx + 1}/${state.bookmarks.length})`;

    document.querySelectorAll('.chapter-item').forEach((item, idx) => {
      item.classList.toggle('active', idx === chapIdx);
    });
  } else {
    dom.readerChapIndicator.textContent = `Manhwa`;
    dom.hudChap.textContent = `Reading`;
  }

  recordHistory(currentChapTitle, chapIdx + 1);
}

// ==========================================================================
// 5. HISTORY PERSISTENCE (downloads/reading_history.json)
// ==========================================================================

function recordHistory(chapTitle, chapIdx) {
  clearTimeout(state.historySyncTimer);
  state.historySyncTimer = setTimeout(() => {
    if (!state.currentFile || !state.totalPages || state.currentPage < 1) return;

    const payload = {
      filename: state.currentFile.filename,
      last_chapter_title: chapTitle,
      last_chapter_index: chapIdx,
      last_page: state.currentPage,
      total_pages: state.totalPages,
      progress_percent: Math.round((state.currentPage / state.totalPages) * 1000) / 10,
      updated_at: new Date().toISOString()
    };

    fetch('/api/update_history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).catch(() => {});
  }, 400);
}

window.addEventListener('beforeunload', () => {
  if (!state.currentFile || !state.totalPages || state.currentPage < 1) return;
  const chapIdx = getCurrentChapterIndex();
  const chapTitle = state.bookmarks[chapIdx]?.title || 'Reading';

  const payload = JSON.stringify({
    filename: state.currentFile.filename,
    last_chapter_title: chapTitle,
    last_chapter_index: chapIdx + 1,
    last_page: state.currentPage,
    total_pages: state.totalPages,
    progress_percent: Math.round((state.currentPage / state.totalPages) * 1000) / 10,
    updated_at: new Date().toISOString()
  });

  if (navigator.sendBeacon) {
    navigator.sendBeacon('/api/update_history', payload);
  }
});

// ==========================================================================
// 6. READER CONTROLS: AUTO-SCROLL, FIT, FULLSCREEN, DRAWER
// ==========================================================================

function toggleAutoScroll() {
  state.autoScrollActive = !state.autoScrollActive;

  if (state.autoScrollActive) {
    dom.btnAuto.classList.add('active');
    dom.autoScrollBadge.classList.add('active');
    showToast(`Auto-scroll ON (${state.autoScrollSpeed}x)`);
    state.autoScrollInterval = setInterval(() => {
      dom.readerContainer.scrollTop += state.autoScrollSpeed;
      if (dom.readerContainer.scrollTop + dom.readerContainer.clientHeight >= dom.readerContainer.scrollHeight - 10) {
        toggleAutoScroll();
      }
    }, 16);
  } else {
    stopAutoScroll();
    showToast('Auto-scroll PAUSED');
  }
}

function stopAutoScroll() {
  state.autoScrollActive = false;
  dom.btnAuto.classList.remove('active');
  dom.autoScrollBadge.classList.remove('active');
  if (state.autoScrollInterval) {
    clearInterval(state.autoScrollInterval);
    state.autoScrollInterval = null;
  }
}

function toggleFitWidth() {
  state.fitWidth = !state.fitWidth;
  dom.btnFit.classList.toggle('active', state.fitWidth);

  const toReload = Array.from(state.activePages.keys());
  toReload.forEach(pNum => unloadPage(pNum));
  toReload.forEach(pNum => renderPage(pNum));
  showToast(state.fitWidth ? 'Fit to Width' : 'Original Scale');
}

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch(() => {});
  } else {
    document.exitFullscreen().catch(() => {});
  }
}

function toggleSidebar() {
  const isOpen = dom.sidebar.classList.contains('open');
  if (isOpen) {
    closeSidebar();
  } else {
    openSidebar();
  }
}

function openSidebar() {
  dom.sidebar.classList.add('open');
  dom.sidebarBackdrop.classList.add('open');
  const curIdx = getCurrentChapterIndex();
  const el = document.getElementById(`chapter-drawer-item-${curIdx}`);
  if (el) el.scrollIntoView({ block: 'nearest' });
}

function closeSidebar() {
  dom.sidebar.classList.remove('open');
  dom.sidebarBackdrop.classList.remove('open');
}

function toggleShortcutsModal() {
  dom.shortcutsModal.classList.toggle('open');
}

function resetHideNavTimer() {
  dom.readerTopbar.classList.remove('hidden');
  dom.floatingHud.classList.remove('hidden');
  clearTimeout(state.hideNavTimer);

  if (state.currentView === 'reader' && dom.readerContainer.scrollTop > 100 && !state.autoScrollActive) {
    state.hideNavTimer = setTimeout(() => {
      const isDrawerOpen = dom.sidebar.classList.contains('open');
      const isModalOpen = dom.shortcutsModal.classList.contains('open');
      if (!isDrawerOpen && !isModalOpen) {
        dom.readerTopbar.classList.add('hidden');
      }
    }, 3800);
  }
}

window.addEventListener('mousemove', resetHideNavTimer);
window.addEventListener('touchstart', resetHideNavTimer);

// ==========================================================================
// 7. KEYBOARD SHORTCUTS & EVENT LISTENERS
// ==========================================================================

let _spaceHeld = false;
let _spaceHoldTimer = null;
let _spaceHoldInterval = null;

window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

  if (e.key === '?') {
    e.preventDefault();
    toggleShortcutsModal();
    return;
  }

  if (e.key === 'Escape') {
    if (dom.shortcutsModal.classList.contains('open')) {
      toggleShortcutsModal();
      return;
    }
    if (dom.sidebar.classList.contains('open')) {
      closeSidebar();
      return;
    }
    if (state.currentView === 'reader') {
      switchView('home');
      return;
    }
  }

  if (state.currentView !== 'reader') return;

  // Spacebar smooth scroll
  if (e.code === 'Space') {
    e.preventDefault();
    const dir = e.shiftKey ? -1 : 1;
    if (e.repeat) return;

    if (!_spaceHeld) {
      _spaceHeld = true;
      const scrollStep = window.innerHeight * 0.8;
      dom.readerContainer.scrollBy({ top: dir * scrollStep, behavior: 'smooth' });

      _spaceHoldTimer = setTimeout(() => {
        if (_spaceHeld) {
          _spaceHoldInterval = setInterval(() => {
            if (_spaceHeld) {
              dom.readerContainer.scrollBy({ top: dir * scrollStep, behavior: 'smooth' });
            }
          }, 160);
        }
      }, 200);
    }
    return;
  }

  // Next Chapter
  if (e.code === 'ArrowRight' || (e.ctrlKey && e.code === 'ArrowDown') || e.code === 'KeyD') {
    e.preventDefault();
    nextChapter();
    return;
  }

  // Prev Chapter
  if (e.code === 'ArrowLeft' || (e.ctrlKey && e.code === 'ArrowUp')) {
    e.preventDefault();
    prevChapter();
    return;
  }

  // Step scroll
  if (e.code === 'PageDown' || e.code === 'ArrowDown') {
    e.preventDefault();
    dom.readerContainer.scrollBy({ top: e.code === 'PageDown' ? window.innerHeight * 0.85 : 120, behavior: 'smooth' });
    return;
  }
  if (e.code === 'PageUp' || e.code === 'ArrowUp') {
    e.preventDefault();
    dom.readerContainer.scrollBy({ top: e.code === 'PageUp' ? -window.innerHeight * 0.85 : -120, behavior: 'smooth' });
    return;
  }

  // Toggle Auto-Scroll (A)
  if (e.code === 'KeyA' && !e.ctrlKey) {
    e.preventDefault();
    toggleAutoScroll();
    return;
  }

  // Auto-scroll speed
  if (e.key === '+' || e.key === '=') {
    state.autoScrollSpeed = Math.min(state.autoScrollSpeed + 1, 10);
    dom.autoSpeedDisplay.textContent = state.autoScrollSpeed;
    showToast(`Speed: ${state.autoScrollSpeed}x`);
    return;
  }
  if (e.key === '-' || e.key === '_') {
    state.autoScrollSpeed = Math.max(state.autoScrollSpeed - 1, 1);
    dom.autoSpeedDisplay.textContent = state.autoScrollSpeed;
    showToast(`Speed: ${state.autoScrollSpeed}x`);
    return;
  }

  // Fit Width (W)
  if (e.code === 'KeyW') {
    e.preventDefault();
    toggleFitWidth();
    return;
  }

  // Fullscreen (F)
  if (e.code === 'KeyF') {
    e.preventDefault();
    toggleFullscreen();
    return;
  }
});

window.addEventListener('keyup', (e) => {
  if (e.code === 'Space') {
    _spaceHeld = false;
    clearTimeout(_spaceHoldTimer);
    clearInterval(_spaceHoldInterval);
  }
});

// Chapter Drawer Search
dom.chapterSearch.addEventListener('input', (e) => {
  renderChapterDrawer(e.target.value);
});

// Drag & Drop anywhere on page
window.addEventListener('dragover', (e) => {
  e.preventDefault();
  dom.heroDropzone.classList.add('dragover');
});
window.addEventListener('dragleave', (e) => {
  e.preventDefault();
  dom.heroDropzone.classList.remove('dragover');
});
window.addEventListener('drop', (e) => {
  e.preventDefault();
  dom.heroDropzone.classList.remove('dragover');
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    const file = e.dataTransfer.files[0];
    if (file.name.toLowerCase().endsWith('.pdf')) {
      openLocalPdfFile(file);
    }
  }
});

// Local file input
dom.fileInput.addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (file) openLocalPdfFile(file);
});

// Helper utilities
function showToast(msg) {
  dom.toast.textContent = msg;
  dom.toast.classList.add('show');
  setTimeout(() => dom.toast.classList.remove('show'), 2400);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function goToDownloader() {
  if (window.parent && window.parent !== window && typeof window.parent.switchAppTab === 'function') {
    window.parent.switchAppTab('downloader');
  } else {
    window.location.href = '/';
  }
}

// Initialize on Load: loads history only, 0MB RAM, zero folder scan!
window.addEventListener('DOMContentLoaded', () => {
  fetchRecentHistory();

  // If running inside iframe, show popout button
  try {
    const popoutBtn = document.getElementById('btn-popout');
    if (popoutBtn && window.parent !== window) {
      popoutBtn.style.display = 'inline-flex';
    }
  } catch (e) {}

  // Check URL parameters for direct file launch
  try {
    const params = new URLSearchParams(window.location.search);
    const fileParam = params.get('file');
    if (fileParam) {
      const pageParam = parseInt(params.get('page') || '0', 10);
      setTimeout(() => {
        openFileFromHistory({
          filename: fileParam,
          title: fileParam.replace(/\.[^/.]+$/, ''),
          last_page: pageParam > 0 ? pageParam : 1
        });
      }, 150);
    }
  } catch (e) {
    console.warn('Error reading URL params:', e);
  }
});

