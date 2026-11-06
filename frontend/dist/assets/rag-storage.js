import { formatBytes } from "/assets/models.js?v=11";
import { node, iconButton, tableView, readableDate, failure, downloadBlob, archivePages } from "/assets/storage.js";
import { isRagRefined, isRagListed } from "/assets/rag-names.js";

export function mountRagStorage(root, api, { onSelect, onClear } = {}) {
  root.innerHTML = `
    <h2 class="section"><span class="section-num">01</span><span class="section-label">Saved JSON RAG</span></h2>
    <form class="storage-filters rag-storage-filters" id="rag-storage-filters">
      <div class="field"><label for="rag-storage-kind">JSON type</label>
        <select id="rag-storage-kind"><option value="">All JSON RAG</option><option value="original">Original only</option><option value="refined">Refined only</option></select></div>
      <div class="field"><label for="rag-storage-date-from">From (UTC)</label><input id="rag-storage-date-from" type="date" /></div>
      <div class="field"><label for="rag-storage-date-to">To (UTC)</label><input id="rag-storage-date-to" type="date" /></div>
      <div class="field"><label for="rag-storage-search">Search files</label><input id="rag-storage-search" type="search" placeholder="JSON RAG, parent file or archive ID" autocomplete="off" /></div>
      <button class="btn btn-primary" id="rag-storage-refresh" type="submit">Refresh</button>
    </form>
    <p class="storage-status" id="rag-storage-status" role="status"></p>
    <div id="rag-storage-files"></div>
    <nav class="storage-pagination" aria-label="JSON RAG pagination">
      <label>Rows per page <select id="rag-storage-page-size"><option selected>10</option><option>20</option><option>50</option></select></label>
      <span id="rag-storage-page-info" role="status"></span>
      <div class="storage-page-buttons"><button type="button" class="btn" id="rag-storage-prev" disabled>Previous</button><button type="button" class="btn" id="rag-storage-next" disabled>Next</button></div>
    </nav>
    <p class="storage-status" id="rag-storage-file-status" role="status"></p>
    <dialog class="storage-confirm" id="rag-storage-confirm" aria-labelledby="rag-storage-confirm-title">
      <h2 id="rag-storage-confirm-title">Delete file?</h2>
      <dl class="storage-confirm-facts" id="rag-storage-confirm-description"></dl><p class="alert">The file is permanently deleted from its archive.</p>
      <p id="rag-storage-confirm-status" class="storage-status" role="status"></p>
      <p id="rag-storage-confirm-error" class="alert alert-error" role="alert"></p>
      <div class="storage-confirm-actions"><button type="button" class="btn" id="rag-storage-confirm-cancel">Cancel</button><button type="button" class="btn storage-danger" id="rag-storage-confirm-delete">Delete file</button></div>
    </dialog>`;
  const find = id => root.querySelector(`#rag-storage-${id}`);
  const { wrapper, body } = tableView("Saved JSON RAG files", ["File JSON RAG", "Id", "Parent file", "Created", "Size", "Action"]);
  find("files").append(wrapper);
  let active = false, rows = [], page = 0, loading = false, loadFailed = false, invalidRange = false;
  let listRequest = null, previewRequest = null, selectedKey = null;
  let pendingDelete = null, deleting = false;
  const downloads = new Map();
  function message(id, text, error = false) {
    find(id).replaceChildren(...[].concat(text));
    find(id).className = error ? "storage-status alert alert-error" : "storage-status";
  }

  function render() {
    const focused = body.contains(document.activeElement) ? {
      key: document.activeElement.closest("tr")?.dataset.key,
      action: document.activeElement.dataset.action,
    } : null;
    const query = find("search").value.trim().toLowerCase();
    const wanted = find("kind").value;
    const matches = rows.filter(({ file, parent, archive }) =>
      (!wanted || isRagRefined(file.name) === (wanted === "refined"))
      && `${file.name} ${parent} ${archive.job_id} ${archive.prefix}`.toLowerCase().includes(query));
    const size = Number(find("page-size").value);
    page = Math.min(page, Math.max(0, Math.ceil(matches.length / size) - 1));
    const start = page * size;
    const shown = matches.slice(start, start + size);
    body.replaceChildren();
    find("page-info").textContent = `Page ${page + 1} · ${shown.length} of ${matches.length} files`;
    find("prev").disabled = page === 0;
    find("next").disabled = start + size >= matches.length;
    if (!matches.length) {
      const text = invalidRange ? "From date must not be after To date." : loading ? "Loading JSON RAG files…" : loadFailed
        ? "Files could not all be loaded. Refresh to try again."
        : query ? "No matching JSON RAG files."
          : "No JSON RAG files found. Enable json_rag when parsing, then refresh.";
      const line = node("tr"), cell = node("td", text, "storage-empty");
      cell.colSpan = 6; line.append(cell); body.append(line);
    }
    for (const row of shown) {
      const { file, parent, archive } = row;
      const line = node("tr");
      line.dataset.key = file.key;
      line.classList.toggle("is-selected", selectedKey === file.key);
      const name = node("td"), original = node("td"), actions = node("td");
      const code = node("td", archive.job_id, "storage-id-cell");
      code.title = archive.prefix;
      const choice = node("button", file.name, "storage-file-choice storage-name");
      choice.type = "button"; choice.title = file.key;
      choice.dataset.action = "preview-name";
      choice.setAttribute("aria-pressed", String(selectedKey === file.key));
      choice.addEventListener("click", () => selectFile(row));
      name.append(choice);
      original.append(node("span", parent));
      original.title = archive.prefix;
      if (archive.partial_ocr) original.append(node("span", "Partial OCR", "storage-partial"));
      const date = readableDate(archive.created_at), created = node("td", date.text, "storage-date-cell");
      created.title = date.title;
      const controls = node("div", "", "storage-actions");
      const view = iconButton("preview", `Preview and select ${file.name} · ${parent}`, "", "Preview and select");
      const save = iconButton("download", `Download ${file.name} · ${parent}`, "", "Download");
      view.dataset.action = "preview"; save.dataset.action = "download";
      view.addEventListener("click", () => selectFile(row));
      save.disabled = downloads.has(file.key);
      save.addEventListener("click", () => download(row));
      controls.append(view, save);
      // Only refined artifacts are removable: the source json_rag.json stays.
      if (isRagRefined(file.name)) {
        const remove = iconButton("delete", `Delete ${file.name}`, "storage-danger", "Delete");
        remove.dataset.action = "delete";
        remove.addEventListener("click", () => confirmDelete(row));
        controls.append(remove);
      }
      actions.append(controls);
      line.append(name, code, original, created, node("td", formatBytes(file.size), "storage-number"), actions);
      body.append(line);
    }
    if (focused) {
      const line = [...body.rows].find(row => row.dataset.key === focused.key);
      const control = line && [...line.querySelectorAll("button")].find(button => button.dataset.action === focused.action);
      const target = control?.disabled ? line.querySelector(".storage-file-choice") : control;
      target?.focus({ preventScroll: true });
    }
  }

  function cancelFiles() {
    previewRequest?.abort();
    downloads.forEach(controller => controller.abort());
    downloads.clear();
  }

  async function load() {
    listRequest?.abort(); previewRequest?.abort();
    const dateFrom = find("date-from").value, dateTo = find("date-to").value;
    if (dateFrom && dateTo && dateFrom > dateTo) {
      rows = []; page = 0; loading = false; loadFailed = false; invalidRange = true;
      find("files").removeAttribute("aria-busy");
      find("refresh").disabled = false;
      message("file-status", "");
      message("status", "From date must not be after To date.", true);
      render();
      return;
    }
    const controller = listRequest = new AbortController();
    const { signal } = controller;
    rows = []; page = 0; loading = true; loadFailed = false; invalidRange = false;
    // The table's own empty row shows "Loading JSON RAG files…".
    message("file-status", ""); message("status", "");
    find("refresh").disabled = true; find("files").setAttribute("aria-busy", "true");
    render();
    let skipped = 0, unavailable = 0, firstError = "";
    const seen = new Set();
    const filters = { date_from: dateFrom || undefined, date_to: dateTo || undefined };
    try {
      for await (const listing of archivePages(api, filters, signal)) {
        if (!active) return;
        skipped += Number(listing.skipped_archives) || 0;
        // OCR archives never hold a json_rag.json (only parse results produce it).
        const archives = listing.items.filter(archive =>
          !archive.incomplete && archive.kind !== "ocr" && !seen.has(archive.prefix));
        // Bound concurrent archive reads; never fetch file contents for the listing.
        for (let offset = 0; offset < archives.length; offset += 3) {
          const batch = archives.slice(offset, offset + 3);
          const results = await Promise.allSettled(batch.map(archive => api.storageArchive(archive.prefix, { signal })));
          if (signal.aborted || !active) return;
          results.forEach((outcome, index) => {
            const archive = batch[index];
            seen.add(archive.prefix);
            const detail = outcome.status === "fulfilled" ? outcome.value : { ok: false };
            if (!detail.ok || !Array.isArray(detail.data?.files)) {
              unavailable++;
              firstError ||= failure(detail, "Archive files are unavailable.");
              return;
            }
            const parent = detail.data.manifest?.source?.original_name || archive.filename || "Original filename unavailable";
            for (const file of detail.data.files) {
              if (isRagListed(file.name)) rows.push({ file, parent, archive });
            }
          });
          render();
        }
      }
      if (signal.aborted || !active) return;
      loadFailed = unavailable > 0;
      if (selectedKey && !rows.some(row => row.file.key === selectedKey)) {
        selectedKey = null;
        onClear?.();
      }
      message("status", (skipped ? `${skipped} archives still being written are not shown yet.` : "")
        + (unavailable ? ` Listing incomplete: ${unavailable} archives could not be read. ${firstError} Refresh to retry.` : ""), loadFailed);
    } catch (error) {
      if (signal.aborted || !active) return;
      loadFailed = true;
      message("status", `Listing incomplete. ${error.message} Refresh to retry.`, true);
    } finally {
      if (!signal.aborted && active) {
        loading = false; find("refresh").disabled = false;
        find("files").removeAttribute("aria-busy"); render();
      }
    }
  }

  async function selectFile({ file, parent, archive }) {
    if (selectedKey === file.key && !previewRequest) {
      message("file-status", "");
      return;
    }
    previewRequest?.abort();
    const controller = previewRequest = new AbortController();
    const done = () => {
      if (previewRequest === controller) previewRequest = null;
      if (!loading && !previewRequest) find("files").removeAttribute("aria-busy");
    };
    message("file-status", "");
    find("files").setAttribute("aria-busy", "true");
    const result = await api.storageFile(file.key, { signal: controller.signal });
    if (controller.signal.aborted || !active) return done();
    if (!result.ok) {
      const text = failure(result, "JSON RAG file unavailable. Refresh and try again.");
      message("file-status", ["Selection of ", node("span", file.name, "literal"), ` failed: ${text}`], true);
      return done();
    }
    const contents = await result.data.text();
    if (controller.signal.aborted || !active) return done();
    let value;
    try { value = JSON.parse(contents); } catch { /* Invalid stored JSON is reported below. */ }
    if (!onSelect?.(value, { ...file, sha256: result.sha256 }, parent)) {
      const text = "This file is not a valid JSON RAG document.";
      message("file-status", ["Selection of ", node("span", file.name, "literal"), ` failed: ${text}`], true);
      return done();
    }
    selectedKey = file.key;
    // Only errors appear here: the row and section 02 already show the selection.
    message("file-status", "");
    done(); render();
  }

  async function download({ file, parent }) {
    if (downloads.has(file.key)) return;
    const controller = new AbortController();
    downloads.set(file.key, controller); render();
    try {
      const result = await api.storageFile(file.key, { signal: controller.signal });
      if (controller.signal.aborted || !active) return;
      if (!result.ok) {
        message("file-status", ["Download of ", node("span", file.name, "literal"), ` failed: ${failure(result, "Download failed.")}`], true);
        return;
      }
      downloadBlob(result.data, file.name.split("/").pop());
      message("file-status", "");
    } catch {
      if (!controller.signal.aborted && active) message("file-status",
        ["Download of ", node("span", file.name, "literal"), " failed. Try again."], true);
    } finally {
      if (downloads.get(file.key) === controller) downloads.delete(file.key);
      if (active) render();
    }
  }

  function confirmDelete({ file, parent, archive }) {
    pendingDelete = { file, parent, archive };
    find("confirm-description").replaceChildren(
      ...[["File", file.name], ["Parent file", parent], ["Archive code", archive.job_id]]
        .flatMap(([term, value]) => [node("dt", term), node("dd", value)]));
    find("confirm-error").textContent = "";
    find("confirm-status").textContent = "";
    find("confirm").showModal();
    find("confirm-cancel").focus();
  }

  find("filters").addEventListener("submit", event => { event.preventDefault(); load(); });
  for (const id of ["date-from", "date-to"]) find(id).addEventListener("change", load);
  find("search").addEventListener("input", () => { page = 0; render(); });
  find("kind").addEventListener("change", () => { page = 0; render(); });
  find("page-size").addEventListener("change", () => { page = 0; render(); });
  find("prev").addEventListener("click", () => { page--; render(); if (find("prev").disabled) find("next").focus(); });
  find("next").addEventListener("click", () => { page++; render(); if (find("next").disabled) find("prev").focus(); });
  find("confirm-cancel").addEventListener("click", () => find("confirm").close());
  find("confirm").addEventListener("cancel", event => { if (deleting) event.preventDefault(); });
  find("confirm-delete").addEventListener("click", async () => {
    if (!pendingDelete || deleting) return;
    const { file } = pendingDelete;
    deleting = true;
    find("confirm-cancel").disabled = find("confirm-delete").disabled = true;
    find("confirm-status").textContent = "Deleting file…";
    try {
      const result = await api.deleteStorageFile(file.key, file.name);
      if (!result.ok) {
        find("confirm-error").textContent = failure(result, "Deletion failed. Refresh and retry.");
        return;
      }
      find("confirm").close();
      pendingDelete = null;
      rows = rows.filter(row => row.file.key !== file.key);
      if (selectedKey === file.key) {
        selectedKey = null;
        onClear?.();
      }
      render();
      message("file-status", "");
    } finally {
      deleting = false;
      find("confirm-status").textContent = "";
      find("confirm-cancel").disabled = find("confirm-delete").disabled = false;
    }
  });
  render();
  return {
    activate() { active = true; load(); },
    deactivate() { active = false; listRequest?.abort(); cancelFiles(); },
    added({ prefix, file }) {
      let last = -1, match = null;
      rows.forEach((row, index) => {
        if (row.archive.prefix === prefix) { last = index; match = row; }
      });
      if (!match || rows.some(row => row.file.key === file.key)) return;
      rows.splice(last + 1, 0, { file, parent: match.parent, archive: match.archive });
      render();
    },
  };
}
