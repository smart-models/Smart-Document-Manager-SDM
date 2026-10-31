import { metricTiles } from "/assets/models.js?v=11";
import { planRagAggregation, ragGroupPreview, ragAggregationPreview, groupTokenCount, entryText, tokenRangeStatus } from "/assets/rag-preflight.js";
import { ragStructurePresence, ragStructureBoundaries, ragTextSeparator, hasPage, pageOf } from "/assets/rag-structure.js";
import { gestureFor, applySplit, glueGroups, undoGesture, resetGestures, countsOf, capState, sameChunk, memberCounts, noteCode, noteText } from "/assets/rag-repair.js";
import { node, iconButton, tableView, failure } from "/assets/storage.js";
import { isRagSource } from "/assets/rag-names.js";

export function mountRagSelection(root, api, executionRoot, { onSaved } = {}) {
  root.classList.add("rag-workspace");
  executionRoot.innerHTML = `
    <h2 class="section"><span class="section-num">04</span><span class="section-label">Execute refinement</span></h2>
    <details class="reference rag-selected-json-preview" id="rag-execution-json-details" hidden>
      <summary id="rag-execution-json-summary">JSON preview</summary>
      <p id="rag-execution-json-note" class="storage-meta" hidden></p>
      <pre id="rag-execution-json"></pre>
    </details>
    <p id="rag-execution-state" class="storage-status" role="status"></p>
    <button type="button" id="rag-execute" class="btn btn-primary" disabled>Execute and save JSON RAG</button>`;
  root.innerHTML = `
    <div id="rag-selected" hidden>
      <div id="rag-source-badges" class="rag-source-badges" role="group" aria-label="Selected JSON RAG information"></div>
      <details class="reference rag-selected-json-preview" id="rag-selected-json-details" hidden>
        <summary id="rag-selected-json-summary">JSON preview</summary>
        <p id="rag-selected-json-note" class="storage-meta" hidden></p>
        <pre id="rag-selected-json"></pre>
      </details>
      <p id="rag-selected-note" class="storage-status alert" role="status" hidden>This is a refined file. Its chunks cannot be redefined. Select the original <span class="literal">json_rag.json</span> to create a new refinement.</p>
      <div id="rag-preflight-section">
      <h2 class="section"><span class="section-num">03</span><span class="section-label">Aggregation preflight</span></h2>
      <form id="rag-preflight-form">
        <div class="rag-preflight-row">
          <label class="rag-field">min token<input id="rag-preflight-min" type="number" min="1" step="1" value="512" required /></label>
          <label class="rag-field">max token<input id="rag-preflight-max" type="number" min="1" step="1" value="1024" required /></label>
          <div class="check-row rag-protection-options">
            <label class="check"><input id="rag-keep-tables" type="checkbox" checked /> keep tables</label>
            <label class="check"><input id="rag-keep-code" type="checkbox" checked /> keep code blocks</label>
            <label class="check"><input id="rag-page-boundary" type="checkbox" checked /> page boundary</label>
          </div>
          <button type="submit" id="rag-preflight-run" class="btn btn-primary">Run preflight</button>
        </div>
      </form>
      <p id="rag-preflight-error" class="alert alert-error" role="alert"></p>
      <p id="rag-preflight-status" class="storage-status" role="status"></p>
      <section id="rag-preflight-result" aria-label="Aggregation preflight result" hidden>
        <div id="rag-preflight-counts"></div>
        <div id="rag-preflight-distribution"></div>
        <div id="rag-preflight-groups"></div>
        <nav class="storage-pagination" aria-label="Preflight pagination">
          <label>Rows per page <select id="rag-preflight-page-size"><option selected>10</option><option>20</option><option>50</option></select></label>
          <span id="rag-preflight-page-info" role="status"></span>
          <div class="storage-page-buttons"><button type="button" class="btn" id="rag-preflight-prev">Previous</button><button type="button" class="btn" id="rag-preflight-next">Next</button></div>
        </nav>
        <section id="rag-preflight-detail" class="storage-preview" aria-label="Aggregated group preview" hidden></section>
      </section>
      </div>
    </div>`;
  const find = id => root.querySelector(`#rag-preflight-${id}`);
  const selected = root.querySelector("#rag-selected");
  const badges = root.querySelector("#rag-source-badges");
  let current = null, plan = null, planPage = 0;
  let execution = null, previewKind = null;
  // Gesture state: the row model is the plan's groups as bare entry lists.
  let rows = [], gestures = [], counts = new Map(), separators = new Map();
  let continuations = new Set(), tokenizerUnavailable = false;
  // FR-022: the cap is re-measured on the gesture-modified plan.
  let capTooLarge = false;
  const rowByEntries = new Map();
  const labels = { within: "Within range", below: "Below range", above: "Above range", unknown: "Unknown size" };
  const executeButton = executionRoot.querySelector("#rag-execute");
  const executionState = executionRoot.querySelector("#rag-execution-state");

  // Status convention: the console's bare .alert line — uppercase and muted,
  // red on failure. A file name keeps its real case inside a .literal span.
  function setExecutionState(content, error = false) {
    executionState.className = error
      ? "storage-status alert alert-error" : "storage-status alert";
    executionState.replaceChildren(...[].concat(content)
      .filter(part => part !== "" && part != null)
      .map(part => typeof part === "string" ? part : node("span", part.literal, "literal")));
  }

  function lazyPreview(details, pre, note) {
    const summary = details.querySelector("summary");
    let produce = null, extra = "";
    details.addEventListener("toggle", () => {
      if (!details.open || produce === null) return;
      const contents = produce();
      const truncated = contents.length > 100000;
      pre.textContent = truncated
        ? `${contents.slice(0, 100000)}\n… Preview truncated.` : contents;
      note.hidden = !(truncated || extra);
      note.textContent = truncated
        ? "Showing the first 100,000 characters. Download the file for the complete JSON." : extra;
    });
    return {
      // A file name keeps its real case inside the uppercased summary label.
      set(summaryText, producer, extraNote = "", literal = false) {
        summary.replaceChildren(literal ? node("span", summaryText, "literal") : summaryText);
        produce = producer; extra = extraNote;
        pre.replaceChildren();
        note.hidden = true; note.textContent = "";
        details.hidden = false; details.open = false;
      },
      reset() {
        produce = null; extra = "";
        details.hidden = true; details.open = false;
        pre.replaceChildren();
        note.hidden = true; note.textContent = "";
        summary.textContent = "JSON preview";
      },
    };
  }
  const selectedJson = lazyPreview(
    root.querySelector("#rag-selected-json-details"),
    root.querySelector("#rag-selected-json"),
    root.querySelector("#rag-selected-json-note"),
  );
  const executionJson = lazyPreview(
    executionRoot.querySelector("#rag-execution-json-details"),
    executionRoot.querySelector("#rag-execution-json"),
    executionRoot.querySelector("#rag-execution-json-note"),
  );

  function resetExecutionJson() {
    previewKind = null;
    executionJson.reset();
  }

  function showJson(summary, produce, note = "", literal = false) {
    executionJson.set(summary, produce, note, literal);
  }

  async function showSavedJson(file, selected, reviewed, savedRows) {
    const stillCurrent = () => execution === null && current === selected
      && plan === reviewed && rows === savedRows;
    const result = await api.storageFile(file.key);
    if (!result.ok) {
      if (stillCurrent()) {
        executionState.append(node("span",
          ` Preview unavailable: ${failure(result, "the saved file could not be read.")}`));
        executionState.className = "storage-status alert alert-error";
      }
      return;
    }
    const contents = await result.data.text();
    if (!stillCurrent()) return;
    let formatted = contents;
    try { formatted = JSON.stringify(JSON.parse(contents), null, 2); } catch { /* Raw text is shown when the file is not JSON. */ }
    previewKind = "saved";
    showJson(file.name, () => formatted, "", true);
  }

  function syncExecute() {
    executeButton.disabled = !current || !plan || !rows.length || Boolean(execution)
      || splitPendingRow !== null
      || capTooLarge || !current.refinable || !current.checksumOk
      || current.value.chunks.length === 0;
  }

  // FR-022: the 100,000-group / 2 MB cap is measured on the encoded plan and
  // re-measured after every gesture, not only at preflight.
  function recomputeCap() {
    capTooLarge = capState(rows.map(row => row.entries)).tooLarge;
    if (plan !== null) plan.too_large = capTooLarge;
  }

  const minInput = find("min"), maxInput = find("max"), runButton = find("run");

  // The same bounds planRagAggregation enforces, checked on every keystroke so
  // an invalid range is flagged before preflight can be launched.
  function syncTokenRange() {
    const minValue = minInput.valueAsNumber, maxValue = maxInput.valueAsNumber;
    const minInvalid = !Number.isSafeInteger(minValue) || minValue < 1;
    const maxInvalid = !Number.isSafeInteger(maxValue) || maxValue < 1
      || (!minInvalid && maxValue < minValue);
    const maxBound = !minInvalid && Number.isSafeInteger(maxValue) && maxValue >= 1
      && maxValue < minValue;
    minInput.setCustomValidity(minInvalid
      ? "Min token must be a whole number of at least 1." : "");
    maxInput.setCustomValidity(maxInvalid
      ? (maxBound ? "Max token must be greater than or equal to min token."
        : "Max token must be a whole number of at least 1.") : "");
    maxInput.min = minInvalid ? "1" : String(minValue);
    minInput.setAttribute("aria-invalid", String(minInvalid));
    maxInput.setAttribute("aria-invalid", String(maxInvalid));
    find("error").textContent = minInvalid ? minInput.validationMessage
      : maxInvalid ? maxInput.validationMessage : "";
    runButton.disabled = !current?.refinable || minInvalid || maxInvalid;
  }

  function resetPlan() {
    const discarded = gestures.length > 0 || splitPendingRow !== null;
    if (gestures.length) resetGestures({ groups: rows.map(row => row.entries), gestures, counts });
    plan = null; planPage = 0;
    rows = []; gestures = []; counts = new Map(); separators = new Map();
    continuations = new Set(); rowByEntries.clear();
    splitPendingRow = null; capTooLarge = false;
    find("result").hidden = true;
    find("detail").hidden = true; find("detail").replaceChildren();
    find("groups").replaceChildren();
    // R23: this line lives in the status region outside the result section,
    // which is hidden three lines above, so the user can actually see it.
    find("status").textContent = discarded
      ? "The plan changed: manual gestures were discarded. Run preflight again." : "";
    find("error").textContent = "";
    // A plan preview describes a plan that no longer exists; a saved file stays.
    if (previewKind === "plan") resetExecutionJson();
    syncTokenRange();
    syncExecute();
  }

  function previewGroup(entries, index) {
    const data = ragGroupPreview(current.value, { source_indices: entries, text_separators: separators.get(entries) });
    const target = find("detail");
    target.hidden = false;
    // Same collapsible JSON panel as the selected-file and aggregated JSON previews.
    const payloads = node("details", "", "reference rag-selected-json-preview");
    payloads.append(node("summary", "Source payloads"), node("pre", JSON.stringify(data.source_payloads, null, 2)));
    // Named like the table's "Chunk id" column; the range reason stays in the row tooltip.
    target.replaceChildren(node("h3", `Chunk id: ${index + 1}`), node("pre", data.text), payloads);
  }

  function pagesOfEntries(entries) {
    const pages = [];
    for (const entry of entries) {
      const chunk = current.value.chunks[Array.isArray(entry) ? entry[0] : entry];
      if (chunk && hasPage(chunk)) pages.push(pageOf(chunk));
    }
    return [...new Set(pages)];
  }

  function makeRow(entries, extra = {}) {
    const known = rowByEntries.get(entries);
    if (known) return known;
    const tokenCount = extra.token_count ?? groupTokenCount(current.value, entries, counts);
    const status = extra.status ?? tokenRangeStatus(tokenCount, plan.options);
    const row = { entries, status, reason: extra.reason ?? "", pages: pagesOfEntries(entries),
      missing_pages: extra.missing_pages ?? 0, token_count: tokenCount, known_tokens: tokenCount,
      notes: extra.notes ?? [], gestureId: extra.gestureId ?? null };
    rowByEntries.set(entries, row);
    // R17: a gesture group joins with the real structural continuation flag.
    if (!separators.has(entries)) separators.set(entries, separatorsFor(entries));
    return row;
  }

  function textOfEntry(entry) {
    const chunk = current.value.chunks[Array.isArray(entry) ? entry[0] : entry];
    if (!chunk) return "";
    return entryText(current.value, entry);
  }

  function separatorsFor(entries) {
    return entries.slice(1).map((entry, offset) => {
      const previous = entries[offset];
      const previousIndex = Array.isArray(previous) ? previous[0] : previous;
      const currentIndex = Array.isArray(entry) ? entry[0] : entry;
      return ragTextSeparator(textOfEntry(previous), textOfEntry(entry),
        previousIndex === currentIndex || continuations.has(previousIndex));
    });
  }

  function previewPlan() {
    return { ...plan, groups: rows.map(row => ({ source_indices: row.entries,
      text_separators: separators.get(row.entries) })) };
  }

  function renderGroups() {
    const { wrapper, body } = tableView("Planned RAG groups", ["Chunk id", "Aggregated chunks", "Page", "Tokens", "Token range", "Action"]);
    const size = Number(find("page-size").value);
    const start = planPage * size;
    // Undo controls come from the gesture list because later gestures may
    // consume the rows produced by an earlier gesture.
    const undosByRow = new Map();
    for (const [order, gesture] of gestures.entries()) {
      const later = gestures.slice(order + 1).find(item =>
        item.slice.some(group => gesture.produced.includes(group)));
      const own = rows.filter(row => gesture.produced.includes(row.entries));
      const consumed = later ? rows.filter(row => later.produced.includes(row.entries)) : [];
      for (const row of (consumed.length ? consumed : own)) {
        if (!undosByRow.has(row.entries)) undosByRow.set(row.entries, []);
        undosByRow.get(row.entries).push({ id: gesture.id, blockedBy: later ? String(later.id) : "" });
      }
    }
    for (const [offset, row] of rows.slice(start, start + size).entries()) {
      const index = start + offset, tr = node("tr"), actions = node("td");
      const controls = node("div", "", "storage-actions");
      const view = iconButton("preview", `Preview group ${index + 1}`, "", "Preview");
      view.addEventListener("click", () => previewGroup(row.entries, index));
      controls.append(view);
      const gesture = gestureFor(row.status, index, rows.length, { tokenizerUnavailable });
      // FR-020: only the row being cut is locked, while a save in flight locks
      // every scissors and glue; the other rows keep their controls untouched.
      const cutting = splitPendingRow !== null && row.entries === splitPendingRow;
      const locked = executing || cutting;
      const cut = iconButton("scissors", `Cut group ${index + 1}`, "", "Cut");
      cut.disabled = !gesture.scissors || locked;
      cut.addEventListener("click", () => runSplit(index));
      if (!cut.disabled) controls.append(cut);
      if (row.status === "below" || row.status === "unknown") {
        // R24b: fragments of the same chunk are recomposed with undo, not glue.
        const previousSameChunk = index > 0 && sameChunk(rows[index - 1].entries, row.entries);
        const previous = iconButton("gluePrevious", `Merge group ${index + 1} with previous`, "", "Merge with previous");
        previous.disabled = !gesture.gluePrevious || locked || previousSameChunk;
        previous.addEventListener("click", () => runGlue(index, "previous"));
        const nextSameChunk = index < rows.length - 1 && sameChunk(row.entries, rows[index + 1].entries);
        const next = iconButton("glueNext", `Merge group ${index + 1} with next`, "", "Merge with next");
        next.disabled = !gesture.glueNext || locked || nextSameChunk;
        next.addEventListener("click", () => runGlue(index, "next"));
        if (!previous.disabled) controls.append(previous);
        if (!next.disabled) controls.append(next);
      }
      for (const { id, blockedBy } of undosByRow.get(row.entries) ?? []) {
        const undo = iconButton("undo", `Undo gesture ${id}`, "", "Undo");
        undo.disabled = cutting || executing || Boolean(blockedBy);
        undo.addEventListener("click", () => runUndo(id));
        if (!undo.disabled) controls.append(undo);
      }
      actions.append(controls);
      const state = node("td", labels[row.status]);
      const belowReason = row.status === "below"
        ? `This fragment has ${row.token_count ?? "an unknown number of"} tokens; the target minimum is ${plan.options.min_tokens}. Splitting may leave smaller fragments to respect the maximum and preserve structure. You can keep it, undo the cut, or merge it with a different neighbouring chunk.` : "";
      state.title = [row.reason, belowReason].filter(Boolean).join(" ");
      state.className = `rag-preflight-${row.status}`;
      const pages = row.pages.join(", ") + (row.missing_pages ? " · unknown" : "");
      tr.append(node("td", String(index + 1), "storage-number"),
        node("td", String(row.entries.length), "storage-number"), node("td", pages || "Unknown"),
        node("td", row.token_count === null ? (row.known_tokens === null ? "Unknown" : `Unknown (${row.known_tokens} counted)`) : String(row.token_count), "storage-number"),
        state, actions);
      body.append(tr);
    }
    find("groups").replaceChildren(wrapper);
    find("prev").disabled = planPage === 0;
    find("next").disabled = start + size >= rows.length;
    find("page-info").textContent = `${rows.length ? start + 1 : 0}–${Math.min(start + size, rows.length)} of ${rows.length} groups`;
  }

  // FR-020: a split is in flight for at most 30 s (the api module timeout);
  // only its row shows a status line and locked controls, while the plan stays
  // untouched. `executing` is the section 04 save in flight, which locks the
  // scissors and glue of every row.
  let splitPendingRow = null, executing = false;

  async function runSplit(index) {
    if (splitPendingRow !== null || executing) return;
    const selected = current, reviewed = plan, entries = rows[index].entries;
    splitPendingRow = entries;
    find("status").textContent = "Cutting…";
    find("error").textContent = "";
    renderGroups();
    syncExecute();
    const result = await api.splitRag({ source_key: selected.file.key, source_sha256: selected.file.sha256,
      options: plan.options, group: entries });
    if (splitPendingRow === entries) splitPendingRow = null;
    if (selected !== current || reviewed !== plan) return;
    syncExecute();
    find("status").textContent = "";
    if (!result.ok) {
      if (result.status === 503) tokenizerUnavailable = true;
      find("error").textContent = result.status === 503
        ? `Splitting is unavailable: ${failure(result, "the tokenizer is unavailable.")}`
        : `Split failed: ${failure(result, "the server could not split the group.")}`;
      renderGroups();
      return;
    }
    const fragments = Array.isArray(result.data?.fragments) ? result.data.fragments : [];
    // Another gesture (an undo) may have moved the row while the split was in
    // flight; the cut follows its entries, and gives up if they are gone.
    const at = rows.findIndex(row => row.entries === entries);
    if (at < 0) return;
    const model = applySplit(selected.value, rows.map(row => row.entries), at, fragments, gestures);
    const id = model.gestures[0].id;
    counts = new Map([...counts, ...model.counts]);
    const produced = fragments.map(fragment => makeRow(fragment.entries,
      { token_count: fragment.token_count, status: fragment.status, notes: fragment.notes ?? [], gestureId: id }));
    gestures = [...gestures, { ...model.gestures[0], rowRefs: produced }];
    rows = [...rows.slice(0, at), ...produced, ...rows.slice(at + 1)];
    recomputeCap();
    renderGroups();
    syncPreview();
  }

  function runGlue(index, direction) {
    if (executing) return;
    // R10 choice: refuse the glue while a cut is in flight instead of letting it
    // consume the pending row and silently dropping the split response.
    if (splitPendingRow !== null) {
      find("error").textContent = "A cut is in progress; merge again once it finishes.";
      return;
    }
    const existing = gestures.length;
    const model = glueGroups(current.value, rows.map(row => row.entries), index, direction, plan.options, counts, gestures);
    if (model.gestures.length === existing) return;
    const record = model.gestures[model.gestures.length - 1];
    if (!record || record.kind !== "glue") return;
    counts = model.counts;
    const at = Math.min(index, direction === "previous" ? index - 1 : index + 1);
    const merged = makeRow(model.groups[at], { notes: model.notes, gestureId: record.id });
    gestures = model.gestures.map(item => item === record ? { ...item, rowRefs: [merged] } : item);
    rows = model.groups.map(entries => rowByEntries.get(entries) ?? merged);
    recomputeCap();
    renderGroups();
    syncPreview();
  }

  function runUndo(gestureId) {
    if (executing) return;
    const model = undoGesture({ groups: rows.map(row => row.entries), gestures, counts }, gestureId);
    if (model.blockedBy) {
      find("error").textContent = `Cannot undo gesture ${gestureId}: a later gesture (${model.blockedBy}) used the rows it produced.`;
      return;
    }
    gestures = model.gestures;
    counts = model.counts;
    rows = model.groups.map(entries => rowByEntries.get(entries) ?? makeRow(entries));
    recomputeCap();
    renderGroups();
    syncPreview();
  }

  function syncPreview() {
    updatePreflightMetrics();
    if (previewKind === "plan" && plan !== null) {
      showJson("aggregated JSON",
        () => JSON.stringify(ragAggregationPreview(current.value, previewPlan(), countsOf({ counts })), null, 2),
        "Preview of the aggregation plan. The saved file adds execution metadata.");
    }
    syncExecute();
  }

  function updatePreflightMetrics() {
    const totals = { within: 0, below: 0, above: 0, unknown: 0 };
    for (const row of rows) totals[row.status] += 1;
    find("counts").innerHTML = metricTiles(Object.entries(totals)
      .map(([status, count]) => [labels[status], count]));
    const tokenCounts = rows.map(row => row.token_count);
    const known = tokenCounts.filter(Number.isFinite);
    find("distribution").innerHTML = metricTiles([
      ["total chunks", rows.length],
      ...(known.length === rows.length && known.length
        ? [
          ["min chunk tokens", Math.min(...known)],
          ["avg chunk tokens", Math.round(known.reduce((sum, count) => sum + count, 0) / known.length)],
          ["max chunk tokens", Math.max(...known)],
        ] : []),
    ]);
  }

  find("form").addEventListener("submit", event => {
    event.preventDefault();
    if (!current?.refinable || !find("form").reportValidity()) return;
    resetPlan();
    if (!current.value.chunks.length) {
      find("error").textContent = "The selected file has no chunks to aggregate.";
      return;
    }
    try {
      plan = planRagAggregation(current.value, {
        min_tokens: Number(find("min").value), max_tokens: Number(find("max").value),
        keep_tables_together: root.querySelector("#rag-keep-tables").checked,
        keep_code_blocks_together: root.querySelector("#rag-keep-code").checked,
        page_boundary: root.querySelector("#rag-page-boundary").checked,
      });
      plan.execution_id = newExecutionId();
      plan.created_at = new Date().toISOString();
      // FR-021: the tokenizer flag is a page-session fact; a new preflight
      // does not clear it, only a page reload does.
      rows = []; gestures = []; counts = new Map();
      rowByEntries.clear();
      separators = new Map(plan.groups.map(group => [group.source_indices, group.text_separators]));
      continuations = ragStructureBoundaries(current.value.chunks, current.value.metadata, plan.options).continuations;
      for (const group of plan.groups) makeRow(group.source_indices, { status: group.status, reason: group.reason,
        pages: group.pages, missing_pages: group.missing_pages, token_count: group.token_count,
        known_tokens: group.known_tokens });
      rows = plan.groups.map(group => rowByEntries.get(group.source_indices));
      find("result").hidden = false;
      updatePreflightMetrics();
      recomputeCap();
      renderGroups();
      previewKind = "plan";
      // The chunk count is the "total chunks" tile in section 03.
      showJson("aggregated JSON",
        () => JSON.stringify(ragAggregationPreview(current.value, previewPlan(), countsOf({ counts })), null, 2),
        "Preview of the aggregation plan. The saved file adds execution metadata.");
      syncExecute();
    } catch (error) { find("error").textContent = error.message; }
  });
  for (const control of [find("min"), find("max"), root.querySelector("#rag-keep-tables"),
    root.querySelector("#rag-keep-code"), root.querySelector("#rag-page-boundary")]) {
    control.addEventListener(control.type === "checkbox" || control.tagName === "SELECT" ? "change" : "input",
      () => resetPlan());
  }
  find("prev").addEventListener("click", () => { planPage--; renderGroups(); if (find("prev").disabled) find("next").focus(); });
  find("next").addEventListener("click", () => { planPage++; renderGroups(); if (find("next").disabled) find("prev").focus(); });
  find("page-size").addEventListener("change", () => { planPage = 0; renderGroups(); });

  function newExecutionId() {
    return crypto.randomUUID ? crypto.randomUUID()
      : [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, "0")).join("")
        .replace(/^(........)(....)(....)(....)(............)$/, "$1-$2-4$3-a$4-$5");
  }

  executeButton.addEventListener("click", async () => {
    if (!current || !plan || executeButton.disabled) return;
    const selected = current, reviewed = plan, savedRows = rows;
    const run = execution = Symbol("rag-refinement");
    executeButton.disabled = true;
    // FR-020: a save in flight disables the scissors and glue of every row.
    executing = true;
    renderGroups();
    resetExecutionJson();
    setExecutionState(["Saving ", { literal: selected.file.name }, "…"]);
    // O1-1: declare the two size notes the rows display. A whole-chunk glue
    // above the maximum has no partial entry, so this declaration is the only
    // signal that reaches the saved file; the server re-measures the group and
    // refuses a mismatch. Fact codes stay server-derived (R21).
    const declared = rows.map(row => row.notes.map(noteCode).filter(code =>
      code === "above_maximum_fragment" || code === "below_minimum_fragment"));
    const payload = {
      source_key: selected.file.key,
      source_sha256: selected.file.sha256,
      options: reviewed.options,
      groups: rows.map(row => row.entries),
      overrides: declared.some(codes => codes.length) ? declared : undefined,
      // R24: the per-chunk member counts the console displays, so a real
      // discordance with the server's own measurement is a 422, never a silent
      // difference. A gesture-free whole-chunk plan stays free of the field.
      counts: rows.some(row => row.entries.some(Array.isArray))
        ? rows.map(row => memberCounts(current.value, row.entries, counts)) : undefined,
    };
    const signature = JSON.stringify(payload);
    // Retry the same bytes with the same identity, even after an uncertain save.
    if (reviewed.requestSignature !== undefined && reviewed.requestSignature !== signature) {
      reviewed.execution_id = newExecutionId();
      reviewed.created_at = new Date().toISOString();
    }
    reviewed.requestSignature = signature;
    const result = await api.refineRag({ ...payload,
      execution_id: reviewed.execution_id, created_at: reviewed.created_at });
    if (execution !== run) return;
    execution = null;
    executing = false;
    if (result.ok) {
      const file = result.data.file;
      // FR-014: a successful save invalidates every gesture, so the undo
      // controls disappear with the list they are rendered from.
      const changed = selected !== current || reviewed !== plan || savedRows !== rows;
      if (!changed) gestures = [];
      setExecutionState(
        [changed ? "Previous selection saved: " : "Saved ", { literal: file.name },
          ` (${result.data.chunk_count} chunks).`],
      );
      showSavedJson(file, selected, reviewed, savedRows);
      onSaved?.({ prefix: result.data.prefix, file });
    } else {
      // FR-021: a tokenizer 503 from the save path also disables the scissors
      // for the rest of the page session, with the reason in its tooltip.
      if (result.status === 503) tokenizerUnavailable = true;
      const changed = selected !== current || reviewed !== plan;
      setExecutionState(
        `${changed ? "Previous selection: " : ""}Save failed: ${failure(result, "Storage unavailable.")}`
        + (result.status === 0 ? " The server may still have saved the file; Execute retries the same plan." : ""),
        true,
      );
    }
    if (plan !== null && rows.length) renderGroups();
    syncExecute();
  });

  const panel = {
    select(value, file, parent) {
      if (!value || !Array.isArray(value.chunks)
        // Same id rule as materialize(): a safe non-negative integer (1.0 counts).
        || !value.chunks.every(chunk => chunk && Number.isSafeInteger(chunk.id) && chunk.id >= 0
          && typeof chunk.text === "string")) return false;
      const refinable = isRagSource(file.name);
      const checksumOk = /^[a-f0-9]{64}$/.test(file.sha256 || "");
      current = { value, file, parent, refinable, checksumOk };
      resetPlan();
      resetExecutionJson();
      const metadata = value.metadata ?? {};
      const average = typeof metadata.avg_tokens === "number" && Number.isFinite(metadata.avg_tokens)
        ? Number(metadata.avg_tokens.toFixed(2)) : null;
      const presence = ragStructurePresence(value.chunks, metadata);
      badges.innerHTML = metricTiles([
        ["parent file", parent],
        ["raw chunks", value.chunks.length],
        ["avg tokens", average],
        ["pages", metadata.pages],
        ["tables", presence.tables ? "yes" : "no"],
        ["code blocks", presence.code ? "yes" : "no"],
        ...(Array.isArray(metadata.unprocessed_pages) && metadata.unprocessed_pages.length
          ? [["partial", `${metadata.unprocessed_pages.length} unprocessed pages`]] : []),
      ], { fit: true });
      selectedJson.set(file.name, () => JSON.stringify(value, null, 2), "", true);
      selected.hidden = false;
      root.querySelector("#rag-selected-note").hidden = refinable;
      root.querySelector("#rag-preflight-section").hidden = !refinable;
      executionRoot.hidden = !refinable;
      const hint = !value.chunks.length ? "This file contains no chunks; there is nothing to refine."
          : !checksumOk ? "The server did not return a valid source checksum, so saving is disabled. Reload the console or update the OCR server."
            : "";
      if (execution === null) setExecutionState(hint);
      syncExecute();
      return true;
    },
    clear() {
      current = null;
      resetPlan();
      resetExecutionJson();
      selected.hidden = true;
      executionRoot.hidden = false;
      badges.replaceChildren();
      selectedJson.reset();
      setExecutionState("");
      syncExecute();
    },
    selected() { return current; },
  };
  panel.clear();
  return panel;
}
