"""Fully local handwriting recognition with Microsoft TrOCR (no API key, no internet after setup).

TrOCR recognises ONE line of text at a time, so a page image is first split into
lines (horizontal projection profile), long lines are split at word gaps, then
all pieces are recognised in one batch and stitched back together.

The model (~250 MB) is downloaded once from Hugging Face into  smartpad/models/
and loaded in a background thread when the server starts.
"""
from __future__ import annotations

import io
import logging
import os
import re
import threading
import time

from .config import ROOT_DIR

# Keep the model cache inside the project folder.
os.environ.setdefault("HF_HOME", str(ROOT_DIR / "models"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

log = logging.getLogger("smartpad.ocr")

# small: ~250 MB, ~0.6s/line on CPU.  base: ~1.3 GB, ~3s/line, a bit better on messy handwriting.
MODEL_ID = os.environ.get("SMARTPAD_OCR_MODEL") or "microsoft/trocr-small-handwritten"
# 1 = greedy decoding (fast). 3-4 = beam search (slightly more accurate, ~2-3x slower).
BEAMS = max(1, int(os.environ.get("SMARTPAD_OCR_BEAMS", "1")))


class LocalOCR:
    def __init__(self) -> None:
        self.state = "not_loaded"  # not_loaded | loading | ready | unavailable | error
        self.error: str | None = None
        self.load_seconds: float | None = None
        self._processor = None
        self._model = None
        self._lock = threading.Lock()
        self._ready = threading.Event()

    # ---- availability / loading ------------------------------------------
    @staticmethod
    def installed() -> bool:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401

            return True
        except ImportError:
            return False

    def start_background_load(self) -> None:
        if self.state in ("loading", "ready"):
            return
        if not self.installed():
            self.state = "unavailable"
            self.error = "PyTorch/transformers not installed (pip install -r requirements.txt)."
            return
        self.state = "loading"
        threading.Thread(target=self._load, name="trocr-loader", daemon=True).start()

    def _load(self) -> None:
        t0 = time.time()
        try:
            import torch
            from transformers import TrOCRProcessor, VisionEncoderDecoderModel

            # More than ~8 threads makes generation SLOWER (measured: 19 threads 7.2s vs 8 threads 3.1s per line).
            torch.set_num_threads(max(1, min(8, (os.cpu_count() or 2) - 1)))
            log.info("loading handwriting model %s (first run downloads it)…", MODEL_ID)
            self._processor = TrOCRProcessor.from_pretrained(MODEL_ID)
            self._model = VisionEncoderDecoderModel.from_pretrained(MODEL_ID)
            self._model.eval()
            self.load_seconds = round(time.time() - t0, 1)
            self.state = "ready"
            log.info("handwriting model ready in %.1fs", self.load_seconds)
        except Exception as exc:
            self.state = "error"
            self.error = f"Could not load handwriting model: {exc}"
            log.exception("model load failed")
        finally:
            self._ready.set()

    def wait_ready(self, timeout: float = 600) -> None:
        if self.state == "not_loaded":
            self.start_background_load()
        if self.state == "loading":
            self._ready.wait(timeout)
        if self.state != "ready":
            raise RuntimeError(self.error or f"Handwriting model is {self.state}.")

    # ---- recognition ------------------------------------------------------
    def recognize(self, png_bytes: bytes) -> str:
        from PIL import Image

        self.wait_ready()
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        lines = segment_lines(img)
        if not lines:
            return ""
        pieces: list[tuple[int, Image.Image]] = []
        for li, line in enumerate(lines):
            for chunk in split_wide_line(line["image"]):
                pieces.append((li, chunk))

        import torch

        texts: list[str] = []
        with self._lock, torch.inference_mode():
            for start in range(0, len(pieces), 8):  # batches of 8 keep memory modest
                batch = [p[1] for p in pieces[start : start + 8]]
                pixel_values = self._processor(images=batch, return_tensors="pt").pixel_values
                ids = self._model.generate(pixel_values, max_new_tokens=48, num_beams=BEAMS, early_stopping=BEAMS > 1)
                texts += self._processor.batch_decode(ids, skip_special_tokens=True)

        # Re-assemble: chunks of the same line joined by spaces, big vertical gaps = new paragraph.
        out_lines: list[str] = [""] * len(lines)
        for (li, _), txt in zip(pieces, texts):
            txt = re.sub(r"\s+([.,;:!?'])", r"\1", txt.strip())  # TrOCR emits "word ." style spacing
            out_lines[li] = (out_lines[li] + " " + txt).strip()
        result = []
        for i, txt in enumerate(out_lines):
            if not txt:
                continue
            if i and lines[i]["para_break"] and result:
                result.append("")
            result.append(txt)
        return "\n".join(result).strip()


# ---------------------------------------------------------------------------
# Image segmentation (pure numpy)
# ---------------------------------------------------------------------------
def _ink_mask(img):
    import numpy as np

    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    return gray < 170


def _runs(flags) -> list[tuple[int, int]]:
    """[start, end) index ranges where flags is True."""
    runs, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(flags)))
    return runs


