/* SmartModels "Query Lab" UI behaviors — theme-level patterns only.
   Page logic (fetching, rendering results) belongs in the page's own
   app.js, not here. All functions are idempotent wiring helpers. */

/** ARIA tab switching for a .tabs or .subtabs tablist.
    Each button needs role="tab" and aria-controls="<panel-id>";
    panels are shown/hidden via the `hidden` attribute.
    Implements the full APG tabs pattern: click activation, roving
    tabindex, and Arrow/Home/End keyboard navigation (automatic
    activation — moving focus selects the tab). */
export function initTabs(tablist) {
  const tabs = [...tablist.querySelectorAll('[role="tab"]')];
  const panelOf = (tab) => document.getElementById(tab.getAttribute('aria-controls'));
  const visibleTabs = () => tabs.filter((t) => !t.hidden);
  const syncTabindex = () => {
    for (const t of tabs) {
      t.tabIndex = t.getAttribute('aria-selected') === 'true' ? 0 : -1;
    }
  };
  tablist.addEventListener('click', (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (!tab || !tablist.contains(tab)) return;
    for (const t of tabs) {
      const selected = t === tab;
      t.setAttribute('aria-selected', String(selected));
      const panel = panelOf(t);
      if (panel) panel.hidden = !selected;
    }
    syncTabindex();
  });
  tablist.addEventListener('keydown', (event) => {
    // A page may hide tabs at runtime (the console hides Parse and OCR in
    // archive-only mode): arrows and Home/End move among the visible ones.
    const tabs = visibleTabs();
    const index = tabs.indexOf(document.activeElement);
    if (index === -1) return;
    let target;
    if (event.key === 'ArrowRight') target = tabs[(index + 1) % tabs.length];
    else if (event.key === 'ArrowLeft') target = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (event.key === 'Home') target = tabs[0];
    else if (event.key === 'End') target = tabs[tabs.length - 1];
    else return;
    event.preventDefault();
    target.click();
    target.focus();
  });
  syncTabindex();
}

/** Checkbox that enables/disables dependent controls (theme pattern:
    "Set rrf_k" checkbox gating its number input). */
export function initEnableToggle(checkbox, ...controls) {
  const update = () => {
    for (const control of controls) control.disabled = !checkbox.checked;
  };
  checkbox.addEventListener('change', update);
  update();
}

/** Type-to-confirm gate for destructive actions. `expected` is the string
    the user must type (or a function returning it, for dynamic targets
    like the active collection). Shows `hint` only on a non-empty
    mismatch. Returns the update function so callers can re-run it when
    `expected` changes. */
export function initTypeToConfirm({ input, button, hint, expected }) {
  const update = () => {
    const value = input.value.trim();
    const target = typeof expected === 'function' ? expected() : expected;
    const match = value !== '' && value === target;
    button.disabled = !match;
    if (hint) hint.hidden = match || value === '';
  };
  input.addEventListener('input', update);
  update();
  return update;
}

/** Live caption under a range input (theme pattern:
    "dense 0.50 · sparse 0.50"). `format(value)` returns the caption. */
export function initRangeCaption(range, caption, format) {
  const update = () => {
    caption.textContent = format(parseFloat(range.value));
  };
  range.addEventListener('input', update);
  update();
}

/** Popover anchored to a trigger button (theme pattern: the topbar API key
    dialog). Toggles `hidden` + aria-expanded, closes on Esc and on an
    outside pointerdown, and returns focus to the trigger on scripted
    closes only — an outside click keeps focus where the user clicked.
    `onOpen` runs after the popover is shown (focus a field, reset state).
    Returns `{ open, close }`. */
export function initPopover({ button, popover, onOpen }) {
  const open = () => {
    popover.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    if (onOpen) onOpen();
  };
  const close = ({ refocus = true } = {}) => {
    if (popover.hidden) return;
    popover.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (refocus) button.focus();
  };
  button.addEventListener('click', () => (popover.hidden ? open() : close()));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !popover.hidden) close();
  });
  document.addEventListener('pointerdown', (event) => {
    if (!popover.hidden
        && !popover.contains(event.target)
        && !button.contains(event.target)) {
      close({ refocus: false });
    }
  });
  return { open, close };
}

/** Lock a whole region when the backend cannot serve it. Dimming alone is
    not a lockdown: `inert` removes the subtree from focus and pointer
    events, aria-disabled announces it. Pair with a single .service-gate
    alert that states WHY — never leave a locked region unexplained. */
export function setRegionLocked(element, locked) {
  element.classList.toggle('operations-disabled', locked);
  element.toggleAttribute('inert', locked);
  element.setAttribute('aria-disabled', String(locked));
}

/** Replace a region's markup only when it changed. Polled panels rewrite the
    same alerts every tick; a rewrite re-announces role="alert" and live
    regions to screen readers even when nothing happened. */
const renderedHtml = new WeakMap();
export function setHtml(element, html) {
  if (renderedHtml.get(element) === html && element.innerHTML !== "") return;
  renderedHtml.set(element, html);
  element.innerHTML = html;
}

/** Keep keyboard focus when a control that holds it becomes unusable
    (disabled or hidden): focus moves to `fallback` instead of <body>. */
export function keepFocus(control, fallback) {
  if (document.activeElement === control && fallback) fallback.focus({ preventScroll: true });
}

/** HTML-escape a value for interpolation into an innerHTML template.
    Every helper below goes through it; so must your app.js templates.
    Both quote characters are encoded, so the result is safe in element text
    and in double- or single-quoted attributes. Global regex replaces are
    used on purpose: they behave like replaceAll here, and they are the form
    static analysers (CodeQL) recognise as an HTML escaper. */
export function esc(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Alert markup. `type` is "" or "error". Theme convention: messages
    render uppercase (from CSS) and drop any trailing period. */
export function alertHtml(type, message) {
  const cls = type ? `alert alert-${type}` : 'alert';
  const role = type === 'error' ? ' role="alert"' : '';
  return `<p class="${cls}"${role}>${esc(String(message).replace(/\.+\s*$/, ''))}</p>`;
}

/** Pretty-printed JSON payload block. */
export function jsonBlock(data) {
  return `<pre class="json-block"><code>${esc(JSON.stringify(data, null, 2))}</code></pre>`;
}

/** Metric tile row from `[label, value]` pairs; null/undefined renders "—".
    An optional third element `{ wide, text }` spans the tile across the row
    (wide) and renders the value as wrapped mono prose instead of a display
    numeral (text). `fit` sizes every tile to its content on one line
    (ellipsized when the row cannot hold it) instead of the equal-width grid,
    for badge rows carrying long names. */
export function metricTiles(entries, { fit = false } = {}) {
  const tiles = entries.map(([label, value, opts]) =>
    `<div class="metric${opts?.wide ? ' metric-wide' : ''}"><div class="metric-label">${esc(label)}</div>` +
    `<div class="metric-value${opts?.text ? ' metric-text' : ''}">${esc(value === null || value === undefined ? '—' : value)}</div></div>`
  ).join('');
  return `<div class="metric-row${fit ? ' metric-row-fit' : ''}">${tiles}</div>`;
}
