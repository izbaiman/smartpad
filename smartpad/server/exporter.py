"""Export organised Markdown to DOCX, PDF, TXT and MD.

A small Markdown block parser (headings, paragraphs, bullet / numbered /
nested / checkbox lists, tables, **bold**, *italic*) feeds both the Word and the
PDF renderer so the two outputs have the same structure.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class Block:
    kind: str  # heading | para | ul | ol | table | hr
    text: str = ""
    level: int = 0
    items: list[tuple[int, str, bool | None]] = field(default_factory=list)  # (indent, text, checked)
    rows: list[list[str]] = field(default_factory=list)


_UL = re.compile(r"^(\s*)[-*+\u2022]\s+(?:\[( |x|X)\]\s+)?(.*)$")
_OL = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_H = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def parse_markdown(md: str) -> list[Block]:
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    para: list[str] = []
    i = 0

    def flush():
        if para:
            blocks.append(Block("para", " ".join(p.strip() for p in para)))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush()
            blocks.append(Block("hr"))
            i += 1
            continue
        m = _H.match(stripped)
        if m:
            flush()
            blocks.append(Block("heading", m.group(2), level=len(m.group(1))))
            i += 1
            continue
        if "|" in stripped and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            rows = [_cells(stripped)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            blocks.append(Block("table", rows=rows))
            continue
        if _UL.match(line) or _OL.match(line):
            flush()
            kind = "ul" if _UL.match(line) else "ol"
            block = Block(kind)
            while i < len(lines) and lines[i].strip():
                mu, mo = _UL.match(lines[i]), _OL.match(lines[i])
                if mu:
                    checked = None if mu.group(2) is None else mu.group(2).lower() == "x"
                    block.items.append((len(mu.group(1).expandtabs(4)) // 2, mu.group(3), checked))
                elif mo:
                    block.items.append((len(mo.group(1).expandtabs(4)) // 2, mo.group(2), None))
                elif block.items:  # continuation line
                    ind, txt, chk = block.items[-1]
                    block.items[-1] = (ind, txt + " " + lines[i].strip(), chk)
                i += 1
            blocks.append(block)
            continue
        para.append(stripped)
        i += 1
    flush()
    return blocks


_INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|`[^`]+`)")


def inline_runs(text: str) -> list[tuple[str, bool, bool]]:
    """Split text into (segment, bold, italic) runs."""
    runs = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if (part.startswith("**") and part.endswith("**")) or (part.startswith("__") and part.endswith("__")):
            runs.append((part[2:-2], True, False))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            runs.append((part[1:-1], False, True))
        elif part.startswith("`") and part.endswith("`"):
            runs.append((part[1:-1], False, False))
        else:
            runs.append((part, False, False))
    return runs


def plain(text: str) -> str:
    return "".join(r[0] for r in inline_runs(text))


def _has_rtl(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", text))


# ---------------------------------------------------------------------------
# TXT / MD
# ---------------------------------------------------------------------------
def to_txt(md: str) -> bytes:
    out: list[str] = []
    for b in parse_markdown(md):
        if b.kind == "heading":
            t = plain(b.text)
            out += [t.upper() if b.level == 1 else t, ("=" if b.level == 1 else "-") * len(t) if b.level <= 2 else "", ""]
        elif b.kind == "para":
            out += [plain(b.text), ""]
        elif b.kind in ("ul", "ol"):
            counters: dict[int, int] = {}
            for ind, txt, chk in b.items:
                counters[ind] = counters.get(ind, 0) + 1
                for k in [k for k in counters if k > ind]:
                    del counters[k]
                mark = f"{counters[ind]}." if b.kind == "ol" else ("[x]" if chk else "[ ]" if chk is False else "*")
                out.append("    " * ind + f"{mark} {plain(txt)}")
            out.append("")
        elif b.kind == "table":
            widths = [max(len(plain(r[c])) for r in b.rows) for c in range(len(b.rows[0]))]
            for ri, r in enumerate(b.rows):
                out.append("  ".join(plain(c).ljust(w) for c, w in zip(r, widths)).rstrip())
                if ri == 0:
                    out.append("  ".join("-" * w for w in widths))
            out.append("")
        elif b.kind == "hr":
            out += ["-" * 40, ""]
    return ("\n".join(l for l in out).strip() + "\n").replace("\n\n\n", "\n\n").encode("utf-8")


def to_md(md: str) -> bytes:
    return (md.strip() + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def to_docx(md: str, title: str = "") -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn("w:cs"), "Arial")  # complex-script (Urdu) font
    if title:
        doc.core_properties.title = title

    def add_runs(par, text):
        for seg, bold, italic in inline_runs(text):
            r = par.add_run(seg)
            r.bold = bold or None
            r.italic = italic or None
        if _has_rtl(text):
            par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            pPr = par._p.get_or_add_pPr()
            bidi = pPr.makeelement(qn("w:bidi"), {})
            pPr.append(bidi)

    for b in parse_markdown(md):
        if b.kind == "heading":
            if b.level == 1:
                par = doc.add_heading(level=0)  # Title style
            else:
                par = doc.add_heading(level=min(b.level - 1, 4))
            add_runs(par, b.text)
        elif b.kind == "para":
            add_runs(doc.add_paragraph(), b.text)
        elif b.kind in ("ul", "ol"):
            for ind, txt, chk in b.items:
                lvl = min(ind, 2)
                if b.kind == "ol":
                    sty = "List Number" if lvl == 0 else f"List Number {lvl + 1}"
                else:
                    sty = "List Bullet" if lvl == 0 else f"List Bullet {lvl + 1}"
                par = doc.add_paragraph(style=sty)
                if chk is not None:
                    par.add_run("\u2612 " if chk else "\u2610 ")
                add_runs(par, txt)
        elif b.kind == "table":
            table = doc.add_table(rows=len(b.rows), cols=len(b.rows[0]))
            table.style = "Table Grid"
            for ri, row in enumerate(b.rows):
                for ci, cell in enumerate(row):
                    c = table.cell(ri, ci)
                    c.text = ""
                    par = c.paragraphs[0]
                    for seg, bold, italic in inline_runs(cell):
                        r = par.add_run(seg)
                        r.bold = True if ri == 0 else (bold or None)
                        r.italic = italic or None
            doc.add_paragraph()
        elif b.kind == "hr":
            doc.add_paragraph("_" * 40)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
_FONT_CACHE: dict[str, str] = {}


def _pdf_fonts() -> tuple[str, str]:
    """Register a Unicode TTF if available (needed for non-Latin text)."""
    if _FONT_CACHE:
        return _FONT_CACHE["regular"], _FONT_CACHE["bold"]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (Path("/Library/Fonts/Arial Unicode.ttf"), Path("/Library/Fonts/Arial Unicode.ttf")),
    ]
    reg, bold = "Helvetica", "Helvetica-Bold"
    for r, b in candidates:
        if r.exists() and b.exists():
            try:
                pdfmetrics.registerFont(TTFont("SPRegular", str(r)))
                pdfmetrics.registerFont(TTFont("SPBold", str(b)))
                from reportlab.lib.fonts import addMapping

                addMapping("SPRegular", 0, 0, "SPRegular")
                addMapping("SPRegular", 1, 0, "SPBold")
                addMapping("SPRegular", 0, 1, "SPRegular")
                addMapping("SPRegular", 1, 1, "SPBold")
                reg, bold = "SPRegular", "SPBold"
                break
            except Exception:
                continue
    _FONT_CACHE.update(regular=reg, bold=bold)
    return reg, bold


def _rl_inline(text: str) -> str:
    out = []
    for seg, bold, italic in inline_runs(text):
        s = escape(seg)
        if bold:
            s = f"<b>{s}</b>"
        if italic:
            s = f"<i>{s}</i>"
        out.append(s)
    return "".join(out)


def to_pdf(md: str, title: str = "") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    reg, bold = _pdf_fonts()
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontName=reg, fontSize=11, leading=15, spaceAfter=6)
    body_rtl = ParagraphStyle("body_rtl", parent=body, alignment=TA_RIGHT)
    hstyles = {
        1: ParagraphStyle("h1", parent=ss["Title"], fontName=bold, fontSize=20, leading=24, spaceAfter=12),
        2: ParagraphStyle("h2", parent=ss["Heading2"], fontName=bold, fontSize=15, leading=19, spaceBefore=10, spaceAfter=6, textColor=colors.HexColor("#1f3a5f")),
        3: ParagraphStyle("h3", parent=ss["Heading3"], fontName=bold, fontSize=12.5, leading=16, spaceBefore=8, spaceAfter=4),
    }

    def para(text, style=None):
        st = style or (body_rtl if _has_rtl(text) else body)
        return Paragraph(_rl_inline(text), st)

    def build_list(items, kind):
        """Each item becomes an indented paragraph with its own bullet/number (supports nesting)."""
        flow, counters = [], {}
        for ind, txt, chk in items:
            lvl = min(ind, 4)
            counters[lvl] = counters.get(lvl, 0) + 1
            for k in [k for k in counters if k > lvl]:
                del counters[k]
            if chk is not None:
                bullet = "\u2612" if chk else "\u2610"
            elif kind == "ol":
                bullet = f"{counters[lvl]}."
            else:
                bullet = "\u2022" if lvl == 0 else "\u2013"
            st = ParagraphStyle(f"li{lvl}", parent=body, leftIndent=20 + 18 * lvl, bulletIndent=6 + 18 * lvl, spaceAfter=2, bulletFontName=reg)
            flow.append(Paragraph(_rl_inline(txt), st, bulletText=bullet))
        return flow

    story = []
    for b in parse_markdown(md):
        if b.kind == "heading":
            story.append(para(b.text, hstyles.get(b.level, hstyles[3])))
        elif b.kind == "para":
            story.append(para(b.text))
        elif b.kind in ("ul", "ol"):
            story += build_list(b.items, b.kind) + [Spacer(1, 4)]
        elif b.kind == "table":
            data = [[para(c) for c in row] for row in b.rows]
            t = Table(data, repeatRows=1, hAlign="LEFT")
            t.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9aa5b1")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story += [t, Spacer(1, 8)]
        elif b.kind == "hr":
            story.append(HRFlowable(width="100%", color=colors.grey))
    if not story:
        story.append(para(" "))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm, topMargin=2 * cm, bottomMargin=2 * cm, title=title or "SmartPad Document")
    doc.build(story)
    return buf.getvalue()


EXPORTERS = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", lambda md, t: to_docx(md, t)),
    "pdf": ("application/pdf", lambda md, t: to_pdf(md, t)),
    "txt": ("text/plain; charset=utf-8", lambda md, t: to_txt(md)),
    "md": ("text/markdown; charset=utf-8", lambda md, t: to_md(md)),
}


def export(md: str, fmt: str, title: str = "") -> tuple[bytes, str]:
    if fmt not in EXPORTERS:
        raise ValueError(f"Unsupported format '{fmt}'. Choose one of: {', '.join(EXPORTERS)}")
    mime, fn = EXPORTERS[fmt]
    return fn(md, title), mime


def safe_filename(title: str, fmt: str) -> str:
    base = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip().replace(" ", "_")[:60] or "SmartPad_Document"
    return f"{base}.{fmt}"
