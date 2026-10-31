/* Optional RAG workspace. Kept identical in the standalone Studio distribution. */
(function () {
  "use strict";

  const diagnosticLabels = {
    reading_order_conflict: "Layout ranks conflict with source order; source order was retained.",
    empty_section_title: "An empty section title was retained in provenance.",
    empty_content: "An OCR block contains no text.",
    unknown_tag: "An unrecognized layout tag was handled conservatively.",
    content_tokens_mismatch: "A saved token count differs from the exact current count.",
    relation_unresolved: "A caption or note could not be associated unambiguously.",
    geometry_unavailable: "Page dimensions are missing; geometry cannot establish this relation.",
    asset_reference_unassigned: "An image reference could not be assigned to a layout block.",
    table_parse_fallback: "A protected table retained its original markup.",
    table_header_missing: "No declared table headers are available for repetition.",
    table_header_first_row: "The first row is repeated as a header by your explicit policy.",
    table_representation: "A table was split into cells with row and column coordinates.",
    split_inside_code_line: "A code line had to be split to respect the token budget.",
    protected_block_overflow: "A protected block exceeds the budget under the selected non-strict policy.",
  };

  const metadataFields = [
    ["page", "Primary page"], ["pages", "All pages"], ["section_id", "Section ID"],
    ["section", "Section"], ["section_path", "Section path"], ["block_types", "Block types"],
    ["source_spans", "Source spans"], ["context_spans", "Context spans"],
    ["page_context", "Page context"], ["assets", "Assets"], ["relations", "Relations"],
    ["overflow", "Overflow"], ["overflow_tokens", "Overflow tokens"],
    ["overflow_reason", "Overflow reason"], ["overlap_token_count", "Overlap count"],
    ["source_fingerprint", "Source fingerprint"], ["tokenizer", "Tokenizer"],
    ["chunker_version", "Chunker version"], ["policy_hash", "Policy hash"],
    ["source_id", "Source ID"], ["lang", "Language"], ["diagnostics", "Diagnostics"],
    ["document_context", "Document context"],
  ];

  function download(name, text) {
    const url = URL.createObjectURL(new Blob([text], { type: "application/json;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function validSource(value) {
    return value && Array.isArray(value.pages) && value.pages.length > 0
      && value.pages.every(page => page && Number.isInteger(page.page) && page.page > 0
        && Array.isArray(page.blocks));
  }

  function validResult(value) {
    const diagnostics = items => items == null || (Array.isArray(items)
      && items.every(item => item && typeof item.code === "string"));
    const optionalArray = (items, check = () => true) => items == null
      || (Array.isArray(items) && items.every(check));
    return value && Array.isArray(value.chunks) && value.chunks.length > 0
      && value.chunks.every((chunk, index) => chunk && chunk.id === index && typeof chunk.text === "string"
        && Number.isInteger(chunk.token_count) && chunk.token_count >= 0
        && (chunk.section == null || typeof chunk.section === "string")
        && (chunk.overflow == null || typeof chunk.overflow === "boolean")
        && (chunk.overflow_tokens == null || Number.isInteger(chunk.overflow_tokens))
        && optionalArray(chunk.pages, page => Number.isInteger(page) && page > 0)
        && optionalArray(chunk.block_types, tag => typeof tag === "string")
        && diagnostics(chunk.diagnostics) && diagnostics(chunk.document_context?.diagnostics)
        && (chunk.page_context == null || (Array.isArray(chunk.page_context)
          && chunk.page_context.every(page => page && Number.isInteger(page.page)
            && Array.isArray(page.page_labels) && page.page_labels.every(label => label && typeof label.text === "string")))));
  }

  function mount(root, { request, renderText }) {
    root.classList.add("rag-workspace");
    root.innerHTML = `
      <p>Build chunks from structured OCR JSON. Section boundaries and the PaddleOCR tokenizer are always used.</p>
      <p>Strict mode with protected tables or code may refuse a small budget. Options are changed only by you.</p>
      <label class="rag-field">Import OCR JSON <input id="rag-import" type="file" accept=".json,application/json"></label>
      <p id="rag-source" role="status">Parse a document or import its structured JSON.</p>
      <form id="rag-form">
        <fieldset id="rag-options"><legend>Chunk options</legend><div class="rag-options-grid">
          <label class="rag-field">Maximum tokens <input id="rag-max" type="number" min="1" max="32768" step="1" value="512" required></label>
          <label class="rag-field">Overlap tokens <input id="rag-overlap" type="number" min="0" max="32767" step="1" value="0" required></label>
          <label class="rag-field">Page margins <select id="rag-margins"><option value="metadata">Metadata</option><option value="text">Include in text</option></select></label>
          <label class="rag-field">Table headers <select id="rag-headers"><option value="declared">Declared headers only</option><option value="first_row">First row (your assumption)</option><option value="none">Do not repeat headers</option></select></label>
          <label class="rag-check"><input id="rag-strict" type="checkbox" checked> Strict token limit</label>
          <label class="rag-check"><input id="rag-tables" type="checkbox" checked> Keep tables whole</label>
          <label class="rag-check"><input id="rag-code" type="checkbox" checked> Keep code blocks whole</label>
          <label class="rag-field">Source ID (optional) <input id="rag-source-id" type="text"></label>
          <label class="rag-field">Language (optional, e.g. it) <input id="rag-lang" type="text" placeholder="Not inferred"></label>
        </div>
        <fieldset class="rag-metadata"><legend>Output metadata</legend>
          <p class="rag-help">id, text and token_count are always included. Select the optional fields to export.</p>
          <div class="rag-metadata-grid">${metadataFields.map(([name, label]) => `<label class="rag-check"><input type="checkbox" data-rag-metadata="${name}" checked> ${label}</label>`).join("")}</div>
        </fieldset>
        <label class="rag-check"><input id="rag-truncated" type="checkbox"> Accept an explicitly truncated OCR page prefix</label>
        </fieldset>
        <button id="rag-run" class="btn btn-primary primary" type="submit" disabled>Crea chunk RAG</button>
      </form>
      <div id="rag-error" role="alert"></div>
      <section id="rag-result" hidden aria-label="RAG result">
        <p id="rag-result-state" role="status"></p>
        <p id="rag-summary"></p><p id="rag-distribution"></p>
        <div id="rag-histogram" class="rag-histogram" aria-label="Chunk token distribution"></div>
        <button id="rag-download" class="btn" type="button">Download RAG JSON</button>
        <div class="rag-pagination"><button id="rag-prev" class="btn" type="button">Previous chunks</button><span id="rag-pagination-label"></span><button id="rag-next" class="btn" type="button">Next chunks</button></div>
        <div id="rag-chunks"></div>
      </section>`;
    const get = id => root.querySelector("#" + id);
    let source = null;
    let sourceName = "document";
    let sourceVersion = 0;
    let sourceOrigin = null;
    let busy = false;
    let successful = null;
    let failed = false;
    let previewPage = 0;

    function options() {
      return {
        max_tokens: Number(get("rag-max").value),
        strictly: get("rag-strict").checked,
        preserve_tables: get("rag-tables").checked,
        preserve_code_blocks: get("rag-code").checked,
        overlap_tokens: Number(get("rag-overlap").value),
        marginal_policy: get("rag-margins").value,
        table_header_policy: get("rag-headers").value,
        metadata_fields: Array.from(root.querySelectorAll("[data-rag-metadata]:checked"), input => input.dataset.ragMetadata),
      };
    }

    function requestBody() {
      const body = { parsed_document: source, options: options(), accept_truncated: get("rag-truncated").checked };
      if (get("rag-source-id").value) body.source_id = get("rag-source-id").value;
      if (get("rag-lang").value) body.lang = get("rag-lang").value;
      return body;
    }

    function signature() {
      const body = requestBody();
      delete body.parsed_document;
      return sourceVersion + ":" + JSON.stringify(body);
    }

    function refresh() {
      get("rag-run").disabled = busy || !source;
      get("rag-options").disabled = busy;
      get("rag-import").disabled = busy;
      get("rag-run").textContent = busy ? "Creating chunks…" : "Crea chunk RAG";
      if (successful) {
        const previous = failed || successful.signature !== signature();
        get("rag-result-state").textContent = previous
          ? "Previous successful result — " + successful.name + ". Current source/options or latest request differ."
          : "Successful result — " + successful.name;
        get("rag-result-state").classList.toggle("rag-previous", previous);
      }
    }

    function setSource(value, name, origin = "parse") {
      const envelope = value && value.response ? value : null;
      const document = envelope ? envelope.response : value;
      if (!validSource(document)) return false;
      if (source === document && sourceName === name && sourceOrigin === origin) return true;
      source = document;
      sourceName = typeof envelope?.file === "string" ? envelope.file : name || "document";
      sourceVersion += 1;
      sourceOrigin = origin;
      get("rag-truncated").checked = false;
      const truncated = document.warnings?.some(warning => warning.code === "pages_truncated");
      get("rag-source").textContent = sourceName + " · " + document.pages.length + " input pages"
        + (truncated ? " · OCR reports a truncated prefix: acceptance requires your explicit choice." : "");
      get("rag-error").replaceChildren();
      refresh();
      return true;
    }

    function clearSource() {
      if (sourceOrigin === "import") return;
      source = null;
      sourceVersion += 1;
      get("rag-source").textContent = "Parse a document or import its structured JSON.";
      refresh();
    }

    function paragraph(parent, text, className) {
      const node = document.createElement("p");
      node.textContent = text;
      if (className) node.className = className;
      parent.append(node);
      return node;
    }

    function details(parent, label, value) {
      const node = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = label;
      const pre = document.createElement("pre");
      pre.textContent = JSON.stringify(value, null, 2);
      node.append(summary, pre);
      parent.append(node);
    }

    function renderChunks() {
      const chunks = successful.data.chunks;
      const start = previewPage * 25;
      const list = get("rag-chunks");
      list.replaceChildren();
      for (const chunk of chunks.slice(start, start + 25)) {
        const card = document.createElement("article");
        card.className = "rag-chunk";
        const heading = document.createElement("h3");
        heading.textContent = "Chunk " + chunk.id + " · " + chunk.token_count + " tokens";
        card.append(heading);
        paragraph(card, "Section: " + (chunk.section || "Untitled") + " · Input pages: " + chunk.pages.join(", "));
        paragraph(card, "Tags: " + chunk.block_types.join(", "));
        if (chunk.overflow) paragraph(card, "Authorized overflow: +" + chunk.overflow_tokens + " tokens", "rag-overflow");
        const labels = (chunk.page_context || []).flatMap(page => (page.page_labels || [])
          .map(label => "input page " + page.page + ": " + label.text));
        if (labels.length) paragraph(card, "Printed page labels — " + labels.join("; "));
        const text = document.createElement("div");
        text.className = "rag-text";
        if (renderText) text.innerHTML = renderText(chunk.text);
        else text.textContent = chunk.text;
        card.append(text);
        const diagnostics = [...(chunk.diagnostics || []), ...(chunk.document_context?.diagnostics || [])];
        if (diagnostics.length) {
          for (const item of diagnostics) paragraph(card,
            (diagnosticLabels[item.code] || "Diagnostic") + " (" + item.code + ")");
          details(card, "Diagnostic details (" + diagnostics.length + ")", diagnostics);
        }
        details(card, "Chunk metadata", Object.fromEntries(Object.entries(chunk).filter(([key]) => key !== "text")));
        list.append(card);
      }
      get("rag-prev").disabled = previewPage === 0;
      get("rag-next").disabled = start + 25 >= chunks.length;
      get("rag-pagination-label").textContent = (start + 1) + "–" + Math.min(start + 25, chunks.length) + " / " + chunks.length;
    }

    function renderResult() {
      const chunks = successful.data.chunks;
      const sizes = chunks.map(chunk => chunk.token_count).sort((a, b) => a - b);
      const total = sizes.reduce((sum, size) => sum + size, 0);
      const diagnostics = chunks.reduce((sum, chunk) => sum + (chunk.diagnostics?.length || 0)
        + (chunk.document_context?.diagnostics?.length || 0), 0);
      get("rag-result").hidden = false;
      get("rag-summary").textContent = chunks.length + " chunks · " + total + " tokens · "
        + chunks.filter(chunk => chunk.overflow).length + " overflows · " + diagnostics + " diagnostics";
      get("rag-distribution").textContent = "Tokens: min " + sizes[0] + " · median "
        + sizes[Math.floor(sizes.length / 2)] + " · p95 " + sizes[Math.ceil(sizes.length * 0.95) - 1]
        + " · max " + sizes[sizes.length - 1];
      const histogram = get("rag-histogram");
      histogram.replaceChildren();
      const width = Math.max(1, Math.ceil((sizes[sizes.length - 1] + 1) / 10));
      const bins = Array(10).fill(0);
      for (const size of sizes) bins[Math.min(9, Math.floor(size / width))] += 1;
      bins.forEach((count, index) => {
        const label = document.createElement("label");
        label.textContent = index * width + "–" + ((index + 1) * width - 1) + ": " + count;
        const meter = document.createElement("meter");
        meter.min = 0;
        meter.max = chunks.length;
        meter.value = count;
        label.append(meter);
        histogram.append(label);
      });
      previewPage = 0;
      renderChunks();
      refresh();
    }

    function showError(result) {
      const region = get("rag-error");
      region.replaceChildren();
      const detail = result.data?.detail || result.error?.detail || {};
      const code = detail.code || result.error?.code || "request_failed";
      const message = typeof result.error === "string" ? result.error
        : result.error?.message || detail.message || "Cannot create RAG chunks.";
      paragraph(region, code + ": " + message);
      const values = detail.details || {};
      if (code === "protected_block_exceeds_budget" || code === "context_exceeds_budget") {
        paragraph(region, "Input page " + values.page + " · " + values.source_key + " · " + values.type
          + " · budget " + values.max_tokens + " · required with context " + values.required_tokens + " tokens.");
        if (Number.isInteger(values.minimum_feasible_max_tokens)) {
          paragraph(region, "Verified minimum for this document: " + values.minimum_feasible_max_tokens + " tokens.");
        }
        function action(label, change, enabled = true) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "btn";
          button.textContent = label;
          button.disabled = !enabled;
          button.addEventListener("click", () => {
            change();
            paragraph(region, "Option updated. Press Crea chunk RAG to submit the new request.");
            refresh();
          });
          region.append(button);
        }
        const suggestion = values.suggested_max_tokens || values.required_tokens;
        action("Increase token budget", () => { get("rag-max").value = suggestion; },
          Number.isInteger(suggestion) && suggestion > 0 && suggestion <= 32768);
        const protection = values.conflicting_option === "preserve_tables" ? "rag-tables"
          : values.conflicting_option === "preserve_code_blocks" ? "rag-code" : null;
        action("Allow splitting this type", () => { get(protection).checked = false; }, Boolean(protection));
        action("Disable strict limit, keep protection", () => {
          get("rag-strict").checked = false;
          if (protection) get(protection).checked = true;
        }, Boolean(protection));
      }
      if (code === "chunk_capacity_exceeded") {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn";
        retry.textContent = "Retry";
        retry.addEventListener("click", run);
        region.append(retry);
      }
    }

    async function run(event) {
      event?.preventDefault();
      if (busy || !source || !get("rag-form").reportValidity()) return;
      const body = requestBody();
      if (body.options.overlap_tokens >= body.options.max_tokens) {
        showError({ error: "Overlap tokens must be less than maximum tokens." });
        return;
      }
      const submittedSignature = signature();
      const submittedName = sourceName;
      busy = true;
      failed = false;
      get("rag-error").replaceChildren();
      refresh();
      try {
        const result = await request(body);
        if (!result.ok) { failed = true; showError(result); }
        else if (!validResult(result.data) || typeof result.text !== "string") {
          failed = true;
          showError({ error: "Invalid RAG response; no new download was created." });
        } else {
          successful = { data: result.data, text: result.text, name: submittedName, signature: submittedSignature };
          renderResult();
        }
      } catch {
        failed = true;
        showError({ error: "Cannot reach the RAG service. Retry deliberately; OCR JSON is retained." });
      } finally { busy = false; refresh(); }
    }

    get("rag-form").addEventListener("submit", run);
    get("rag-options").addEventListener("input", refresh);
    get("rag-options").addEventListener("change", refresh);
    get("rag-import").addEventListener("change", async event => {
      const file = event.target.files[0];
      if (!file) return;
      try {
        const value = JSON.parse(await file.text());
        if (!setSource(value, file.name, "import")) throw new Error("missing blocks");
      } catch {
        showError({ error: "Import a structured /parse JSON, or an export containing response.pages[].blocks[]." });
      }
    });
    get("rag-download").addEventListener("click", () => {
      if (!successful) return;
      const name = successful.name.replace(/\.[^.]+$/, "").replace(/[^a-zA-Z0-9._-]/g, "_") || "document";
      download(name + ".rag.json", successful.text);
    });
    get("rag-prev").addEventListener("click", () => { previewPage -= 1; renderChunks(); });
    get("rag-next").addEventListener("click", () => { previewPage += 1; renderChunks(); });
    return { setSource, clearSource };
  }

  window.OcrRag = { mount, validResult };
})();
