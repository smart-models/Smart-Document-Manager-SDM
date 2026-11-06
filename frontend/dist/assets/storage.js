import { formatBytes } from "/assets/models.js?v=11";

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(text, className = "btn") {
  const element = node("button", text, className);
  element.type = "button";
  return element;
}

const ICONS = {
  open: '<path d="M3 7h6l2 2h10l-3 11H3z"/><path d="M3 7V4h6l2 3h7v2"/>',
  preview: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  zip: '<path d="M4 3h16v18H4zM9 3v3m6-3v3M9 9h6m-6 4h6m-6 4h6M9 6h6"/><path d="m10 13 2 2 2-2"/>',
  delete: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  scissors: '<circle cx="6" cy="6" r="2.4"/><circle cx="6" cy="18" r="2.4"/><path d="m8 7.5 12 8.5M8 16.5 20 8M14.5 12 20 14"/>',
  gluePrevious: '<path d="M4 12h11M9 6l-5 6 5 6"/><rect x="17" y="6" width="4" height="12" rx="1"/>',
  glueNext: '<path d="M20 12H9M15 6l5 6-5 6"/><rect x="3" y="6" width="4" height="12" rx="1"/>',
  undo: '<path d="M9 7 4 12l5 5"/><path d="M4 12h10a6 6 0 0 1 0 12"/>',
  sortable: '<path d="m8 10 4-4 4 4M8 14l4 4 4-4"/>',
  ascending: '<path d="m7 14 5-5 5 5"/>',
  descending: '<path d="m7 10 5 5 5-5"/>',
};

const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;

function iconButton(name, label, extraClass = "", tooltip = "") {
  const control = button("", `storage-action ${extraClass}`);
  control.title = tooltip || label;
  control.setAttribute("aria-label", label);
  control.innerHTML = icon(name);
  return control;
}

// With `sorting` ({ keys, key, dir, onSort }), a column that has a key gets a
// sort button and the sorted one carries aria-sort.
function tableView(label, columns, sorting = null) {
  const wrapper = node("div", "", "storage-table-wrap");
  wrapper.tabIndex = 0;
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-label", label);
  const table = node("table", "", "storage-table");
  const caption = node("caption", label, "storage-sr-only");
  const head = node("thead"), row = node("tr"), body = node("tbody");
  columns.forEach((title, index) => {
    const key = sorting?.keys[index], cell = node("th", key ? "" : title);
    cell.scope = "col";
    if (key) {
      const order = sorting.key !== key ? "" : sorting.dir > 0 ? "ascending" : "descending";
      if (order) cell.setAttribute("aria-sort", order);
      const control = button(title, "storage-sort");
      control.dataset.sort = key; control.title = `Sort by ${title}`;
      control.insertAdjacentHTML("beforeend", icon(order || "sortable"));
      control.addEventListener("click", () => sorting.onSort(key));
      cell.append(control);
    }
    row.append(cell);
  });
  head.append(row); table.append(caption, head, body); wrapper.append(table);
  return { wrapper, body };
}

// Shown in UTC, like the From/To filters, so a row never appears on a day the
// filter excludes; the title keeps the local time.
function readableDate(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return { text: "Date unavailable", title: "" };
  return {
    text: date.toISOString().slice(0, 16).replace("T", " "),
    title: `Local time: ${date.toLocaleString()}`,
  };
}