def segment_lines(img) -> list[dict]:
    """Split a page into text lines using the horizontal ink profile."""
    import numpy as np

    mask = _ink_mask(img)
    if mask.sum() < 20:
        return []
    rows = mask.sum(axis=1).astype(float)
    k = 5
    smooth = np.convolve(rows, np.ones(k) / k, mode="same")
    runs = _runs(smooth > max(1.0, 0.02 * smooth.max()))
    if not runs:
        return []

    # Merge runs separated by small gaps (i-dots, accents, descenders touching next line).
    heights = sorted(e - s for s, e in runs)
    med_h = heights[len(heights) // 2]
    merged = [list(runs[0])]
    for s, e in runs[1:]:
        gap = s - merged[-1][1]
        small = (e - s) < 0.35 * med_h or (merged[-1][1] - merged[-1][0]) < 0.35 * med_h
        if gap < 0.25 * med_h or (small and gap < 0.6 * med_h):
            merged[-1][1] = e
        else:
            merged.append([s, e])
    # Drop specks
    merged = [r for r in merged if (r[1] - r[0]) >= max(6, 0.2 * med_h) and mask[r[0] : r[1]].sum() > 30]
    if not merged:
        return []

    gaps = [merged[i][0] - merged[i - 1][1] for i in range(1, len(merged))]
    med_gap = sorted(gaps)[(len(gaps) - 1) // 2] if gaps else 0  # lower median: typical line spacing
    h_img, w_img = mask.shape
    lines = []
    for i, (s, e) in enumerate(merged):
        cols = np.where(mask[s:e].any(axis=0))[0]
        x0, x1 = int(cols.min()), int(cols.max()) + 1
        pad = max(8, (e - s) // 4)
        box = (max(0, x0 - pad), max(0, s - pad), min(w_img, x1 + pad), min(h_img, e + pad))
        para_break = bool(i and gaps[i - 1] > max(1.8 * med_gap, 1.2 * (e - s)) and len(merged) > 2)
        lines.append({"image": img.crop(box), "box": box, "para_break": para_break})
    return lines


def split_wide_line(line_img, max_aspect: float = 9.0) -> list:
    """TrOCR squashes input to 384x384; very long lines lose detail, so split at word gaps."""
    import numpy as np

    w, h = line_img.size
    if w / max(h, 1) <= max_aspect:
        return [line_img]
    mask = _ink_mask(line_img)
    col_ink = mask.any(axis=0)
    gaps = [(s, e) for s, e in _runs(~col_ink) if s > 0 and e < w and (e - s) >= max(6, h * 0.18)]
    if not gaps:
        return [line_img]
    target = int(h * max_aspect * 0.8)
    chunks, start = [], 0
    for s, e in gaps:
        cut = (s + e) // 2
        if cut - start >= target:
            chunks.append(line_img.crop((start, 0, cut, h)))
            start = cut
    chunks.append(line_img.crop((start, 0, w, h)))
    return [c for c in chunks if np.asarray(c.convert("L")).min() < 170]


ocr = LocalOCR()
