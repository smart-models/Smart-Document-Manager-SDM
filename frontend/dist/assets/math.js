import { Marked } from "/assets/vendor/marked.esm.js?v=1";
import DOMPurify from "/assets/vendor/purify.es.mjs?v=1";
import katex from "/assets/vendor/katex/katex.mjs";

// Sanitize document HTML before inserting trusted renderer output. OCR input
// never gets KaTeX's broader HTML/SVG/MathML privileges.
const SANITIZE_PROFILE = {
  ALLOWED_TAGS: [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'ul', 'ol', 'li', 'blockquote',
    'pre', 'code', 'strong', 'em', 'b', 'i', 'u', 's', 'br', 'hr', 'a',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption',
    'div', 'span', 'img', 'sup', 'sub',
  ],
  ALLOWED_ATTR: ['colspan', 'rowspan', 'href', 'alt', 'src'],
  ALLOW_DATA_ATTR: false,
};

const DELIMITERS = [
  { left: "$$", right: "$$", display: true },
  { left: "\\[", right: "\\]", display: true },
  { left: "\\(", right: "\\)", display: false },
  { left: "$", right: "$", display: false },
];

// Consume TeX before Markdown can interpret underscores, backslashes or HTML.
// Marked still owns code blocks/spans and escaped dollar signs.
function mathToken(source) {
  const delimiter = DELIMITERS.find(item => source.startsWith(item.left));
  if (!delimiter) return;
  const { left, right, display } = delimiter;
  let braces = 0;
  for (let index = left.length; index < source.length; index++) {
    if (!display && source[index] === "\n") return;
    if (source.startsWith(right, index) && braces <= 0) {
      const latex = source.slice(left.length, index);
      if (!latex.trim()) return;
      // A closing dollar followed by a digit is usually another price.
      if (right === "$" && (/\d/.test(source[index + 1] || "")
        || (!/\s/.test(source[left.length]) && /\s/.test(source[index - 1])))) return;
      return { raw: source.slice(0, index + right.length), latex, display };
    }
    if (source[index] === "\\") index++;
    else if (source[index] === "{") braces++;
    else if (source[index] === "}") braces--;
    else if (source[index] === "%") {
      const newline = source.indexOf("\n", index);
      if (newline === -1 || !display) return;
      index = newline;
    }
  }
}

function formulaNode(latex, original, displayMode) {
  const node = document.createElement("span");
  node.className = displayMode ? "math-expression math-display" : "math-expression";
  try {
    katex.render(latex, node, {
      displayMode,
      output: "htmlAndMathml",
      trust: false,
      throwOnError: true,
      strict: "ignore",
      maxExpand: 1000,
      maxSize: 20,
    });
  } catch {
    node.classList.add("math-fallback");
    node.title = "Preview unavailable; original LaTeX shown.";
    node.textContent = original;
  }
  return node;
}

// Table markup a recognizer may legitimately emit; anything else that looks
// like HTML in recognized text is content (a screenshot of code, say).
const TABLE_HTML = /^\s*<\/?(?:table|thead|tbody|tfoot|tr|th|td|caption|colgroup|col)\b[^>]*>/i;

function escapeHtml(text) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

/**
 * Markdown with math to sanitized HTML. `recognized: true` is for model output
 * of a single image: raw HTML other than tables shows as literal text instead
 * of vanishing (e.g. a recognized "<script>" line), and images only render
 * from inline data, so no remote URL is ever fetched.
 */
export function renderMarkdown(markdown, { recognized = false } = {}) {
  if (!markdown) return "";
  const formulas = new Map();
  const nonce = Array.from(crypto.getRandomValues(new Uint32Array(4)), value => value.toString(16)).join("_");
  const prefix = `OCR_MATH_${nonce}_`;
  const renderer = token => {
    const marker = `${prefix}${formulas.size}`;
    formulas.set(marker, token);
    return `<span>${marker}</span>`;
  };
  const parser = new Marked({ extensions: [{
    name: "displayMath",
    level: "block",
    start: source => source.search(/(?:^|\n) {0,3}(?:\$\$|\\\[)/),
    tokenizer(source) {
      const indent = source.match(/^ {0,3}/)[0];
      const token = mathToken(source.slice(indent.length));
      if (token?.display) return { ...token, type: "displayMath", raw: indent + token.raw };
    },
    renderer,
  }, {
    name: "inlineMath",
    level: "inline",
    start: source => source.search(/\$|\\[([]/),
    tokenizer(source) {
      const token = mathToken(source);
      if (token) return { ...token, type: "inlineMath" };
    },
    renderer,
  }], ...(recognized ? { renderer: {
    html: ({ text }) => (TABLE_HTML.test(text) ? text : escapeHtml(text)),
  } } : {}) });
  const template = document.createElement("template");
  template.innerHTML = DOMPurify.sanitize(parser.parse(markdown), SANITIZE_PROFILE);
  // Random markers are generated per render, never read from input attributes.
  for (const span of template.content.querySelectorAll("span")) {
    const token = formulas.get(span.textContent);
    if (token && !span.children.length) {
      span.replaceWith(formulaNode(token.latex, token.raw, token.display));
    }
  }
  // Keep the console's heading, table and code conventions.
  for (const heading of template.content.querySelectorAll("h1, h2, h4, h5, h6")) {
    const h3 = document.createElement("h3");
    h3.append(...heading.childNodes);
    heading.replaceWith(h3);
  }
  for (const table of template.content.querySelectorAll("table")) {
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    table.replaceWith(wrap);
    wrap.appendChild(table);
  }
  for (const pre of template.content.querySelectorAll("pre")) pre.classList.add("codeblock");
  // Links from documents must not replace the console (and its results).
  for (const link of template.content.querySelectorAll("a[href]")) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  if (recognized) {
    for (const img of template.content.querySelectorAll("img")) {
      if (/^data:image\//i.test(img.getAttribute("src") || "")) continue;
      img.replaceWith(document.createTextNode(`[image${img.alt ? `: ${img.alt}` : ""}]`));
    }
  }
  return template.innerHTML;
}

export function renderFormula(source) {
  const trimmed = source.trim();
  const token = mathToken(trimmed);
  if (token && token.raw.length !== trimmed.length) return renderMarkdown(source);
  return formulaNode(token ? token.latex : trimmed, source, true).outerHTML;
}