function failure(result, fallback, action = "") {
  const code = result.data?.detail?.code || result.data?.code;
  const messages = {
    storage_browser_disabled: "Archive browsing is not enabled on this server.",
    storage_browser_busy: "Storage is busy. Wait a moment and try again.",
    storage_unavailable: "Storage is unavailable. Try again.",
    storage_not_found: "This archive or file is no longer available. Refresh the list.",
    storage_write_disabled: "Storage writing is not enabled on this server.",
    storage_archive_busy: "This archive is being updated. Wait a moment and try again.",
    storage_too_many_objects: "This archive has reached its file limit. Remove an old artifact before saving another.",
    rag_source_changed: "The source file changed after preflight. Select it and run preflight again.",
    rag_execution_conflict: "That execution ID was already used for a different result. Run preflight again.",
    rag_invalid_plan: "The preflight plan is invalid. Run preflight again.",
    rag_invalid_source: "The selected file is not a valid stored JSON RAG source.",
    rag_plan_too_large: "The plan contains too many groups. Adjust the target range and run preflight again.",
    rag_request_timeout: "The plan could not be uploaded in time. Try again.",
    storage_delete_partial: "Some files remain. Retry deletion or refresh the archive.",
    storage_file_too_large: "This file exceeds the size limit for this action. Try Download if preview is unavailable.",
    storage_archive_too_large: "This archive exceeds the download size limit.",
    storage_preview_unsupported: "Preview is unavailable for this file type. Use Download to open it.",
    storage_invalid_manifest: "The archive metadata is damaged or incomplete.",
    storage_archive_incomplete: "This archive has missing or changed files. Refresh it or retry an incomplete deletion before saving or downloading a complete ZIP.",
    storage_confirmation_timeout: "Deletion confirmation timed out. No deletion was started. Try again.",
    storage_archive_in_progress: "This archive is still being written. Try again in a few minutes.",
    storage_listing_too_large: "Too many objects to list at once. Narrow the date range.",
    storage_invalid_listing: "Storage returned an inconsistent listing. Refresh to try again.",
    storage_invalid_cursor: "The page position expired. Refresh from the first page.",
    storage_confirmation_required: "Deletion needs the exact archive code. Reopen the dialog.",
    rate_limited: "Too many requests. Wait a few seconds and retry.",
  };
  if (code === "storage_archive_too_large" && action) {
    return `This archive has too many files or bytes to ${action}.`;
  }
  return messages[code] || result.error || fallback;
}

export { node, iconButton, tableView, readableDate, failure };

// Follow every cursor of an archive listing, yielding each page's response body.
// Ends quietly once `signal` aborts. Throws a displayable Error on a failed or
// malformed response, and on a repeated cursor, which would otherwise never end.
export async function* archivePages(api, filters, signal) {
  const cursors = new Set();
  let cursor;
  do {
    const result = await api.storageArchives({ ...filters, cursor, limit: 50 }, { signal });
    if (signal.aborted) return;
    if (!result.ok || !Array.isArray(result.data?.items)) throw new Error(failure(result, "Storage returned an unexpected response."));
    yield result.data;
    cursor = result.data.next_cursor || undefined;
    if (cursors.has(cursor)) throw new Error("Archive pagination did not advance. Refresh to try again.");
    cursors.add(cursor);
  } while (cursor);
}

const natural = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
// The value each sortable column orders by, in column order; null means missing.
const SORT_VALUES = {
  file: row => row.filename || row.job_id,
  kind: row => row.kind,
  created: row => { const time = Date.parse(row.created_at); return Number.isNaN(time) ? null : time; },
  files: row => row.incomplete ? null : row.object_count,
  size: row => row.incomplete ? null : row.total_bytes,
};

// What an archive row displays beyond its name and kind, shared by its cells and
// the search so the two cannot disagree.
function rowFacts(row) {
  return {
    created: readableDate(row.created_at),
    files: row.incomplete ? "—" : String(row.object_count),
    size: row.incomplete ? "—" : formatBytes(row.total_bytes),
    marks: [row.partial_ocr && "Partial OCR", row.incomplete && "Incomplete"].filter(Boolean),
  };
}

// Everything a search matches: what the row shows, plus its storage prefix.
function searchText(row) {
  const { created, files, size, marks } = rowFacts(row);
  return [row.filename, row.job_id, row.kind, row.prefix, created.text, files, size, ...marks].join(" ").toLowerCase();
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob), link = node("a");
  link.href = url; link.download = filename;
  try { document.body.append(link); link.click(); }
  finally {
    link.remove();
    // Firefox and Safari may read the Blob after click() returns.
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  return link.download;
}

