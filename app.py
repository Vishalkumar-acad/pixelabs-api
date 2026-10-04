import asyncio
import io
import re
import os
import shutil
import subprocess
import tempfile
import time
import zipfile

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from PIL import Image, ImageOps

try:  # HEIC support (iPhone photos) — optional
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_OK = True
except Exception:
    HEIC_OK = False

from pypdf import PdfReader, PdfWriter

STARTED_AT = time.time()  # lets /health show how long this build has been up

MAX_BYTES = 50 * 1024 * 1024  # 50 MB per file
MAX_TOTAL_BYTES = 150 * 1024 * 1024  # per request, across all files
MAX_FILES = 20
# PDFs are streamed to disk and back, never held in memory, so /compress can
# take a bigger file than the in-memory image/audio routes. Kept under
# Cloudflare's 100 MB request-body cap.
MAX_PDF_BYTES = 90 * 1024 * 1024  # 90 MB, PDF compression only

# This box has ~0.9 GiB of RAM. A PDF through Ghostscript, a large image
# through PIL, or an ffmpeg run can each take a sizeable bite of it, and two
# at once is how you get an OOM kill — which takes the API down for everyone.
# So every memory-hungry route holds this one slot: heavy jobs run one at a
# time. Light routes (/health, /) never touch it, so they always answer, no
# matter how busy the box is.
HEAVY = asyncio.Semaphore(1)


async def heavy_slot():
    """Dependency: hold the single heavy-work slot for the whole request."""
    async with HEAVY:
        yield

GS_BIN = os.environ.get("GS_BIN", "gs")
TIMEOUT_SECS = 120

PDF_LEVELS = {"high": "/printer", "medium": "/ebook", "low": "/screen"}
IMG_QUALITY = {"high": 85, "medium": 72, "low": 55}

app = FastAPI(title="PixelAbs Tools API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # stateless, no cookies/credentials involved
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=[
        "X-Original-Size", "X-Result-Size", "X-Kept",
        "X-Format", "X-Page-Count", "X-Heic", "X-Resized",
    ],
)

def check_file(upload: UploadFile, data: bytes):
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "file too large (max 50 MB)")


def check_total(total: int):
    """Guard the whole request, not just each file: 20 x 50 MB would not
    fit in this box's memory, and an OOM kill takes the API down for
    everyone. Reject early, with a clear message, instead."""
    if total > MAX_TOTAL_BYTES:
        raise HTTPException(413, "too much data in one request (max 150 MB in total)")

@app.get("/")
def root():
    return {"ok": True, "service": "pixelabs-tools", "heic": HEIC_OK, "docs": "/docs"}


@app.get("/health")
def health():
    """Liveness plus a look at the binaries the cloud tools need.

    Stays 200 while the process is serving (the uptime monitor and the
    deploy workflow depend on that), but names every dependency so a
    broken image is visible without opening a shell."""
    deps = {
        "ghostscript": bool(shutil.which(GS_BIN)),
        "ffmpeg": bool(shutil.which(FFMPEG)),
        "heic": HEIC_OK,
    }
    return {
        "ok": True,
        "service": "pixelabs-tools",
        # The commit this container was built from — the deploy script passes it
        # in, so "which version is live?" is answerable from outside in one call.
        "commit": os.environ.get("GIT_SHA", "unknown"),
        # Seconds since this container started — a fresh (small) number means a
        # deploy just happened; a huge one means it has been running for ages.
        "uptime_s": int(time.time() - STARTED_AT),
        "deps": deps,
        "degraded": not all(deps.values()),
    }


def hdr(orig_len: int, out, kept: bool, extra=None):
    rlen = out if isinstance(out, int) else len(out)
    h = {
        "X-Original-Size": str(orig_len),
        "X-Result-Size": str(rlen),
        "X-Kept": "1" if kept else "0",
        "Cache-Control": "no-store",
    }
    if extra:
        h.update(extra)
    return h

