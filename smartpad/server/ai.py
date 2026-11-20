"""Handwriting recognition and AI text organisation.

Providers
---------
* ``anthropic`` - Claude vision transcribes the handwriting image, and Claude
  corrects + organises the text. Best quality; needs ANTHROPIC_API_KEY on the
  computer (never sent to the phone).
* ``local``     - DEFAULT when no key is set. Everything runs on this computer:
  handwriting -> Microsoft TrOCR model (``local_ocr.py``), spelling ->
  ``spell.py``, structure -> rule-based ``offline_format.py``.
  Urdu recognition falls back to Tesseract (``urd`` pack) if installed.

``SMARTPAD_AI_PROVIDER=auto`` (default) picks ``anthropic`` only when a key is
configured, otherwise ``local``. ``offline`` is accepted as an alias of ``local``.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os

from . import spell
from .config import settings
from .local_ocr import MODEL_ID as LOCAL_MODEL_ID, ocr as local_ocr
from .offline_format import offline_format

log = logging.getLogger("smartpad.ai")

STYLES: dict[str, str] = {
    "simple": "Simple Text",
    "professional": "Professional Notes",
    "academic": "Academic Notes",
    "assignment": "Assignment Format",
    "report": "Report Format",
    "meeting": "Meeting Notes",
    "outline": "Structured Outline",
}

STYLE_GUIDE: dict[str, str] = {
    "simple": "Clean, corrected paragraphs only. No headings unless the writer clearly wrote one. No lists unless the writer listed items.",
    "professional": "A short ALL-CAPS or Title Case title as '#'. Numbered '##' sections. Concise paragraphs and bullet points for listed items. Business tone.",
    "academic": "A '#' title with the topic. Numbered '##' sections such as Introduction, key concepts, components/types, and a short Summary only if the content supports it. Define terms in bold. Bullets for enumerations. Use a Markdown table when the writer compares items or lists attributes.",
    "assignment": "'# Title', then '## Introduction', '## Main Body' sections with '###' subheadings as needed, and '## Conclusion' that only restates what the writer said. Formal academic tone, full paragraphs.",
    "report": "'# Title', '## Summary', then numbered '##' sections for findings/details, '## Conclusion' (only from the writer's content). Formal, objective tone; tables for structured data.",
    "meeting": "'# Meeting Notes' title (include topic/date only if written), '## Attendees', '## Discussion Points', '## Decisions', '## Action Items' (as a checklist '- [ ]' with owner/date if written). Omit any section the writer gave no content for.",
    "outline": "A hierarchical outline: '# Topic', then numbered lists with nested sub-points (indent with 3 spaces). Keep each point short.",
}

ORGANIZE_SYSTEM = """You are the formatting engine of a handwriting-to-document app.
You receive raw text recognised from the user's handwriting. Your job:
1. Fix spelling, grammar, capitalisation, punctuation and spacing.
2. Organise the content into the requested document style using GitHub-flavoured Markdown
   (#/##/### headings, numbered lists, "-" bullets, tables with | pipes |, **bold** for key terms).

Hard rules - these override the style:
- Do NOT add facts, examples, definitions, numbers, names or dates that are not in the input.
- Do NOT remove information or change the intended meaning. Rephrasing for grammar is fine.
- Section headings may only label content that is actually present; never create empty sections.
- If the input is in Urdu, keep it in Urdu (headings in Urdu too). Same for any other language.
- Keep mathematical expressions exactly as written.
- Output ONLY the Markdown document. No preface, no explanation, no code fences."""

RECOGNIZE_PROMPT = """Transcribe the handwriting in this image exactly as written.
- Output only the transcribed text, nothing else.
- Preserve line breaks and paragraph breaks as the writer made them.
- Keep the writer's own words and order. Do NOT correct spelling or grammar and do not add anything.
- If a word is illegible, give your best reading followed by [?].
- Write mathematical expressions in plain text / LaTeX-style (e.g. x^2 + 3x = 10, \\frac{{a}}{{b}}).
- Expected language: {language}.
- If the image contains no handwriting, output exactly: [blank]"""


class AIError(Exception):
    """User-facing error from the AI layer."""


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------
def _has_anthropic_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def active_provider() -> str:
    p = settings.ai_provider
    if p == "auto":
        return "anthropic" if _has_anthropic_credentials() else "local"
    return "local" if p == "offline" else p


def tesseract_available() -> bool:
    try:
        import pytesseract  # noqa: F401

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def capabilities() -> dict:
    provider = active_provider()
    if provider == "anthropic":
        engine, model_state, recognition = "claude-vision", "ready", True
    else:
        installed = local_ocr.installed()
        engine = "trocr" if installed else ("tesseract" if tesseract_available() else None)
        model_state = local_ocr.state if installed else "unavailable"
        recognition = engine is not None and model_state != "error"
    return {
        "provider": provider,
        "model": settings.model if provider == "anthropic" else LOCAL_MODEL_ID,
        "recognition": recognition,
        "recognition_engine": engine,
        "model_state": model_state,  # not_loaded | loading | ready | unavailable | error
        "model_error": local_ocr.error if provider != "anthropic" else None,
        "urdu_recognition": provider == "anthropic" or tesseract_available(),
        "smart_format": provider == "anthropic",
        "correction": "claude" if provider == "anthropic" else ("spellcheck" if spell.available() else None),
        "styles": STYLES,
        "languages": {"en": "English", "ur": "Urdu"},
    }


_client = None


def _anthropic_client():
    global _client
    if _client is None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise AIError("The 'anthropic' package is not installed. Run: pip install -r requirements.txt") from exc
        _client = anthropic.Anthropic(max_retries=2, timeout=120.0)
    return _client


def _call_claude(system: str | None, content: list | str, effort: str) -> str:
    """One Messages API call with refusal fallback + typed error handling."""
    import anthropic

    client = _anthropic_client()
    kwargs = dict(
        model=settings.model,
        max_tokens=16000,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": content}],
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if system:
        kwargs["system"] = system
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise AIError("Anthropic API key is invalid. Check ANTHROPIC_API_KEY in the .env file on the computer.") from exc
    except anthropic.PermissionDeniedError as exc:
        raise AIError("The API key does not have access to this model.") from exc
    except anthropic.NotFoundError as exc:
        raise AIError(f"Model '{settings.model}' not found. Check SMARTPAD_MODEL.") from exc
    except anthropic.RateLimitError as exc:
        raise AIError("AI rate limit reached. Wait a minute and try again.") from exc
    except anthropic.BadRequestError as exc:
        raise AIError(f"AI request rejected: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"AI service error ({exc.status_code}). Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError("Cannot reach the AI service. Check the computer's internet connection, or switch to offline mode.") from exc

    if response.stop_reason == "refusal":
        raise AIError("The AI declined to process this content.")
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if response.stop_reason == "max_tokens":
        log.warning("AI output hit max_tokens; result may be truncated")
    if not text:
        raise AIError("The AI returned an empty result.")
    return text


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------
def decode_image(data_url: str) -> tuple[bytes, str]:
    """Accepts a data URL (data:image/png;base64,...) or bare base64. Returns (bytes, media_type)."""
    if not data_url:
        raise AIError("No image received.")
    media_type = "image/png"
    payload = data_url
    if data_url.startswith("data:"):
        header, _, payload = data_url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
    if media_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise AIError(f"Unsupported image type {media_type}.")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AIError("Image data is corrupted.") from exc
    if len(raw) > 8 * 1024 * 1024:
        raise AIError("Image too large (max 8 MB).")
    return raw, media_type


def recognize(image_data_url: str, language: str = "en") -> dict:
    raw, media_type = decode_image(image_data_url)
    lang_name = {"en": "English", "ur": "Urdu (Nastaliq/Naskh script; output Urdu script)"}.get(language, "English")
    provider = active_provider()

    if provider == "anthropic":
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(raw).decode()}},
            {"type": "text", "text": RECOGNIZE_PROMPT.format(language=lang_name)},
        ]
        text = _call_claude(None, content, effort="low")
        if text.strip() == "[blank]":
            text = ""
        return {"text": text, "engine": "claude-vision"}

    # --- local engines -----------------------------------------------------
    if language == "en" and local_ocr.installed():
        if local_ocr.state == "loading":
            log.info("recognition requested while model is loading; waiting…")
        try:
            text = local_ocr.recognize(raw)
        except RuntimeError as exc:
            raise AIError(str(exc)) from exc
        return {"text": text, "engine": "trocr (local)"}

    if tesseract_available():
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(raw)).convert("L")
        lang = "urd" if language == "ur" else "eng"
        try:
            text = pytesseract.image_to_string(img, lang=lang)
        except pytesseract.TesseractError as exc:
            raise AIError(f"Tesseract failed ({exc}). For Urdu install the 'urd' language pack.") from exc
        return {"text": text.strip(), "engine": "tesseract"}

    if language == "ur":
        raise AIError(
            "Urdu handwriting can't be recognised offline: the local model reads English only. "
            "Type the Urdu text in the review box (the organiser and Word export support Urdu), "
            "or install Tesseract OCR with the 'urd' language pack."
        )
    raise AIError(
        "The local handwriting model is not installed. Run: pip install -r requirements.txt. "
        "You can still type or correct the text manually and use the organiser."
    )


# ---------------------------------------------------------------------------
# Organisation
# ---------------------------------------------------------------------------
def organize(text: str, style: str = "academic", use_ai: bool = True) -> dict:
    text = (text or "").strip()
    if not text:
        raise AIError("There is no text to organise.")
    if len(text) > 60000:
        raise AIError("Text too long (max 60,000 characters).")
    if style not in STYLES:
        style = "simple"

    if use_ai and active_provider() == "anthropic":
        prompt = (
            f"Requested style: {STYLES[style]}\n"
            f"Style guide: {STYLE_GUIDE[style]}\n\n"
            "Raw handwritten text (between the markers):\n"
            "<<<BEGIN>>>\n"
            f"{text}\n"
            "<<<END>>>"
        )
        md = _call_claude(ORGANIZE_SYSTEM, prompt, effort="medium")
        # Strip accidental code fences
        if md.startswith("```"):
            md = md.strip("`")
            md = md.split("\n", 1)[1] if "\n" in md else md
        return {"markdown": md.strip(), "engine": "claude", "style": style}

    changes: list[tuple[str, str]] = []
    if use_ai:
        text, changes = spell.correct(text)
    return {
        "markdown": offline_format(text, style),
        "engine": "local",
        "style": style,
        "corrections": [{"from": a, "to": b} for a, b in changes],
    }