export function mountStorage(root, api) {
  root.innerHTML = `
    <form class="storage-filters" id="storage-filters">
      <div class="field"><label for="storage-kind">Output type</label>
        <select id="storage-kind"><option value="">All outputs</option><option value="parse">Parse</option><option value="ocr">OCR</option></select></div>
      <div class="field"><label for="storage-date-from">From (UTC)</label><input id="storage-date-from" type="date" /></div>
      <div class="field"><label for="storage-date-to">To (UTC)</label><input id="storage-date-to" type="date" /></div>
      <div class="field"><label for="storage-search">Search archives</label><input id="storage-search" type="search" placeholder="Any column" autocomplete="off" /></div>
      <button class="btn btn-primary" id="storage-refresh" type="submit">Refresh</button>
    </form>
    <p class="storage-status" id="storage-status" role="status"></p>
    <div class="storage-workspace">
      <div class="storage-list"><div id="storage-archives" aria-label="Saved archives"></div>
        <nav class="storage-pagination" aria-label="Archive pagination">
          <label>Rows per page <select id="storage-page-size"><option selected>10</option><option>20</option><option>50</option></select></label>
          <span id="storage-page-info" role="status"></span>
          <span id="storage-search-status" class="storage-sr-only" role="status"></span>
          <div class="storage-page-buttons"><button type="button" class="btn" id="storage-prev" disabled>Previous</button><button type="button" class="btn" id="storage-next" disabled>Next</button></div>
        </nav>
      </div>
      <section class="storage-detail" id="storage-detail" aria-label="Archive files"><p class="storage-empty">Select an archive to view its files.</p></section>
    </div>
    <dialog class="storage-confirm" id="storage-confirm" aria-labelledby="storage-confirm-title">
      <h2 id="storage-confirm-title">Delete archive?</h2>
      <dl class="storage-confirm-facts" id="storage-confirm-description"></dl><p class="alert">All files in this archive will be permanently deleted.</p>
      <p id="storage-confirm-error" class="alert alert-error" role="alert"></p>
      <div class="storage-confirm-actions"><button type="button" class="btn" id="storage-cancel">Cancel</button><button type="button" class="btn storage-danger" id="storage-delete-confirm">Delete archive</button></div>
    </dialog>`;
  const find = id => root.querySelector(`#storage-${id}`);
  const list = find("archives"), detail = find("detail"), status = find("status");
  const dialog = find("confirm");
  let active = false, archives = [], pageIndex = 0, sort = { key: "created", dir: -1 };
  // Replaces the empty-table text while a load runs or after it failed.
  let listNotice = null;
  let selected = null, pendingDelete = null, deleting = false, imageUrl = null;
  let listRequest = null, detailRequest = null, previewRequest = null, fileStatus = null;
  const downloads = new Set();

  function releaseImage() {
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
  }
  function message(text, error = false) {
    status.replaceChildren(...[].concat(text));
    status.className = error ? "storage-status alert alert-error" : "storage-status";
  }
  function clearDetail() {
    detailRequest?.abort(); previewRequest?.abort(); releaseImage();
    selected = null; fileStatus = null;
    detail.removeAttribute("aria-busy");
    detail.replaceChildren(node("p", "Select an archive to view its files.", "storage-empty"));
  }
  // Missing values sort last either way; ties keep the server's newest-first order.
  function compare(a, b) {
    const x = SORT_VALUES[sort.key](a), y = SORT_VALUES[sort.key](b);
    if (x === null || y === null) return (x === null) - (y === null);
    return sort.dir * (typeof x === "string" ? natural.compare(x, y) : x - y);
  }
  // A new column starts ascending; the same column flips. Back to the first page.
  function sortBy(key) {
    sort = { key, dir: sort.key === key ? -sort.dir : 1 };
    pageIndex = 0; displayRows();
  }
  // Render the current page of the sorted archives matching the search. The table
  // is rebuilt, so carry over the focused sort header and the horizontal scroll.
  function displayRows() {
    const size = Number(find("page-size").value);
    const query = find("search").value.trim().toLowerCase();
    const matches = archives.filter(row => !query || searchText(row).includes(query)).sort(compare);
    const pages = Math.max(1, Math.ceil(matches.length / size));
    // A notice means the list is empty for now: keep pageIndex to return to it.
    const page = Math.min(pageIndex, pages - 1);
    if (!listNotice) pageIndex = page;
    const rows = matches.slice(page * size, (page + 1) * size);
    const scrollLeft = list.firstElementChild?.scrollLeft || 0;
    const focusedSort = document.activeElement?.dataset?.sort;
    const { wrapper, body } = tableView("Saved archives", ["File", "Output", "Created", "Files", "Size", "Action"],
      { keys: Object.keys(SORT_VALUES), ...sort, onSort: sortBy });
    list.replaceChildren(wrapper);
    find("page-info").textContent = `Page ${page + 1} of ${pages}`;
    find("search-status").textContent = query && !listNotice ? `${matches.length} ${matches.length === 1 ? "match" : "matches"}` : "";
    find("prev").disabled = page === 0;
    find("next").disabled = page === pages - 1;
    if (!rows.length) {
      const text = listNotice || (query ? "No matching archives." : "No saved archives found. Run Parse or OCR, then refresh.");
      const cell = node("td", text, "storage-empty"); cell.colSpan = 6;
      const line = node("tr"); line.append(cell); body.append(line);
    }
    for (const row of rows) {
      const line = node("tr");
      line.classList.toggle("is-selected", selected?.prefix === row.prefix);
      const label = rowLabel(row), facts = rowFacts(row);
      const name = node("td"), kind = node("td");
      if (row.incomplete) {
        // An abandoned write: nothing to open or download, only to delete.
        name.append(node("span", row.filename || row.job_id, "storage-name"));
      } else {
        const choice = button(row.filename || row.job_id, "storage-archive storage-name");
        choice.title = row.prefix;
        choice.setAttribute("aria-pressed", String(selected?.prefix === row.prefix));
        choice.addEventListener("click", () => selectArchive(row));
        name.append(choice);
      }
      if (row.filename) name.append(node("small", `id: ${row.job_id}`, "storage-job-code"));
      kind.append(node("span", row.kind, "storage-kind"));
      for (const mark of facts.marks) kind.append(node("span", mark, "storage-partial"));
      const actions = node("td"), controls = node("div", "", "storage-actions");
      if (!row.incomplete) {
        const open = iconButton("open", `Open archive ${label}`, "", "Open");
        open.addEventListener("click", () => selectArchive(row));
        const save = iconButton("zip", `Download ${label} as ZIP`, "", "Download ZIP");
        save.addEventListener("click", () => downloadArchive(row, save));
        controls.append(open, save);
        if (row.input_key) {
          const source = iconButton("download", `Download source of ${label}`, "", "Download source");
          source.addEventListener("click", () => download({ key: row.input_key, name: sourceName(row) }, source, row));
          controls.append(source);
        }
      }
      const remove = iconButton("delete", `Delete archive ${label}`, "storage-danger", "Delete");
      remove.addEventListener("click", () => confirmDelete(row));
      controls.append(remove); actions.append(controls);
      const date = node("td", facts.created.text, "storage-date-cell");
      if (facts.created.title) date.title = facts.created.title;
      line.append(name, kind, date, node("td", facts.files, "storage-number"), node("td", facts.size, "storage-number"), actions);
      body.append(line);
    }
    // After the rows: an empty table would clamp the scroll back to zero.
    wrapper.scrollLeft = scrollLeft;
    if (focusedSort) list.querySelector(`[data-sort="${focusedSort}"]`).focus({ preventScroll: true });
  }
  function rowLabel(row) {
    return row.filename ? `${row.filename} (${row.job_id})` : row.job_id;
  }
  // The uploaded name, with the extension the archive detected for its bytes.
  function sourceName(row) {
    const stored = row.input_key.split("/").pop();
    return row.filename ? row.filename.replace(/\.[^.]*$/, "") + stored.slice(stored.lastIndexOf(".")) : stored;
  }
  // Load every archive of the filters, so any column can sort them all and the
  // console pages locally. With keepSelection (Refresh) stay on the viewed page
  // and reopen the open archive. A page that fails keeps the rows already loaded.
  // ponytail: each load reads every manifest in range (4.6 s measured for 257
  // archives); move sorting server-side if archives reach the thousands.
  async function loadArchives({ keepSelection = false } = {}) {
    const dateFrom = find("date-from").value, dateTo = find("date-to").value;
    if (!keepSelection) pageIndex = 0;
    if (dateFrom && dateTo && dateFrom > dateTo) {
      // Stale rows would still be paged with the previous filter.
      const invalid = "From date must not be after To date.";
      listRequest?.abort(); clearDetail();
      archives = []; listNotice = invalid; displayRows();
      list.removeAttribute("aria-busy");
      find("refresh").disabled = false;
      message(invalid, true);
      return;
    }
    // Loading disables Refresh: hand focus back instead of dropping it on <body>.
    const held = document.activeElement === find("refresh");
    listRequest?.abort();
    const controller = listRequest = new AbortController();
    const reopen = keepSelection ? selected : null;
    clearDetail();
    archives = []; listNotice = "Loading saved archives…"; displayRows();
    find("refresh").disabled = true;
    message(listNotice);
    list.setAttribute("aria-busy", "true");
    const filters = { kind: find("kind").value || undefined, date_from: dateFrom || undefined, date_to: dateTo || undefined };
    const loaded = [];
    let skipped = 0, error = "";
    try {
      for await (const listing of archivePages(api, filters, controller.signal)) {
        loaded.push(...listing.items);
        skipped += Number(listing.skipped_archives) || 0;
        if (listing.next_cursor) message(`Loading saved archives… ${loaded.length}`);
      }
    } catch (failed) { error = failed.message; }
    // A newer load or leaving the tab (deactivate aborts too) owns the list now.
    if (controller.signal.aborted) return;
    list.removeAttribute("aria-busy");
    find("refresh").disabled = false;
    archives = loaded;
    listNotice = error && !loaded.length ? "Saved files could not be loaded. Refresh to try again." : null;
    if (error) message(loaded.length ? `Listing incomplete. ${error} Refresh to retry.` : error, true);
    else message(skipped ? `${skipped} archives still being written are not shown yet.` : "");
    displayRows();
    if (held && document.activeElement === document.body) find("refresh").focus({ preventScroll: true });
    const again = reopen && archives.find(row => row.prefix === reopen.prefix);
    if (again) selectArchive(again, { moveFocus: false });
  }
  function confirmDelete(row) {
    pendingDelete = row;
    // Many uploads share a name (input.png): show what tells them apart.
    const facts = [["File", row.filename || "—"], ["Code", row.job_id],
      ["Created", readableDate(row.created_at).text], ["Output", row.kind],
      ["Files", row.incomplete ? "incomplete archive" : String(row.object_count)]];
    find("confirm-description").replaceChildren(...facts.flatMap(([term, value]) =>
      [node("dt", term), node("dd", value)]));
    find("confirm-error").textContent = "";
    dialog.showModal(); find("cancel").focus();
  }
  async function selectArchive(row, { moveFocus = true } = {}) {
    detailRequest?.abort(); previewRequest?.abort(); releaseImage();
    const controller = detailRequest = new AbortController();
    selected = row; displayRows();
    detail.setAttribute("aria-busy", "true");
    const loading = node("p", "Loading archive files…", "storage-status");
    loading.tabIndex = -1;
    detail.replaceChildren(loading);
    if (moveFocus) loading.focus({ preventScroll: true });
    const scrollX = window.scrollX, scrollY = window.scrollY;
    const result = await api.storageArchive(row.prefix, { signal: controller.signal });
    if (controller.signal.aborted || !active) return;
    detail.removeAttribute("aria-busy");
    if (!result.ok || !Array.isArray(result.data?.files)) {
      detail.replaceChildren(node("p", failure(result, "Archive files are unavailable.", "list"), "alert alert-error"));
      return;
    }
    const head = node("div", "", "storage-detail-head");
    const heading = node("div");
    const title = node("h2", `${row.kind.toUpperCase()} ARCHIVE`);
    title.tabIndex = -1;
    heading.append(title, node("p", row.prefix, "storage-prefix"));
    head.append(heading);
    const { wrapper: files, body } = tableView("Archive files", ["File name", "Type", "Size", "Action"]);
    // Not a live region: a screen reader would read up to 1 MB of preview text.
    // Short outcomes go to the status line under the file table instead.
    const preview = node("section", "", "storage-preview");
    preview.setAttribute("aria-label", "File preview");
    fileStatus = node("p", "", "storage-status");
    fileStatus.setAttribute("role", "status");
    let filePage = 0, filePageSize = 10, selectedKey = null;
    const pagination = node("nav", "", "storage-pagination");
    pagination.setAttribute("aria-label", "File pagination");
    const sizeLabel = node("label", "Rows per page ");
    const size = node("select");
    for (const count of [10, 20, 50]) { const option = node("option", String(count)); option.value = count; size.append(option); }
    sizeLabel.append(size);
    const info = node("span"); info.setAttribute("role", "status");
    const previous = button("Previous"), next = button("Next"), pageButtons = node("div", "", "storage-page-buttons");
    pageButtons.append(previous, next); pagination.append(sizeLabel, info, pageButtons);
    function renderFiles() {
      body.replaceChildren();
      const total = result.data.files.length, start = filePage * filePageSize;
      info.textContent = `${total ? start + 1 : 0}–${Math.min(start + filePageSize, total)} of ${total} files · Page ${filePage + 1} of ${Math.max(1, Math.ceil(total / filePageSize))}`;
      previous.disabled = filePage === 0; next.disabled = start + filePageSize >= total;
      if (!total) {
        const line = node("tr"), cell = node("td", "No files in this archive.", "storage-empty");
        cell.colSpan = 4; line.append(cell); body.append(line);
      }
      for (const file of result.data.files.slice(start, start + filePageSize)) {
        const line = node("tr", "", "storage-file");
        const choose = button(file.name, "storage-file-choice storage-name");
        const view = iconButton("preview", `Preview ${file.name}`, "", "Preview");
        const show = () => {
          selectedKey = file.key;
          body.querySelectorAll("tr").forEach(item => {
            const chosen = item === line;
            item.classList.toggle("is-selected", chosen);
            item.querySelector(".storage-file-choice")?.setAttribute("aria-pressed", String(chosen));
          });
          showPreview(file, preview);
        };
        choose.addEventListener("click", show); view.addEventListener("click", show);
        line.classList.toggle("is-selected", selectedKey === file.key);
        choose.setAttribute("aria-pressed", String(selectedKey === file.key));
        const save = iconButton("download", `Download ${file.name}`, "", "Download");
        save.addEventListener("click", () => download(file, save));
        const name = node("td"), actions = node("td"), controls = node("div", "", "storage-actions");
        name.append(choose); controls.append(view, save); actions.append(controls);
        line.append(name, node("td", file.content_type || "file", "storage-type-cell"), node("td", formatBytes(file.size), "storage-number"), actions);
        body.append(line);
      }
    }
    // The page buttons disable at the ends: keep focus on the other one.
    previous.addEventListener("click", () => { filePage--; renderFiles(); if (previous.disabled) next.focus(); });
    next.addEventListener("click", () => { filePage++; renderFiles(); if (next.disabled) previous.focus(); });
    size.addEventListener("change", () => { filePageSize = Number(size.value); filePage = 0; renderFiles(); });
    renderFiles();
    // Only continue the opening interaction if the user is still waiting here.
    const stillWaiting = document.activeElement === loading
      && window.scrollX === scrollX && window.scrollY === scrollY;
    detail.replaceChildren(head, files, pagination, fileStatus, preview);
    if (stillWaiting) {
      title.focus({ preventScroll: true });
      detail.scrollIntoView({ block: "nearest" });
    }
  }
  async function showPreview(file, target) {
    previewRequest?.abort(); releaseImage();
    const controller = previewRequest = new AbortController();
    target.replaceChildren(node("h3", file.name), node("p", "Loading preview…", "storage-meta"));
    const type = file.content_type || "";
    const png = type === "image/png" || file.name.toLowerCase().endsWith(".png");
    const text = type.startsWith("text/") || /\.(json|md|txt)$/i.test(file.name);
    if (!png && !text) {
      target.replaceChildren(node("h3", file.name), node("p", "Preview is unavailable for this file type. Use Download to open it.", "storage-meta"));
      return;
    }
    const result = await api.storageFile(file.key, { preview: true, signal: controller.signal });
    if (controller.signal.aborted || !active) return;
    target.replaceChildren(node("h3", file.name));
    if (!result.ok) {
      const text = failure(result, "Preview unavailable. Try downloading the file.");
      target.append(node("p", text, "alert alert-error"));
      fileMessage(["Preview of ", node("span", file.name, "literal"), ` failed: ${text}`], true);
      return;
    }
    fileMessage(["Previewing ", node("span", file.name, "literal"), "."]);
    if (png) {
      // Use a fixed safe media type; never execute stored HTML/SVG documents.
      imageUrl = URL.createObjectURL(new Blob([result.data], { type: "image/png" }));
      const image = node("img"); image.alt = file.name; image.src = imageUrl;
      target.append(image);
    } else {
      let contents = await result.data.text();
      if (controller.signal.aborted || !active) return;
      if (file.name.endsWith(".json")) {
        try { contents = JSON.stringify(JSON.parse(contents), null, 2); } catch { /* Keep original text. */ }
      }
      target.append(node("pre", contents));
    }
  }
  // File actions report next to the file table and name the file, so one
  // outcome never silently replaces another.
  function fileMessage(text, error = false) {
    if (!fileStatus) return;
    fileStatus.replaceChildren(...[].concat(text));
    fileStatus.className = error ? "storage-status alert alert-error" : "storage-status";
  }
  function downloadMessage(origin, text, error = false) {
    if (selected?.prefix === origin.prefix && fileStatus) fileMessage(text, error);
    else message(["Archive ", node("span", rowLabel(origin), "literal"), ": ", ...[].concat(text)], error);
  }
  async function download(file, control, origin = selected) {
    const report = (text, error = false) => downloadMessage(origin, text, error);
    const controller = new AbortController(); downloads.add(controller); control.disabled = true;
    try {
      const result = await api.storageFile(file.key, { signal: controller.signal });
      if (controller.signal.aborted || !active) return;
      if (!result.ok) { report(["Download of ", node("span", file.name, "literal"), ` failed: ${failure(result, "Download failed.")}`], true); return; }
      const filename = downloadBlob(result.data, file.name.split("/").pop());
      report(["Downloaded ", node("span", filename, "literal"), "."]);
    } catch (error) {
      if (active && !controller.signal.aborted) report(`Download of ${file.name} failed: ${error.message || "download unavailable"}`, true);
    } finally { downloads.delete(controller); control.disabled = false; }
  }
  async function downloadArchive(row, control) {
    const controller = new AbortController(); downloads.add(controller); control.disabled = true;
    try {
      const result = await api.storageArchiveZip(row.prefix, { signal: controller.signal });
      if (controller.signal.aborted || !active) return;
      if (!result.ok) { message(["ZIP of ", node("span", rowLabel(row), "literal"), ` failed: ${failure(result, "ZIP download failed.", "download as a ZIP")}`], true); return; }
      const filename = downloadBlob(result.data, `${row.prefix.replace(/\/$/, "").split("/").pop()}.zip`);
      message(["Downloaded ", node("span", filename, "literal"), "."]);
    } catch (error) {
      if (active && !controller.signal.aborted) message(`ZIP of ${rowLabel(row)} failed: ${error.message || "download unavailable"}`, true);
    } finally { downloads.delete(controller); control.disabled = false; }
  }
  // Refresh reloads and stays on the page viewed; a filter starts over;
  // the page size only re-pages what is loaded.
  find("filters").addEventListener("submit", event => { event.preventDefault(); loadArchives({ keepSelection: true }); });
  for (const name of ["kind", "date-from", "date-to"]) find(name).addEventListener("change", () => loadArchives());
  find("page-size").addEventListener("change", () => { pageIndex = 0; displayRows(); });
  find("search").addEventListener("input", () => { pageIndex = 0; displayRows(); });
  // Enter would submit the form and reload every archive for nothing.
  find("search").addEventListener("keydown", event => { if (event.key === "Enter") event.preventDefault(); });
  // The page buttons disable at the ends: keep focus on the other one.
  find("next").addEventListener("click", () => { pageIndex++; displayRows(); if (find("next").disabled) find("prev").focus(); });
  find("prev").addEventListener("click", () => { pageIndex--; displayRows(); if (find("prev").disabled) find("next").focus(); });
  find("cancel").addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", event => { if (deleting) event.preventDefault(); });
  find("delete-confirm").addEventListener("click", async () => {
    if (!pendingDelete || deleting) return;
    deleting = true;
    find("cancel").disabled = find("delete-confirm").disabled = true;
    find("confirm-error").textContent = "Deleting archive…";
    try {
      const result = await api.deleteStorageArchive(pendingDelete.prefix, pendingDelete.job_id);
      if (!result.ok) { find("confirm-error").textContent = failure(result, "Deletion failed. Some files may remain; retry or refresh."); return; }
      // Drop the row instead of reloading every archive: stay on this page and
      // keep another archive's files open.
      const removed = pendingDelete.prefix;
      dialog.close(); pendingDelete = null;
      if (selected?.prefix === removed) clearDetail();
      archives = archives.filter(row => row.prefix !== removed); displayRows();
      if (active) {
        message("Archive deleted.");
        find("refresh").focus();
      }
    } finally { deleting = false; find("cancel").disabled = find("delete-confirm").disabled = false; }
  });
  return {
    activate() { active = true; loadArchives(); },
    deactivate() {
      active = false; listRequest?.abort(); detailRequest?.abort(); previewRequest?.abort();
      downloads.forEach(controller => controller.abort()); releaseImage();
    },
  };
}