# ---------------- PDF: compress (Ghostscript) ----------------

@app.post("/compress", dependencies=[Depends(heavy_slot)])
async def compress_pdf(file: UploadFile = File(...), level: str = Form("medium")):
    """Ghostscript. Unlike the image/audio routes this one never holds the
    upload in memory: the file is streamed to disk, compressed there, and
    streamed back — so a PDF may be much larger than the in-memory limit
    (MAX_BYTES) allows. See MAX_PDF_BYTES."""
    size = file.size or 0
    if size == 0:
        raise HTTPException(400, "empty file")
    if size > MAX_PDF_BYTES:
        raise HTTPException(413, "file too large (max 90 MB)")
    lvl = PDF_LEVELS.get(level, PDF_LEVELS["medium"])

    tmpdir = tempfile.mkdtemp(prefix="pat_")
    src = os.path.join(tmpdir, "in.pdf")
    out = os.path.join(tmpdir, "out.pdf")
    try:
        # stream the upload to disk in chunks — never file.read() the whole PDF
        with open(src, "wb") as f:
            shutil.copyfileobj(file.file, f, 1024 * 1024)
        in_len = os.path.getsize(src)
        if in_len > MAX_PDF_BYTES:
            raise HTTPException(413, "file too large (max 90 MB)")

        kept = True
        try:
            # Off the event loop, so /health keeps answering while gs works.
            # The heavy_slot dependency already guarantees this is the only
            # heavy job running.
            rc = await asyncio.to_thread(
                subprocess.run,
                [
                    GS_BIN, "-sDEVICE=pdfwrite",
                    "-dCompatibilityLevel=1.4",
                    "-dPDFSETTINGS=" + lvl,
                    "-dNOPAUSE", "-dQUIET", "-dBATCH",
                    "-sOutputFile=" + out, src,
                ],
                timeout=TIMEOUT_SECS,
                capture_output=True,
            )
            if rc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) < in_len:
                kept = False
        except (subprocess.TimeoutExpired, FileNotFoundError):
            kept = True

        result = src if kept else out
        headers = hdr(in_len, os.path.getsize(result), kept)
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    def stream_and_clean():
        try:
            with open(result, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return StreamingResponse(
        stream_and_clean(), media_type="application/pdf", headers=headers
    )

# ---------------- Image helpers ----------------

def load_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        # Apply EXIF orientation so phone photos are always upright.
        # Remember the original format first: exif_transpose() drops it.
        fmt = img.format
        img = ImageOps.exif_transpose(img)
        img.format = fmt
        return img
    except Exception:
        raise HTTPException(400, "unsupported or corrupted image file")


def save_image(img: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if fmt in ("jpg", "jpeg"):
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    elif fmt == "png":
        img.save(buf, format="PNG", optimize=True)
    elif fmt == "webp":
        img.save(buf, format="WEBP", quality=quality, method=6)
    else:
        raise HTTPException(400, "unsupported target format")
    return buf.getvalue()

# ---------------- Image: compress ----------------

@app.post("/image/compress", dependencies=[Depends(heavy_slot)])
async def compress_image(
    file: UploadFile = File(...),
    level: str = Form("medium"),
    target_kb: int = Form(0),
):
    data = await file.read()
    check_file(file, data)
    img = await asyncio.to_thread(load_image, data)
    did_resize = False

    if target_kb and target_kb > 0:
        # Smart target-size search. Crushing JPEG quality alone to hit a
        # small size looks terrible (blocky text, washed-out colors), so
        # never go below FLOOR quality while the image can still be made
        # smaller: shrink 15% at a time first. A slightly smaller image
        # at good quality always beats a full-size image at awful quality.
        target = target_kb * 1024
        FLOOR = 45
        MIN_EDGE = 500  # keep text on documents readable
        w, h = img.size
        cur = img
        best = None
        while True:
            if len(await asyncio.to_thread(save_image, cur, "jpg", FLOOR)) <= target:
                best = await asyncio.to_thread(save_image, cur, "jpg", FLOOR)
                lo, hi = FLOOR, 95
                for _ in range(7):
                    mid = (lo + hi) // 2
                    out = await asyncio.to_thread(save_image, cur, "jpg", mid)
                    if len(out) <= target:
                        best = out
                        lo = mid + 1
                    else:
                        hi = mid - 1
                break
            if w <= MIN_EDGE or h <= MIN_EDGE:
                break
            w = max(1, round(w * 0.85))
            h = max(1, round(h * 0.85))
            cur = await asyncio.to_thread(img.resize, (w, h), Image.LANCZOS)
            did_resize = True
        if best is None:
            # already at the readability limit — fall back to quality search
            lo, hi = 5, 95
            for _ in range(7):
                mid = (lo + hi) // 2
                out = await asyncio.to_thread(save_image, cur, "jpg", mid)
                if len(out) <= target:
                    best = out
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best is None:
                best = await asyncio.to_thread(save_image, cur, "jpg", 5)
        out_bytes = best
        kept = len(out_bytes) >= len(data)
        if kept:
            out_bytes = data
    else:
        q = IMG_QUALITY.get(level, IMG_QUALITY["medium"])
        out_bytes = await asyncio.to_thread(save_image, img, "jpg", q)
        if len(out_bytes) >= len(data):
            out_bytes = data
            kept = True
        else:
            kept = False

    if kept:
        mime = ("image/" + (img.format or "jpeg").lower()).replace("image/jpeg", "image/jpeg")
        if img.format == "JPG":
            mime = "image/jpeg"
    else:
        mime = "image/jpeg"

    return Response(
        content=out_bytes,
        media_type=mime,
        headers=hdr(len(data), out_bytes, kept, {"X-Resized": "1" if did_resize else "0"}),
    )


# ---------------- Image: convert ----------------

@app.post("/image/convert", dependencies=[Depends(heavy_slot)])
async def convert_image(file: UploadFile = File(...), format: str = Form("jpg")):
    data = await file.read()
    check_file(file, data)
    fmt = (format or "jpg").lower()
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in ("jpg", "png", "webp"):
        raise HTTPException(400, "supported formats: jpg, png, webp")

    img = await asyncio.to_thread(load_image, data)
    out_bytes = await asyncio.to_thread(save_image, img, fmt, 90)
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[fmt]
    return Response(
        content=out_bytes,
        media_type=mime,
        headers=hdr(len(data), out_bytes, False, {"X-Format": fmt}),
    )


# ---------------- Image: resize ----------------

@app.post("/image/resize", dependencies=[Depends(heavy_slot)])
async def resize_image(
    file: UploadFile = File(...),
    width: int = Form(0),
    height: int = Form(0),
    format: str = Form(""),
):
    data = await file.read()
    check_file(file, data)
    if width <= 0 and height <= 0:
        raise HTTPException(400, "width or height required")

    img = await asyncio.to_thread(load_image, data)
    w, h = img.size
    if width > 0 and height > 0:
        nw, nh = width, height
    elif width > 0:
        nw = width
        nh = max(1, round(h * width / w))
    else:
        nh = height
        nw = max(1, round(w * height / h))

    img = await asyncio.to_thread(img.resize, (nw, nh), Image.LANCZOS)

    fmt = (format or "").lower()
    if fmt not in ("jpg", "png", "webp"):
        fmt = "png" if img.mode in ("RGBA", "LA", "P") else "jpg"
    out_bytes = await asyncio.to_thread(save_image, img, fmt, 90)
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[fmt]
    return Response(
        content=out_bytes,
        media_type=mime,
        headers=hdr(len(data), out_bytes, False, {"X-Format": fmt}),
    )


# ---------------- PDF: merge ----------------

@app.post("/pdf/merge", dependencies=[Depends(heavy_slot)])
async def merge_pdfs(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "no files provided")
    if len(files) > MAX_FILES:
        raise HTTPException(400, "too many files (max 20)")

    datas = []
    total = 0
    for f in files:
        d = await f.read()
        check_file(f, d)
        total += len(d)
        check_total(total)
        datas.append(d)

    writer = PdfWriter()
    total_in = 0
    for d in datas:
        total_in += len(d)
        try:
            reader = PdfReader(io.BytesIO(d))
            for page in reader.pages:
                writer.add_page(page)
        except Exception:
            raise HTTPException(400, "invalid PDF in upload")

    buf = io.BytesIO()
    writer.write(buf)
    out_bytes = buf.getvalue()
    return Response(
        content=out_bytes,
        media_type="application/pdf",
        headers=hdr(total_in, out_bytes, False, {"X-Page-Count": str(len(writer.pages))}),
    )


# ---------------- PDF: split / extract ----------------

def parse_pages(spec: str, total: int):
    """'all' or '1-3,5' (1-based, inclusive) -> list of 0-based indexes"""
    spec = (spec or "all").strip().lower()
    if spec in ("all", ""):
        return list(range(total))
    idx = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            a, b = int(a), int(b)
            if a < 1 or b > total or a > b:
                raise HTTPException(400, "page range out of bounds")
            idx.extend(range(a - 1, b))
        else:
            p = int(part)
            if p < 1 or p > total:
                raise HTTPException(400, "page out of bounds")
            idx.append(p - 1)
    return idx

@app.post("/pdf/split", dependencies=[Depends(heavy_slot)])
async def split_pdf(file: UploadFile = File(...), pages: str = Form("all")):
    data = await file.read()
    check_file(file, data)
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "invalid PDF")

    idx = parse_pages(pages, len(reader.pages))
    if not idx:
        raise HTTPException(400, "no pages selected")

    writer = PdfWriter()
    for i in idx:
        writer.add_page(reader.pages[i])

    buf = io.BytesIO()
    writer.write(buf)
    out_bytes = buf.getvalue()
    return Response(
        content=out_bytes,
        media_type="application/pdf",
        headers=hdr(len(data), out_bytes, False, {"X-Page-Count": str(len(idx))}),
    )


# ---------------- PDF: images to PDF ----------------

PAGE_SIZES = {"a4": (595, 842), "letter": (612, 792)}


@app.post("/pdf/from-images", dependencies=[Depends(heavy_slot)])
async def images_to_pdf(
    files: list[UploadFile] = File(...),
    page_size: str = Form("a4"),
    fit: str = Form("contain"),
):
    if not files:
        raise HTTPException(400, "no files provided")
    if len(files) > MAX_FILES:
        raise HTTPException(400, "too many files (max 20)")

    writer = PdfWriter()
    total = 0

    for f in files:
        d = await f.read()
        check_file(f, d)
        total += len(d)
        check_total(total)
        img = await asyncio.to_thread(load_image, d)
        if img.mode != "RGB":
            img = img.convert("RGB")

        iw, ih = img.size

        if page_size == "fit":
            pw, ph = iw, ih
            nw, nh = iw, ih
        else:
            pw, ph = PAGE_SIZES.get((page_size or "a4").lower(), PAGE_SIZES["a4"])
            if fit == "cover":
                scale = max(pw / iw, ph / ih)
            else:  # contain
                scale = min(pw / iw, ph / ih)
            nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
            img = await asyncio.to_thread(img.resize, (nw, nh), Image.LANCZOS)

        # white page canvas, image centered
        canvas = Image.new("RGB", (pw, ph), (255, 255, 255))
        if (pw, ph) == (nw, nh) and page_size == "fit":
            canvas = img
        else:
            canvas.paste(img, ((pw - nw) // 2, (ph - nh) // 2))

        single = io.BytesIO()
        # 72 dpi => 1 px == 1 pt, page size equals canvas size
        canvas.save(single, format="PDF", resolution=72.0)
        single.seek(0)
        sub = PdfReader(single)
        writer.add_page(sub.pages[0])

    buf = io.BytesIO()
    writer.write(buf)
    out_bytes = buf.getvalue()
    return Response(
        content=out_bytes,
        media_type="application/pdf",
        headers=hdr(0, out_bytes, False, {"X-Page-Count": str(len(writer.pages))}),
    )

# ---------------- PDF: pages to images ----------------

RENDER_DPI = {"high": 200, "medium": 150, "low": 100}
RENDER_QUALITY = {"high": 90, "medium": 80, "low": 65}
MAX_RENDER_PAGES = 300


def base_name(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name or "document"))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "document"
    return stem[:60]


@app.post("/pdf/to-images", dependencies=[Depends(heavy_slot)])
async def pdf_to_images(
    file: UploadFile = File(...),
    format: str = Form("jpg"),
    level: str = Form("medium"),
    pages: str = Form("all"),
):
    """Ghostscript renders each page to a JPEG/PNG and the images come back as
    a ZIP. Both the upload and the ZIP go via disk, so this stays well under
    the memory the in-memory image routes need."""
    size = file.size or 0
    if size == 0:
        raise HTTPException(400, "empty file")
    if size > MAX_PDF_BYTES:
        raise HTTPException(413, "file too large (max 90 MB)")

    fmt = "png" if (format or "").lower() == "png" else "jpg"
    ext = fmt
    dpi = RENDER_DPI.get(level, RENDER_DPI["medium"])
    quality = RENDER_QUALITY.get(level, RENDER_QUALITY["medium"])

    tmpdir = tempfile.mkdtemp(prefix="pat_")
    src = os.path.join(tmpdir, "in.pdf")
    try:
        with open(src, "wb") as f:
            shutil.copyfileobj(file.file, f, 1024 * 1024)
        in_len = os.path.getsize(src)
        if in_len > MAX_PDF_BYTES:
            raise HTTPException(413, "file too large (max 90 MB)")

        try:
            reader = PdfReader(src)
            total_pages = len(reader.pages)
        except Exception:
            raise HTTPException(400, "invalid PDF")

        spec = (pages or "all").strip().lower()
        wanted = None
        if spec in ("", "all"):
            if total_pages > MAX_RENDER_PAGES:
                raise HTTPException(
                    413, "too many pages (max %d)" % MAX_RENDER_PAGES
                )
        else:
            wanted = list(dict.fromkeys(parse_pages(spec, total_pages)))
            if not wanted:
                raise HTTPException(400, "no pages selected")
            writer = PdfWriter()
            for i in wanted:
                writer.add_page(reader.pages[i])
            sub = os.path.join(tmpdir, "sel.pdf")
            with open(sub, "wb") as f:
                writer.write(f)
            src = sub

        device = "png16m" if fmt == "png" else "jpeg"
        out_pat = os.path.join(tmpdir, "p-%04d." + ext)
        args = [
            GS_BIN, "-dNOPAUSE", "-dBATCH", "-dQUIET",
            "-sDEVICE=" + device, "-r" + str(dpi),
        ]
        if device == "jpeg":
            args.append("-dJPEGQ=" + str(quality))
        args += ["-sOutputFile=" + out_pat, src]
        await asyncio.to_thread(
            subprocess.run, args, timeout=TIMEOUT_SECS, capture_output=True
        )

        rendered = sorted(
            os.path.join(tmpdir, n)
            for n in os.listdir(tmpdir)
            if n.startswith("p-") and n.endswith("." + ext)
        )
        if not rendered:
            raise HTTPException(
                422, "could not render any pages — is this a valid, unlocked PDF?"
            )

        base = base_name(file.filename)
        zip_path = os.path.join(tmpdir, "images.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for n, path in enumerate(rendered):
                label = (wanted[n] + 1) if wanted else (n + 1)
                z.write(path, "%s-page-%03d.%s" % (base, label, ext))
        out_size = os.path.getsize(zip_path)
        headers = {
            "X-Page-Count": str(len(rendered)),
            "X-Result-Size": str(out_size),
            "Cache-Control": "no-store",
        }
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    def stream_and_clean():
        try:
            with open(zip_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return StreamingResponse(
        stream_and_clean(), media_type="application/zip", headers=headers
    )


# ---------------- Media: audio & video (ffmpeg) ----------------

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".wma", ".amr", ".aiff"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".ts"}
MP3_MIME = "audio/mpeg"


def _ext(name: str, allowed: set) -> str:
    e = os.path.splitext((name or "").lower())[1]
    if e not in allowed:
        raise HTTPException(400, f"unsupported file type '{e or '(none)'}'")
    return e


async def run_ffmpeg(args, timeout=TIMEOUT_SECS):
    try:
        p = await asyncio.to_thread(
            subprocess.run, [FFMPEG] + args, capture_output=True, timeout=timeout
        )
    except FileNotFoundError:
        raise HTTPException(503, "ffmpeg not installed")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "processing timed out — try a shorter clip")
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "ignore")[-300:]
        raise HTTPException(400, "ffmpeg failed: " + tail.replace("\n", " "))
    return p.stdout


async def probe_sample_rate(path: str) -> int:
    try:
        p = await asyncio.to_thread(
            subprocess.run, [FFMPEG, "-i", path], capture_output=True, timeout=30
        )
        text = p.stderr.decode("utf-8", "ignore")
        m = re.search(r"(\d{4,6}) Hz", text)
        return int(m.group(1)) if m else 44100
    except Exception:
        return 44100


def mp3_response(out: bytes, orig_len: int, extra=None):
    h = {
        "X-Original-Size": str(orig_len),
        "X-Result-Size": str(len(out)),
        "X-Kept": "0",
        "Cache-Control": "no-store",
    }
    if extra:
        h.update(extra)
    return Response(content=out, media_type=MP3_MIME, headers=h)


@app.post("/audio/trim", dependencies=[Depends(heavy_slot)])
async def audio_trim(
    file: UploadFile = File(...),
    start: float = Form(0),
    end: float = Form(-1),
    format: str = Form("mp3"),
):
    data = await file.read()
    check_file(file, data)
    _ext(file.filename, AUDIO_EXT)
    fmt = "mp3" if format != "wav" else "wav"
    start = max(0.0, float(start))
    if end < 0 or end <= start:
        raise HTTPException(400, "end must be greater than start")
    if end - start > 1800:
        raise HTTPException(400, "clip too long (max 30 minutes)")
    duration = min(end - start, 1800)

    with tempfile.NamedTemporaryFile(suffix=_ext(file.filename, AUDIO_EXT), delete=False) as t:
        t.write(data)
        path = t.name
    try:
        if fmt == "wav":
            out = await run_ffmpeg([
                "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                "-i", path, "-c:a", "pcm_s16le", "-f", "wav", "-",
            ])
            return Response(
                content=out, media_type="audio/wav",
                headers={"X-Original-Size": str(len(data)), "X-Result-Size": str(len(out)),
                         "X-Kept": "0", "X-Format": "wav", "Cache-Control": "no-store"})
        out = await run_ffmpeg([
            "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", path, "-c:a", "libmp3lame", "-b:a", "160k", "-f", "mp3", "-",
        ])
        return mp3_response(out, len(data), {"X-Format": "mp3"})
    finally:
        os.unlink(path)


@app.post("/audio/speed", dependencies=[Depends(heavy_slot)])
async def audio_speed(
    file: UploadFile = File(...),
    factor: float = Form(1.0),
    mode: str = Form("speed"),
    format: str = Form("mp3"),
):
    data = await file.read()
    check_file(file, data)
    _ext(file.filename, AUDIO_EXT)
    factor = float(factor)
    if factor < 0.25 or factor > 4.0:
        raise HTTPException(400, "factor must be between 0.25 and 4")

    with tempfile.NamedTemporaryFile(suffix=_ext(file.filename, AUDIO_EXT), delete=False) as t:
        t.write(data)
        path = t.name
    try:
        sr = await probe_sample_rate(path)
        # tape style: asetrate changes pitch+speed together (cassette player)
        out = await run_ffmpeg([
            "-hide_banner", "-loglevel", "error",
            "-i", path,
            "-af", f"asetrate={int(sr * factor)},aresample={sr}",
            "-c:a", "libmp3lame", "-b:a", "160k", "-f", "mp3", "-",
        ])
        return mp3_response(out, len(data), {"X-Format": "mp3"})
    finally:
        os.unlink(path)


@app.post("/audio/join", dependencies=[Depends(heavy_slot)])
async def audio_join(
    files: list[UploadFile] = File(...),
    gap: float = Form(0),
):
    if not files:
        raise HTTPException(400, "no files provided")
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"too many files (max {MAX_FILES})")
    gap = max(0.0, min(float(gap), 10.0))

    total_in = 0
    tmpdir = tempfile.mkdtemp()
    try:
        parts = []
        for i, f in enumerate(files):
            d = await f.read()
            check_file(f, d)
            total_in += len(d)
            check_total(total_in)
            ext = _ext(f.filename, AUDIO_EXT)
            src = os.path.join(tmpdir, f"in{i}{ext}")
            with open(src, "wb") as fh:
                fh.write(d)
            part = os.path.join(tmpdir, f"p{i}.wav")
            await run_ffmpeg(["-hide_banner", "-loglevel", "error", "-i", src,
                        "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", part])
            parts.append(part)
            if gap > 0 and i < len(files) - 1:
                sil = os.path.join(tmpdir, f"s{i}.wav")
                await run_ffmpeg(["-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
                            "-t", f"{gap:.2f}", "-c:a", "pcm_s16le", sil])
                parts.append(sil)

        with open(os.path.join(tmpdir, "list.txt"), "w") as lf:
            for p in parts:
                lf.write(f"file '{p}'\n")

        out = await run_ffmpeg([
            "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", os.path.join(tmpdir, "list.txt"),
            "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "-",
        ])
        return mp3_response(out, total_in, {"X-Format": "mp3"})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/video/gif", dependencies=[Depends(heavy_slot)])
async def video_gif(
    file: UploadFile = File(...),
    start: float = Form(0),
    end: float = Form(-1),
    width: int = Form(320),
    fps: int = Form(10),
):
    data = await file.read()
    check_file(file, data)
    _ext(file.filename, VIDEO_EXT)
    start = max(0.0, float(start))
    if end < 0 or end <= start:
        raise HTTPException(400, "end must be greater than start")
    duration = min(end - start, 30.0)
    width = max(120, min(int(width), 1280))
    fps = max(3, min(int(fps), 20))

    with tempfile.NamedTemporaryFile(suffix=_ext(file.filename, VIDEO_EXT), delete=False) as t:
        t.write(data)
        path = t.name
    try:
        vf = (f"fps={fps},scale={width}:-2:flags=lanczos,"
              "split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer")
        out = await run_ffmpeg([
            "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", path,
            "-vf", vf, "-loop", "0", "-f", "gif", "-",
        ])
        return Response(
            content=out, media_type="image/gif",
            headers={"X-Original-Size": str(len(data)), "X-Result-Size": str(len(out)),
                     "X-Kept": "0", "X-Format": "gif", "Cache-Control": "no-store"},
        )
    finally:
        os.unlink(path)
