"""Rule-based formatter used when no AI provider is configured.

It never invents content: it only re-segments, capitalises, punctuates and
re-arranges the user's own words. Quality is lower than the AI formatter
(no spelling/grammar correction), but it works fully offline.
"""
from __future__ import annotations

import re

_LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_SUBJECT_START = r"(?:it|they|this|these|there|he|she|we|i|its|their)"
_ENUM_NOUNS = r"(components|types|parts|kinds|categories|examples|features|steps|elements|uses|advantages|disadvantages|functions|units|devices|properties|characteristics)"
_ENUM_RE = re.compile(
    rf"^(?P<lead>.*?\b(?:different|main|various|several|many|two|three|four|five|following)?\s*{_ENUM_NOUNS})\s*(?:are|is|include|includes|like|such as|namely|:)?\s+(?P<items>[^.]+?)\s*\.?$",
    re.IGNORECASE,
)


def _clean_spaces(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"([,.;:!?])(?=[A-Za-z])", r"\1 ", s)
    return s.strip()


def _sentence_case(s: str) -> str:
    s = s.strip()
    if not s:
        return s
    s = re.sub(r"\bi\b", "I", s)
    s = s[0].upper() + s[1:]
    if s[-1] not in ".!?:":
        s += "."
    return s


def _split_sentences(paragraph: str) -> list[str]:
    paragraph = _clean_spaces(paragraph)
    if re.search(r"[.!?]", paragraph):
        parts = re.split(r"(?<=[.!?])\s+", paragraph)
    else:
        # Unpunctuated run-on text: break before a new subject pronoun.
        parts = re.split(rf"\s+(?={_SUBJECT_START}\s+\w)", paragraph, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _split_items(items: str) -> list[str]:
    items = items.strip().rstrip(".")
    parts = re.split(r"\s*,\s*|\s+and\s+|\s*;\s*", items)
    return [p.strip() for p in parts if p.strip()]


def _title_from(sentences: list[str], first_line: str) -> str:
    fl = first_line.strip().rstrip(".:")
    if 0 < len(fl.split()) <= 6 and not re.search(r"\b(is|are|was|were)\b", fl, re.I):
        return fl
    if sentences:
        m = re.match(r"^(?:an?|the)?\s*([\w\s-]{1,40}?)\s+(?:is|are|was|were|means|refers)\b", sentences[0], re.I)
        if m:
            return m.group(1).strip()
        return " ".join(sentences[0].split()[:5]).rstrip(".,")
    return "Notes"


def _parse(text: str):
    """Returns (first_line, blocks) where blocks are ('para', [sentences]) or ('list', [items])."""
    text = text.replace("\r\n", "\n").strip()
    first_line = text.split("\n", 1)[0]
    blocks: list[tuple[str, list[str]]] = []
    for para in re.split(r"\n\s*\n", text):
        lines = [l for l in para.split("\n") if l.strip()]
        items = [m.group(1) for m in (_LIST_RE.match(l) for l in lines) if m]
        if items and len(items) == len(lines):
            blocks.append(("list", [_clean_spaces(i) for i in items]))
            continue
        # Join wrapped handwriting lines into one paragraph
        joined = " ".join(l.strip() for l in lines)
        blocks.append(("para", _split_sentences(joined)))
    return first_line, blocks


def _enumeration(sentence: str):
    m = _ENUM_RE.match(sentence)
    if not m:
        return None
    items = _split_items(m.group("items"))
    if len(items) < 2 or any(len(i.split()) > 6 for i in items):
        return None
    noun = re.search(_ENUM_NOUNS, m.group("lead"), re.I).group(1)
    return noun.capitalize(), [i[0].upper() + i[1:] for i in items], sentence


def offline_format(text: str, style: str = "academic") -> str:
    first_line, blocks = _parse(text)
    all_sentences = [s for kind, b in blocks if kind == "para" for s in b]
    title = _title_from(all_sentences, first_line)

    # If the first line was used as a short title, drop it from the body.
    if blocks and blocks[0][0] == "para" and blocks[0][1] and blocks[0][1][0].rstrip(".:").strip() == title:
        blocks[0] = ("para", blocks[0][1][1:])

    if style == "simple":
        out = []
        for kind, b in blocks:
            if kind == "list":
                out.append("\n".join(f"- {_sentence_case(i).rstrip('.')}" for i in b))
            elif b:
                out.append(" ".join(_sentence_case(s) for s in b))
        return "\n\n".join(out).strip() + "\n"

    if style == "outline":
        lines = [f"# {title.title()}", ""]
        n = 0
        for kind, b in blocks:
            for s in b:
                enum = _enumeration(s) if kind == "para" else None
                n += 1
                if enum:
                    lines.append(f"{n}. {enum[0]}")
                    lines += [f"   - {i}" for i in enum[1]]
                else:
                    lines.append(f"{n}. {_sentence_case(s).rstrip('.')}")
        return "\n".join(lines) + "\n"

    if style == "meeting":
        lines = ["# Meeting Notes", "", f"**Topic:** {title}", "", "## Discussion Points"]
        for kind, b in blocks:
            lines += [f"- {_sentence_case(s).rstrip('.')}" for s in b]
        return "\n".join(lines) + "\n"

    # Structured styles: professional / academic / assignment / report
    sections: list[tuple[str, list[str]]] = []
    intro: list[str] = []
    for kind, b in blocks:
        if kind == "list":
            sections.append(("Key Points", [f"- {_sentence_case(i).rstrip('.')}" for i in b]))
            continue
        for s in b:
            enum = _enumeration(s)
            if enum:
                heading, items, _ = enum
                sections.append((heading, [f"- {i}" for i in items]))
            else:
                (sections[-1][1] if sections and not sections[-1][1][0].startswith("- ") else intro).append(_sentence_case(s))

    first_heading = {"report": "Summary", "assignment": "Introduction"}.get(style, "Introduction")
    lines = [f"# {title.upper() if style in ('academic', 'professional') else title.title()}", ""]
    num = 0
    if intro:
        num += 1
        lines += [f"## {num}. {first_heading}", "", " ".join(intro), ""]
    for heading, body in sections:
        num += 1
        lines += [f"## {num}. {heading}", ""]
        lines += body if body[0].startswith("- ") else [" ".join(body)]
        lines.append("")
    return "\n".join(lines).strip() + "\n"
