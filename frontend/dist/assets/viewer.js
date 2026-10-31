// ── Parse result viewer ──
// OCR Studio's workspace in the console theme: page preview with block
// overlay, thumbnails and zoom, beside Rendered / Markdown / Blocks /
// Figures tabs. Previews are display surfaces only; /parse
// already received the original file.

import { api } from "/assets/api.js?v=10";
import { esc, isOfficeUpload, isPdfUpload } from "/assets/models.js?v=11";
import { renderMarkdown } from "/assets/math.js";

const RENDER_MAX_SIDE = 1800;
const THUMB_SIDE = 80;
const PALETTE = ["#4a9eff", "#f5b23a", "#7c8fff", "#dc244c", "#2dd4bf",
  "#a3e635", "#e879f9", "#ff8a50", "#38bdf8", "#facc15"];
const TABS = ["rendered", "markdown", "blocks", "figures"];
const SVG_NS = "http://www.w3.org/2000/svg";

let pdfjs = null;
function loadPdfJs() {
  pdfjs ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/assets/vendor/pdf.min.js";
    script.onload = () => {
      window.pdfjsLib.GlobalWorkerOptions.workerSrc = "/assets/vendor/pdf.worker.min.js";
      resolve(window.pdfjsLib);
    };
    script.onerror = () => { pdfjs = null; reject(new Error("pdf.js failed to load")); };
    document.head.append(script);
  });
  return pdfjs;
}

async function pdfPreview(blob) {
  const lib = await loadPdfJs();
  // isEvalSupported:false mitigates CVE-2024-4367 (no eval of font code).
  const loading = lib.getDocument({ data: await blob.arrayBuffer(), isEvalSupported: false });
  const doc = await loading.promise.catch(async (error) => { await loading.destroy(); throw error; });
  return {
    pageCount: doc.numPages,
    async render(pageNum, maxSide = RENDER_MAX_SIDE) {
      const page = await doc.getPage(pageNum);
      const base = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: maxSide / Math.max(base.width, base.height) });
      const canvas = document.createElement("canvas");
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      await page.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport }).promise;
      return { el: canvas, width: canvas.width, height: canvas.height };
    },
    destroy: () => doc.destroy(),
  };
}

function imagePreview(file, multipleFrames) {
  // shortcut: frame previews need a decoder; show an explicit warning until one is available.
  if (multipleFrames) return Promise.reject(new Error("Multi-frame image previews are unavailable; use the page results"));
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => resolve({
      pageCount: 1,
      render: async () => ({ el: img, width: img.naturalWidth, height: img.naturalHeight }),
      destroy: () => URL.revokeObjectURL(url),
    });
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("the image could not be decoded")); };
    img.src = url;
  });
}

async function openPreview(file, signal, multipleFrames = false) {
  if (isOfficeUpload(file)) {
    const result = await api.officePdf(file, { signal });
    if (!result.ok) throw new Error(result.error || "Office conversion failed");
    return pdfPreview(result.data);
  }
  if (isPdfUpload(file)) return pdfPreview(file);
  // The server sniffs content, so a PDF renamed .png still parses: preview it
  // the same way instead of reporting an undecodable image.
  return imagePreview(file, multipleFrames).catch(async (error) => {
    const head = new Uint8Array(await file.slice(0, 5).arrayBuffer());
    if (String.fromCharCode(...head) !== "%PDF-") throw error;
    return pdfPreview(file);
  });
}

// Copy with a fallback for origins without the async Clipboard API (plain
// http on a LAN address): select the text and use the legacy command.
export async function copyText(text, source) {
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(source);
  selection.removeAllRanges();
  selection.addRange(range);
  const copied = document.execCommand("copy");
  selection.removeAllRanges();
  if (!copied) throw new Error("copy unavailable");
}

function pngDataUrl(base64) {
  return `data:image/png;base64,${String(base64).replace(/[^A-Za-z0-9+/=]/g, "")}`;
}

