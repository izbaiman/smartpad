# SmartPad: phone touchpad and smart handwriting pad

SmartPad turns your phone into:

- **A wireless touchpad** for your computer. You can move the cursor, tap, double-tap, right-click, scroll, drag, and type on the computer from the phone.
- **A handwriting pad.** You write on the phone with a finger or stylus. SmartPad recognizes the handwriting, lets you check and fix the text, organizes it into notes, reports, outlines and similar formats, and exports Word, PDF, TXT or Markdown.

You don't install an app on the phone. The computer runs a small server, and the phone opens it in Chrome after you scan a QR code.

**Everything runs on your computer. No API key or account is needed.** Handwriting is read by a local AI model (Microsoft TrOCR), and spelling is corrected by an offline dictionary. After the one-time model download, nothing leaves your Wi-Fi network.

---

## 1. Quick start (Windows)

1. Install **Python 3.10+** from python.org. Tick "Add to PATH" during setup.
2. Double-click **`start.bat`**. The first run creates `.venv` and installs the dependencies (about 1 GB including PyTorch), which takes a few minutes.
3. If Windows Firewall asks, allow Python on **Private networks**.
4. A status page opens on the computer with a QR code. Make sure the phone is on the **same Wi-Fi**, then scan the code with the phone camera.
5. On the first start, the handwriting model (~250 MB) downloads into `models/` in the background. The status page shows "TrOCR handwriting model — ready" when it's done. Later starts load it from disk in a few seconds.

Manual start (any OS):

```
python -m venv .venv
.venv\Scripts\activate        (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python run.py                 (options: --https  --port 9000  --no-browser  --no-input)
```

### Several people, each controlling their own computer

One SmartPad server can be shared. Each person's phone controls **their own** computer, not the server's:

1. Start SmartPad on one computer (the server) as above.
2. On any other computer on the same network, open `http://<server-ip>:8765/connect` in the browser. The server's status page shows this address.
3. Click **Download SmartPad Helper**, unzip it, and double-click **SmartPad Helper.exe**. Python isn't needed: the `.exe` has it built in. If SmartScreen warns, click *More info → Run anyway*. On macOS or Linux, use the `with-python` folder instead.
4. The browser opens a page with **that computer's own QR code**. A phone that scans it controls only that computer.

A browser can't move the mouse by itself, so the helper is required. It connects to the server, gets its own pairing code, and applies only the events from phones paired with that code. Each computer also gets its own document history. *Save on computer* writes to `Documents\SmartPad` on that computer. Handwriting recognition still runs on the server.

The `.exe` is built on the server computer with `agentuild_helper_exe.bat`, which outputs `agent\dist\SmartPad Helper.exe`, and the download includes it automatically. Rebuild it after you change `agent/smartpad_agent.py` or `server/touchpad.py`. The server's own status page lists the other computers under **Other computers**.

---

## 2. Architecture

```
 PHONE (Chrome, Android)                         COMPUTER (Python)
 ┌──────────────────────────┐   Wi-Fi / LAN    ┌──────────────────────────────────────┐
 │ web/index.html + app.js  │                  │ run.py  → uvicorn → server/main.py   │
 │  • Touchpad gestures ────┼─ WebSocket /ws ─▶│  touchpad.py ─▶ pynput ─▶ real mouse │
 │  • Heartbeat/reconnect ◀─┼── ping / pong ───┤                                      │
 │  • Canvas (vector ink)   │                  │                                      │
 │  • Review / organise UI ─┼─ REST /api/* ───▶│  ai.py ─▶ local_ocr.py (TrOCR model) │
 │  • Export / history      │  (token header)  │        ├▶ spell.py + offline_format  │
 │                          │                  │        └▶ Claude (only if key set)   │
 └──────────────────────────┘                  │  exporter.py ─▶ DOCX / PDF / TXT / MD│
        ▲ scan QR (pairing URL + token)        │  storage.py  ─▶ data/documents/*.json│
        └──────────────────────────────────────│  /desktop status page (localhost)    │
                                               └──────────────────────────────────────┘
```

**Why this stack**

