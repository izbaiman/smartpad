"""Accuracy check for the local handwriting engine.

Renders multi-line text in Windows' handwriting-style fonts (a stand-in for real
handwriting), runs it through segmentation + TrOCR, and reports per-sample
word accuracy.  Run:  .venv\\Scripts\\python tests\\ocr_sample_check.py
"""
import difflib
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image, ImageDraw, ImageFont

from server.local_ocr import ocr, segment_lines

SAMPLES = [
    "computer is an electronic machine\nit accepts data process data\nand produces information\nit has different components\nhardware and software",
    "Meeting with the design team\nreview the new homepage\nAli will send the report on Monday",
    "The quick brown fox jumps over the lazy dog",
]
FONTS = [Path("C:/Windows/Fonts/Inkfree.ttf"), Path("C:/Windows/Fonts/segoepr.ttf")]


def render(text: str, font_path: Path, size: int = 34) -> bytes:
    font = ImageFont.truetype(str(font_path), size)
    lines = text.split("\n")
    w = max(int(font.getlength(l)) for l in lines) + 60
    h = len(lines) * int(size * 1.9) + 40
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for i, l in enumerate(lines):
        d.text((30, 20 + i * int(size * 1.9)), l, font=font, fill=(17, 17, 17))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def word_acc(ref: str, hyp: str) -> float:
    norm = lambda s: [w.strip(".,").lower() for w in s.split()]
    sm = difflib.SequenceMatcher(None, norm(ref), norm(hyp))
    return sum(b.size for b in sm.get_matching_blocks()) / max(1, len(norm(ref)))


t0 = time.time()
ocr.wait_ready()
print(f"model ready in {time.time() - t0:.1f}s\n")
total = []
for font in [f for f in FONTS if f.exists()]:
    for text in SAMPLES:
        png = render(text, font)
        n_lines = len(segment_lines(Image.open(io.BytesIO(png)).convert("RGB")))
        t1 = time.time()
        out = ocr.recognize(png)
        acc = word_acc(text, out)
        total.append(acc)
        print(f"[{font.stem}] lines expected {text.count(chr(10)) + 1}, found {n_lines}, {time.time() - t1:.1f}s, word accuracy {acc:.0%}")
        print("  got:", out.replace("\n", " | "))
print(f"\nmean word accuracy: {sum(total) / len(total):.0%}")
