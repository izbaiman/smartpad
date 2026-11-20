/* Tiny Markdown renderer for the document preview.
 * Supports the same subset the exporter understands: headings, paragraphs,
 * bullet / numbered / nested / task lists, tables, **bold**, *italic*, `code`, ---.
 * All text is HTML-escaped first, so recognised text can never inject markup. */
(function () {
  const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const RTL = /[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]/;

  function inline(s) {
    return esc(s)
      .replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, (_, a, b) => `<strong>${a || b}</strong>`)
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }
  const dirAttr = (s) => (RTL.test(s) ? ' dir="rtl"' : "");
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const UL = /^(\s*)[-*+•]\s+(?:\[( |x|X)\]\s+)?(.*)$/;
  const OL = /^(\s*)\d+[.)]\s+(.*)$/;
  const SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function renderList(items) {
    // items: [{indent, text, checked, ordered}]
    let html = "";
    const stack = [];
    for (const it of items) {
      while (stack.length && stack[stack.length - 1].indent > it.indent) html += `</li></${stack.pop().tag}>`;
      const top = stack[stack.length - 1];
      if (!top || top.indent < it.indent) {
        const tag = it.ordered ? "ol" : "ul";
        stack.push({ indent: it.indent, tag });
        html += `<${tag}>`;
      } else {
        html += "</li>";
      }
      const box = it.checked === undefined ? "" : it.checked ? "☒ " : "☐ ";
      html += `<li${it.checked !== undefined ? ' class="task"' : ""}${dirAttr(it.text)}>${box}${inline(it.text)}`;
    }
    while (stack.length) html += `</li></${stack.pop().tag}>`;
    return html;
  }

  function render(md) {
    const lines = (md || "").replace(/\r\n/g, "\n").split("\n");
    let out = "", para = [], i = 0;
    const flush = () => {
      if (para.length) {
        const t = para.join(" ");
        out += `<p${dirAttr(t)}>${inline(t)}</p>`;
        para = [];
      }
    };
    while (i < lines.length) {
      const line = lines[i], t = line.trim();
      if (!t) { flush(); i++; continue; }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { flush(); out += "<hr>"; i++; continue; }
      const h = t.match(/^(#{1,6})\s+(.*?)\s*#*$/);
      if (h) { flush(); const n = h[1].length; out += `<h${n}${dirAttr(h[2])}>${inline(h[2])}</h${n}>`; i++; continue; }
      if (t.includes("|") && i + 1 < lines.length && SEP.test(lines[i + 1])) {
        flush();
        const head = cells(t);
        out += "<table><thead><tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>";
        i += 2;
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          out += "<tr>" + cells(lines[i]).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>";
          i++;
        }
        out += "</tbody></table>";
        continue;
      }
      if (UL.test(line) || OL.test(line)) {
        flush();
        const items = [];
        while (i < lines.length && lines[i].trim()) {
          const u = lines[i].match(UL), o = lines[i].match(OL);
          if (u) items.push({ indent: Math.floor(u[1].length / 2), text: u[3], checked: u[2] === undefined ? undefined : u[2].toLowerCase() === "x", ordered: false });
          else if (o) items.push({ indent: Math.floor(o[1].length / 2), text: o[2], ordered: true });
          else if (items.length) items[items.length - 1].text += " " + lines[i].trim();
          i++;
        }
        out += renderList(items);
        continue;
      }
      para.push(t);
      i++;
    }
    flush();
    return out;
  }

  window.SmartMarkdown = { render, escape: esc };
})();