| Choice | Reason |
|---|---|
| **Phone = mobile web app served by the computer** (vanilla JS, no build step) | No app-store install, works on any Android browser, and starting it is one command. React/Next.js would add a Node toolchain and a build step without adding any capability this app needs. |
| **FastAPI + uvicorn** | Async WebSocket for the low-latency touchpad channel and REST for documents, in one process. |
| **WebSocket, events batched per animation frame** | At most one message per screen refresh (~60 Hz). Sub-pixel moves accumulate on the server, so slow finger movements aren't lost. |
| **pynput** | Cross-platform mouse and keyboard control on Windows, macOS and Linux (X11). |
| **Microsoft TrOCR (`trocr-small-handwritten`) via PyTorch + transformers** | A free, open handwriting-recognition model that runs on the CPU, with no key and no cloud. It reads one line at a time, so `local_ocr.py` splits the page into lines (and long lines at word gaps), recognizes them in a batch, and rebuilds lines and paragraphs. |
| **pyspellchecker + rule-based organizer** | Offline spelling correction that only changes clearly misspelled words and leaves acronyms and repeated names alone. The organizer then builds the headings, numbered sections and bullet lists. |
| **Claude (optional)** | If you ever add an `ANTHROPIC_API_KEY`, SmartPad can use it for Urdu handwriting and full grammar rewriting. It's off by default. |
| **python-docx + ReportLab** | Native Word styles (Title, Heading, List Bullet/Number, tables) and a PDF that uses a Unicode font. |

---

## 3. Project layout

```
smartpad/
├── run.py                  Entry point: prints QR, optional HTTPS cert, starts server
├── start.bat               One-click Windows launcher (creates venv on first run)
├── requirements.txt
├── .env.example            Config template (API key, port, HTTPS…) → copy to .env
├── server/
│   ├── config.py           Settings, .env loading, folder paths
│   ├── security.py         Pairing token, auth checks, localhost-only guard
│   ├── network.py          LAN IP detection, QR code generation
│   ├── touchpad.py         Touch events → mouse/keyboard (pynput)
│   ├── ai.py               Chooses the engine: local (default) or Claude (if key set)
│   ├── local_ocr.py        TrOCR model loading, line segmentation, recognition
│   ├── spell.py            Offline spelling correction
│   ├── offline_format.py   Rule-based organiser (headings, sections, bullets)
│   ├── exporter.py         Markdown → DOCX / PDF / TXT / MD
│   ├── storage.py          Document history (only on explicit Save)
│   └── main.py             FastAPI app: pages, REST API, WebSocket
├── web/
│   ├── index.html          Phone UI (8 screens)
│   ├── app.js              Connection, router, touchpad, canvas, workflow
│   ├── markdown.js         Safe Markdown preview renderer
│   ├── styles.css          Mobile-first styles, light/dark
│   ├── desktop.html        Computer status page (QR, devices, system)
│   ├── manifest.webmanifest, sw.js, icon.svg
├── tests/
│   ├── test_app.py         26 automated tests (API, WebSocket, AI mocked, exports)
│   └── live_touchpad_check.py  Moves the real cursor a little and back via WebSocket
├── models/                 Downloaded handwriting model (created on first start)
├── data/                   Created at runtime: saved documents, pairing code, HTTPS certs
└── exports/                Files saved "on the computer"
```

---

## 4. Using it

### Touchpad
| Gesture | Action |
|---|---|
| 1 finger move | Move cursor (with acceleration) |
| Tap | Left click (tap twice for double-click) |
| 2-finger tap / long press | Right click |
| 3-finger tap | Middle click |
| 2-finger move / right-edge strip | Scroll (vertical + horizontal) |
| Double-tap and hold, then move | Drag and drop |
| **Left / Right** buttons | Press-and-hold buttons (hold Left + move = drag) |
| ⌨ | Type text and send keys (Enter, Backspace, arrows…) to the computer |

You can adjust pointer speed, acceleration, scroll speed, natural scrolling, tap-to-click and vibration under **Settings**.