function blockColor(type) {
  let h = 0;
  for (const ch of String(type || "block")) h = (h * 31 + ch.charCodeAt(0)) | 0;
  return PALETTE[Math.abs(h) % PALETTE.length];
}

// Ids carry a per-instance prefix: Parse and OCR can each mount a viewer, and
// the tab/panel ARIA references must stay unique within the document.
let viewerCount = 0;

const template = (uid) => `
  <div class="pv-toolbar">
    <div class="pv-tools" role="toolbar" aria-label="Page and view tools">
      <div class="pv-group">
        <button type="button" class="btn pv-icon" data-act="prev" aria-label="Previous page">‹</button>
        <span class="pv-page" aria-live="polite">Page – / –</span>
        <button type="button" class="btn pv-icon" data-act="next" aria-label="Next page">›</button>
        <button type="button" class="btn" data-act="film" aria-pressed="false">Thumbnails</button>
      </div>
      <div class="pv-group">
        <button type="button" class="btn pv-icon" data-act="zoom-out" aria-label="Zoom out">−</button>
        <button type="button" class="btn" data-act="fit" aria-pressed="true">Fit</button>
        <button type="button" class="btn pv-zoom" data-act="zoom-100" title="Reset to 100%" aria-label="Zoom 100%, reset to 100%">100%</button>
        <button type="button" class="btn pv-icon" data-act="zoom-in" aria-label="Zoom in">+</button>
      </div>
      <div class="pv-group">
        <button type="button" class="btn" data-act="blocks" aria-pressed="true">Blocks</button>
      </div>
    </div>
    <div class="pv-tabs" role="tablist" aria-label="Content view">
      ${TABS.map((t, i) => `<button type="button" role="tab" id="pv${uid}-tab-${t}" data-tab="${t}"
        aria-controls="pv${uid}-panel-${t}" aria-selected="${i === 0}"${t === "figures" ? " hidden" : ""}>${t}</button>`).join("")}
    </div>
  </div>
  <div class="pv-film" hidden></div>
  <div class="pv-split">
    <div class="pv-viewer">
      <div class="pv-stage-wrap">
        <div class="pv-stage">
          <div class="pv-host"><p class="caption pv-note">Loading preview…</p></div>
          <svg class="pv-overlay" aria-hidden="true"></svg>
        </div>
      </div>
    </div>
    <div class="pv-content">
      ${TABS.map((t, i) => `<div class="pv-panel" role="tabpanel" id="pv${uid}-panel-${t}" data-panel="${t}" aria-labelledby="pv${uid}-tab-${t}"${i ? " hidden" : ""}></div>`).join("")}
    </div>
  </div>
  <section class="pv-legend-box" aria-labelledby="pv${uid}-legend-title" hidden>
    <header class="pv-legend-head">
      <h3 class="subhead" id="pv${uid}-legend-title">Block types</h3>
      <button type="button" class="btn" data-act="legend-all">Hide all</button>
    </header>
    <div class="pv-legend"></div>
  </section>`;

/**
 * Mount the viewer into `container` for one result shaped like a /parse
 * response ({ pages: [{ page, width, height, markdown, blocks, images }] }).
 * Options: `labels` renames content tabs (e.g. { markdown: "raw" }), and
 * `renderContent(page)` returns the rendered tab's HTML instead of Markdown.
 */