### Handwriting → document
1. **Handwriting Canvas**: write with a finger or stylus. Stylus pressure changes line width. The canvas has pen thickness, 4 colours, eraser, undo/redo, clear, and save as PNG.
2. **Convert to text**: pick English or Urdu, then tap Convert. The recognized text opens for review.
3. **Review**: correct anything that was misread. *Write more* clears the canvas and appends the next page.
4. Choose a **style**: Simple Text, Professional Notes, Academic Notes, Assignment, Report, Meeting Notes, or Structured Outline. Then tap **Organize**.
5. **Preview / Edit** the result, then:
   - **Export** to Word, PDF, TXT or Markdown, downloaded on the phone or saved to `exports/` on the computer.
   - **Save to history**, **Type on computer** (types into the focused window, e.g. Word), or **Copy**.

Example, Academic Notes style:

```
input : computer is an electronic machine it accepts data process data and produces information it has different components hardware and software

output: # COMPUTER
        ## 1. Introduction
        A computer is an electronic machine that accepts data, processes it, and produces information.
        ## 2. Main Components
        - Hardware
        - Software
```

The local organizer only re-segments, capitalizes, punctuates and spell-corrects your own words, so it can't invent content. A toast lists every spelling it changed. It doesn't rewrite grammar (for example "process data" → "processes it"); that needs the optional Claude engine.

---

## 5. Security and privacy

- **Pairing.** Each start creates a random token (or uses `SMARTPAD_TOKEN`). It appears only on the computer (terminal plus `/desktop`, which refuses non-localhost requests). The QR code carries it in the URL *fragment*, which browsers never send to servers. Every API call and WebSocket must present it, and other devices on the Wi-Fi get `401`. **New pairing code** on the status page rotates the token and disconnects all phones.
- **Encryption.** `python run.py --https` serves HTTPS/WSS with an auto-generated self-signed certificate. The phone shows a one-time warning: tap *Advanced → Proceed*. Plain HTTP is the default because self-signed warnings confuse first-time users, and traffic stays on your LAN.
- **Storage.** Nothing is saved unless you tap *Save*. Canvas drafts are kept on the phone only if you enable that in Settings, and the handwriting image is stored with a document only if you enable that too.
- **API keys** live in `.env` on the computer and never reach the phone or the frontend code.
- **Local processing (default).** Recognition, correction and formatting all run on your computer. The only internet access is the one-time model download from Hugging Face. Only if you add an Anthropic key are images and text sent to that API.

---

## 6. Testing

```
.venv\Scripts\python -m pytest -q             # 26 tests, never moves the real mouse
.venv\Scripts\python tests\live_touchpad_check.py   # with run.py running: moves cursor 40px and back
.venv\Scripts\python tests\ocr_sample_check.py     # local handwriting model accuracy check
```

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| Phone can't open the page | Same Wi-Fi? Allow Python in Windows Firewall (Private). Try the other IP in the status page's drop-down. Guest or office networks often block device-to-device traffic. |
| "Pairing expired" | The server restarted with a new token. Scan the QR again, or set a fixed `SMARTPAD_TOKEN` in `.env`. |
| Cursor doesn't move | Check "Mouse control" on the status page. On macOS, grant Accessibility permission to Terminal/Python. On Linux, use an X11 session. |
| First Convert is slow | The model is still downloading or loading. Watch the "Recognition" line on the status page. After that, a 5-line page takes about 3 s on a laptop CPU. |
| Want higher accuracy on messy writing | Set `SMARTPAD_OCR_MODEL=microsoft/trocr-base-handwritten` in `.env`. It's a ~1.3 GB download and about 5x slower. |
| Words misread | Write larger, on the ruled lines, and leave a clear gap between lines. Correct the text in the review screen before organizing. |
| "Urdu handwriting can't be recognised offline" | The local model reads English only. Type the Urdu text on the review screen (organizer and Word export support it), or install Tesseract with the `urd` pack. |
| Urdu in PDF looks disjointed | ReportLab doesn't shape Arabic-script text. Export Urdu documents to **Word**, which renders them correctly. |

## 8. Known limitations
- The local model recognizes **English** handwriting only, and not math notation. Urdu needs Tesseract or the optional Claude engine.
- Recognition is good for clear handwriting but not perfect, so always check the review screen.
- The local organizer is rule-based. It fixes spelling and structure but doesn't rewrite grammar.
- Browsers can't do mDNS discovery, so pairing uses the QR code or a typed address plus pairing code.
- Service-worker offline caching only works over `--https`.