export function mountViewer(container, file, data, { labels = {}, renderContent = null } = {}) {
  const root = document.createElement("section");
  root.className = "pv";
  root.innerHTML = template(++viewerCount);
  container.appendChild(root);
  const q = (selector) => root.querySelector(selector);
  const act = (name) => q(`[data-act="${name}"]`);
  const tabEl = (t) => q(`.pv-tabs [data-tab="${t}"]`);
  const panelEl = (t) => q(`[data-panel="${t}"]`);
  const label = (t) => labels[t] || t;
  for (const t of TABS) tabEl(t).textContent = label(t);
  act("blocks").textContent = label("blocks");

  const controller = new AbortController();
  const pages = data.pages || [];
  const state = {
    page: 1, total: Math.max(1, pages.length), zoom: "fit", scale: 1, showBlocks: true,
    hidden: new Set(), renderVersion: 0, filmVersion: 0,
  };
  // Without structured blocks there is nothing to overlay, list or filter.
  const hasBlocks = pages.some((p) => Array.isArray(p.blocks));
  tabEl("blocks").hidden = !hasBlocks;
  act("blocks").closest(".pv-group").hidden = !hasBlocks;
  const cache = new Map();
  const multipleFrames = pages.length > 1 || (data.warnings || []).some(
    warning => warning.code === "pages_truncated" && warning.details?.total_pages > 1);
  let preview = openPreview(file, controller.signal, multipleFrames);
  // A single page has nothing to page through: hide the pager and Thumbnails.
  const syncPager = () => { act("prev").closest(".pv-group").hidden = state.total <= 1; };
  syncPager();
  const adoptPreview = () => preview.then((p) => {
    if (controller.signal.aborted) return;
    state.total = Math.max(p.pageCount || 1, pages.length);
    syncPager();
    updatePager();
  }, () => {});
  adoptPreview();

  const pageResult = (n) => pages.find((p) => p.page === n) || null;

  function updatePager() {
    q(".pv-page").textContent = `Page ${state.page} / ${state.total}`;
    act("prev").disabled = state.page <= 1;
    act("next").disabled = state.page >= state.total;
  }

  // ── Page preview ──
  async function renderPage() {
    const version = ++state.renderVersion;
    updatePager();
    const host = q(".pv-host");
    const pageNum = state.page;
    try {
      const p = await preview;
      let rendered = cache.get(pageNum);
      if (!rendered) {
        rendered = p.render(pageNum);
        cache.set(pageNum, rendered);
        if (cache.size > 3) cache.delete(cache.keys().next().value);
      }
      rendered = await rendered.catch((error) => { cache.delete(pageNum); throw error; });
      if (version !== state.renderVersion || controller.signal.aborted) return;
      rendered.el.style.display = "block";
      host.replaceChildren(rendered.el);
      applyZoom(rendered);
      drawOverlay(rendered);
    } catch (error) {
      if (version !== state.renderVersion || controller.signal.aborted) return;
      q(".pv-stage").style.width = q(".pv-stage").style.height = "";
      q(".pv-overlay").replaceChildren();
      host.innerHTML = `<div class="alert pv-note">${esc(
        `Preview unavailable: ${error?.message || error}. The recognition results are still complete.`)}` +
        ` <button type="button" class="btn" data-act="retry-preview">Retry preview</button></div>`;
      updatePan();
    }
  }

  function applyZoom({ width, height }) {
    // Fit shows the whole page: bounded by the frame's width and height
    // (minus its padding), never enlarged past 100%.
    const dispW = state.zoom === "fit"
      ? Math.floor(Math.min(width, Math.max(200, wrap.clientWidth - 34),
        Math.max(150, wrap.clientHeight - 34) * (width / height)))
      : width * state.zoom;
    const dispH = dispW * (height / width);
    // Effective scale, so relative zoom steps from Fit start at what is shown.
    state.scale = dispW / width;
    const percent = `${Math.round(state.scale * 100)}%`;
    act("zoom-100").textContent = percent;
    act("zoom-100").setAttribute("aria-label", `Zoom ${percent}, reset to 100%`);
    act("fit").setAttribute("aria-pressed", String(state.zoom === "fit"));
    // Keep the point at the centre of the frame in place across zoom steps.
    const cx = (wrap.scrollLeft + wrap.clientWidth / 2) / wrap.scrollWidth;
    const cy = (wrap.scrollTop + wrap.clientHeight / 2) / wrap.scrollHeight;
    for (const node of [q(".pv-stage"), q(".pv-overlay")]) {
      node.style.width = `${dispW}px`;
      node.style.height = `${dispH}px`;
    }
    const el = q(".pv-host").firstChild;
    el.style.width = `${dispW}px`;
    el.style.height = "auto";
    wrap.scrollLeft = cx * wrap.scrollWidth - wrap.clientWidth / 2;
    wrap.scrollTop = cy * wrap.scrollHeight - wrap.clientHeight / 2;
    updatePan();
  }

  // Block bboxes are in page pixel space at the server DPI; the viewBox
  // scales them onto the preview, whatever its resolution.
  function drawOverlay(rendered) {
    const svg = q(".pv-overlay");
    const pr = pageResult(state.page);
    const blocks = state.showBlocks && pr?.blocks ? pr.blocks : [];
    svg.replaceChildren();
    svg.classList.toggle("on", blocks.length > 0);
    svg.setAttribute("viewBox", `0 0 ${pr?.width || rendered.width} ${pr?.height || rendered.height}`);
    blocks.forEach((b, i) => {
      const bb = b.bbox || [];
      if (bb.length < 4) return;
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", Math.min(bb[0], bb[2]));
      rect.setAttribute("y", Math.min(bb[1], bb[3]));
      rect.setAttribute("width", Math.abs(bb[2] - bb[0]));
      rect.setAttribute("height", Math.abs(bb[3] - bb[1]));
      rect.setAttribute("class", "pv-rect");
      rect.dataset.index = i;
      rect.dataset.type = b.type || "block";
      rect.style.setProperty("--bc", blockColor(b.type));
      rect.classList.toggle("filtered", state.hidden.has(rect.dataset.type));
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent = `${b.type || "block"}${b.order == null ? "" : ` · #${b.order}`}`;
      rect.appendChild(title);
      rect.addEventListener("mouseenter", () => highlight(i, true));
      rect.addEventListener("mouseleave", () => highlight(i, false));
      rect.addEventListener("click", () => focusBlock(i));
      svg.appendChild(rect);
    });
  }

  function highlight(i, on) {
    root.querySelectorAll(`[data-index="${i}"]`).forEach((node) => node.classList.toggle("hot", on));
  }

  function focusBlock(i) {
    setTab("blocks");
    const row = q(`.pv-block[data-index="${i}"]`);
    if (!row) return;
    row.focus({ preventScroll: true });
    row.scrollIntoView({ block: "nearest" });
    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 1100);
  }

  // ── Result panels ──
  function renderPanels() {
    const pr = pageResult(state.page);

    const rendered = panelEl("rendered");
    if (!pr) rendered.innerHTML = `<p class="caption">No result for page ${state.page} — beyond max_pages.</p>`;
    else if (pr.error) rendered.innerHTML = `<div class="alert alert-error" role="alert">${esc(pr.error)}</div>`;
    else if (renderContent) rendered.innerHTML = renderContent(pr);
    else if (pr.markdown) rendered.replaceChildren(markdownNode(pr));
    else rendered.innerHTML = `<p class="caption">No ${esc(label("markdown"))} output for this page${hasBlocks ? ` — see ${esc(label("blocks"))}` : ""}.</p>`;

    panelEl("markdown").innerHTML = pr?.markdown
      ? `<button type="button" class="btn pv-copy" data-act="copy">Copy</button><pre class="codeblock pv-raw"><code>${esc(pr.markdown)}</code></pre>`
      : `<p class="caption">No ${esc(label("markdown"))} output for this page.</p>`;
    q('[data-act="copy"]')?.addEventListener("click", (event) => {
      const button = event.currentTarget;
      const show = (label) => {
        button.textContent = label;
        clearTimeout(button.resetTimer);
        button.resetTimer = setTimeout(() => { button.textContent = "Copy"; }, 1500);
      };
      copyText(pr.markdown, panelEl("markdown").querySelector("code")).then(
        () => show("Copied"), () => show("Copy failed — select the text"));
    });

    renderBlockList(pr);
    renderFigures(pr);
  }

  function renderBlockList(pr) {
    const panel = panelEl("blocks");
    const blocks = pr?.blocks || [];
    if (!blocks.length) { panel.innerHTML = `<p class="caption">No ${esc(label("blocks"))} for this page.</p>`; return; }
    // Unordered blocks (headers, figures…) follow the reading order.
    const rank = (b) => b.order ?? Number.MAX_SAFE_INTEGER;
    const sorted = blocks.map((b, i) => [b, i]).sort(([a], [b]) => rank(a) - rank(b));
    panel.innerHTML = `<ul class="pv-blocks">${sorted.map(([b, i]) => {
      const type = b.type || "block";
      const bbox = (b.bbox || []).map((v) => Math.round(v)).join(", ");
      return `<li class="pv-block${state.hidden.has(type) ? " filtered" : ""}" data-index="${i}" data-type="${esc(type)}" tabindex="0" role="button" aria-expanded="false">` +
        `<div class="pv-b-head"><span class="pv-b-type" style="--bc:${blockColor(type)}">${esc(type)}</span>` +
        `<span>${b.order == null ? "unordered" : `#${esc(b.order)}`}</span><span>[${esc(bbox)}]</span></div>` +
        `<div class="pv-b-content">${esc(b.content || "")}</div></li>`;
    }).join("")}</ul>`;
    panel.querySelectorAll(".pv-block").forEach((row) => {
      const i = Number(row.dataset.index);
      const toggle = () => row.setAttribute("aria-expanded", String(row.classList.toggle("expanded")));
      row.addEventListener("click", toggle);
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        toggle();
      });
      for (const [on, events] of [[true, ["mouseenter", "focus"]], [false, ["mouseleave", "blur"]]]) {
        for (const name of events) row.addEventListener(name, () => highlight(i, on));
      }
    });
  }

  // Paddle markdown references figures by name (imgs/…). Resolve them to the
  // returned crops inside an inert <template>, so no broken URL is fetched.
  function markdownNode(pr) {
    const template = document.createElement("template");
    template.innerHTML = `<div class="result-text">${renderMarkdown(pr.markdown)}</div>`;
    for (const img of template.content.querySelectorAll("img")) {
      const name = img.getAttribute("src") || "";
      const index = (pr.image_names || []).indexOf(name);
      if (index >= 0 && pr.images?.[index]) img.src = pngDataUrl(pr.images[index]);
      else {
        const note = document.createElement("span");
        note.className = "caption";
        // Images arrive only as crops of this response: never fetch a URL.
        const literal = document.createElement("span");
        literal.className = "literal";
        literal.textContent = name;
        note.replaceChildren(/^[a-z][a-z0-9+.-]*:/i.test(name) ? "[external image " : "[figure ",
          literal, /^[a-z][a-z0-9+.-]*:/i.test(name) ? " — not loaded]"
            : Array.isArray(pr.images)
              ? " — crop not returned, see the images_truncated warning]"
              : " — enable include_images to show it]");
        img.replaceWith(note);
      }
    }
    return template.content;
  }

  function renderFigures(pr) {
    const images = pr?.images || [];
    tabEl("figures").hidden = !images.length;
    if (!images.length && state.tab === "figures") setTab("rendered");
    panelEl("figures").innerHTML = `<div class="pv-figures">${images.map((img, i) => {
      const name = pr.image_names?.[i] || `page-${pr.page}-figure-${i + 1}.png`;
      return `<figure class="result-figure"><img src="${pngDataUrl(img)}" alt="${esc(name)}" loading="lazy" decoding="async" />` +
        `<figcaption>${esc(name)}</figcaption></figure>`;
    }).join("")}</div>`;
  }

  function renderLegend() {
    const types = [...new Set(pages.flatMap((p) => (p.blocks || []).map((b) => b.type || "block")))].sort();
    const legend = q(".pv-legend");
    // A single type filters nothing the Blocks toggle does not already cover.
    q(".pv-legend-box").hidden = types.length < 2;
    legend.innerHTML = types.map((t) =>
      `<button type="button" class="pv-lchip" data-type="${esc(t)}" style="--bc:${blockColor(t)}" aria-pressed="true">${esc(t)}</button>`).join("");
    legend.querySelectorAll(".pv-lchip").forEach((chip) => chip.addEventListener("click", () => {
      const t = chip.dataset.type;
      if (state.hidden.has(t)) state.hidden.delete(t); else state.hidden.add(t);
      applyTypeFilter();
    }));
  }

  // Syncs chips, overlay rects, block rows and the Show/Hide all toggle.
  function applyTypeFilter() {
    root.querySelectorAll(".pv-lchip").forEach((chip) =>
      chip.setAttribute("aria-pressed", String(!state.hidden.has(chip.dataset.type))));
    root.querySelectorAll(".pv-rect, .pv-block").forEach((node) =>
      node.classList.toggle("filtered", state.hidden.has(node.dataset.type)));
    act("legend-all").textContent = state.hidden.size ? "Show all" : "Hide all";
  }

  function toggleAllTypes() {
    const chips = [...root.querySelectorAll(".pv-lchip")];
    if (state.hidden.size) state.hidden.clear();
    else chips.forEach((chip) => state.hidden.add(chip.dataset.type));
    applyTypeFilter();
  }

  function setTab(tab) {
    state.tab = tab;
    for (const t of TABS) {
      tabEl(t).setAttribute("aria-selected", String(t === tab));
      tabEl(t).tabIndex = t === tab ? 0 : -1;
      panelEl(t).hidden = t !== tab;
    }
  }

  // ── Thumbnails ──
  async function renderFilm() {
    const version = ++state.filmVersion;
    const strip = q(".pv-film");
    strip.replaceChildren();
    if (strip.hidden) return;
    try {
      const p = await preview;
      if (version !== state.filmVersion) return;
      const buttons = [];
      for (let n = 1; n <= state.total; n++) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = n;
        btn.setAttribute("aria-label", `Page ${n}`);
        btn.classList.toggle("current", n === state.page);
        btn.addEventListener("click", () => navigate(n));
        strip.appendChild(btn);
        buttons.push(btn);
      }
      // One page at a time: rasterizing every page concurrently stalls the tab.
      for (let i = 0; i < Math.min(buttons.length, p.pageCount || 0); i++) {
        if (version !== state.filmVersion || strip.hidden || controller.signal.aborted) return;
        try {
          const thumb = await p.render(i + 1, THUMB_SIDE);
          if (version !== state.filmVersion) return;
          // Image previews reuse one <img>: clone it and drop the zoom's inline size.
          const el = thumb.el.tagName === "IMG" ? thumb.el.cloneNode() : thumb.el;
          el.removeAttribute("style");
          buttons[i].prepend(el);
        } catch { /* The page number stays usable without a thumbnail. */ }
      }
    } catch { /* No preview: the pager still works. */ }
  }

  function navigate(n) {
    if (n < 1 || n > state.total) return;
    state.page = n;
    wrap.scrollTo(0, 0);
    renderPage();
    renderPanels();
    q(".pv-film").querySelectorAll("button").forEach((btn, i) => {
      btn.classList.toggle("current", i + 1 === n);
      if (i + 1 === n) btn.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }

  // ── Drag to pan ──
  // Mouse only (touch scrolls natively). A drag past a few pixels pans the
  // frame and swallows the click, so a plain click still selects a block.
  const wrap = q(".pv-stage-wrap");
  const canPan = () => wrap.scrollWidth > wrap.clientWidth || wrap.scrollHeight > wrap.clientHeight;
  const updatePan = () => wrap.classList.toggle("pannable", canPan());
  let drag = null;
  let swallowClick = false;
  wrap.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "mouse" || event.button !== 0 || !canPan()) return;
    // Presses on the frame's own scrollbars keep their native behaviour.
    const box = wrap.getBoundingClientRect();
    if (event.clientX - box.left >= wrap.clientLeft + wrap.clientWidth
      || event.clientY - box.top >= wrap.clientTop + wrap.clientHeight) return;
    event.preventDefault();
    drag = { x: event.clientX, y: event.clientY, left: wrap.scrollLeft, top: wrap.scrollTop, id: event.pointerId, moved: false };
  });
  wrap.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved) {
      if (Math.hypot(dx, dy) < 4) return;
      drag.moved = true;
      wrap.setPointerCapture(drag.id);
      wrap.classList.add("panning");
    }
    wrap.scrollLeft = drag.left - dx;
    wrap.scrollTop = drag.top - dy;
  });
  const endPan = () => {
    if (drag?.moved) {
      wrap.classList.remove("panning");
      swallowClick = true;
      setTimeout(() => { swallowClick = false; });
    }
    drag = null;
  };
  wrap.addEventListener("pointerup", endPan);
  wrap.addEventListener("pointercancel", endPan);
  wrap.addEventListener("click", (event) => {
    if (swallowClick) { event.stopPropagation(); event.preventDefault(); }
  }, true);

  // ── Wiring ──
  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (!button || button.disabled) return;
    const zoom = state.scale;
    switch (button.dataset.act) {
      case "prev": navigate(state.page - 1); break;
      case "next": navigate(state.page + 1); break;
      case "film": {
        const strip = q(".pv-film");
        strip.hidden = !strip.hidden;
        button.setAttribute("aria-pressed", String(!strip.hidden));
        renderFilm();
        break;
      }
      case "fit": state.zoom = "fit"; renderPage(); break;
      case "zoom-100": state.zoom = 1; renderPage(); break;
      case "zoom-in": state.zoom = Math.min(4, zoom * 1.25); renderPage(); break;
      case "zoom-out": state.zoom = Math.max(0.1, zoom / 1.25); renderPage(); break;
      case "legend-all": toggleAllTypes(); break;
      case "retry-preview":
        preview.then((p) => p.destroy(), () => {});
        cache.clear();
        preview = openPreview(file, controller.signal, multipleFrames);
        adoptPreview();
        renderPage();
        renderFilm();
        break;
      case "blocks":
        state.showBlocks = !state.showBlocks;
        button.setAttribute("aria-pressed", String(state.showBlocks));
        renderPage();
        break;
    }
  });
  q(".pv-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) setTab(tab.dataset.tab);
  });
  q(".pv-tabs").addEventListener("keydown", (event) => {
    const tabs = [...root.querySelectorAll('.pv-tabs [role="tab"]:not([hidden])')];
    const index = tabs.indexOf(document.activeElement);
    if (index === -1) return;
    const target = { ArrowRight: tabs[(index + 1) % tabs.length], ArrowLeft: tabs[(index - 1 + tabs.length) % tabs.length],
      Home: tabs[0], End: tabs[tabs.length - 1] }[event.key];
    if (!target) return;
    event.preventDefault();
    setTab(target.dataset.tab);
    target.focus();
  });
  // Width changes include the tab becoming visible: a viewer mounted while
  // hidden measured 0px and must refit once shown.
  // The frame's border box ignores its own scrollbars, so zooming never loops.
  let lastSize = "";
  const resizeObserver = new ResizeObserver(() => {
    const size = `${wrap.offsetWidth}x${wrap.offsetHeight}`;
    if (!wrap.offsetWidth || size === lastSize) return;
    lastSize = size;
    if (state.zoom === "fit") renderPage(); else updatePan();
  });
  resizeObserver.observe(wrap);

  setTab("rendered");
  renderLegend();
  renderPanels();
  renderPage();

  return {
    destroy() {
      controller.abort();
      state.renderVersion++;
      state.filmVersion++;
      resizeObserver.disconnect();
      preview.then((p) => p.destroy(), () => {});
      root.remove();
    },
  };
}
