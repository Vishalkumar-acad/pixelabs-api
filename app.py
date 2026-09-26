import io
import os
import shutil
import subprocess
import tempfile
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Response
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps

try:  # HEIC support (iPhone photos) — optional
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_OK = True
except Exception:
    HEIC_OK = False

from pypdf import PdfReader, PdfWriter

MAX_BYTES = 50 * 1024 * 1024  # 50 MB per file
MAX_FILES = 20
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

@app.get("/")
def root():
    return {"ok": True, "service": "pixelabs-tools", "heic": HEIC_OK, "docs": "/docs"}


@app.get("/health")
def health():
    return {"ok": True}


def hdr(orig_len: int, out: bytes, kept: bool, extra=None):
    h = {
        "X-Original-Size": str(orig_len),
        "X-Result-Size": str(len(out)),
        "X-Kept": "1" if kept else "0",
        "Cache-Control": "no-store",
    }
    if extra:
        h.update(extra)
    return h

# ---------------- PDF: compress (Ghostscript) ----------------

@app.post("/compress")
async def compress_pdf(file: UploadFile = File(...), level: str = Form("medium")):
    data = await file.read()
    check_file(file, data)
    lvl = PDF_LEVELS.get(level, PDF_LEVELS["medium"])

    tmpdir = tempfile.mkdtemp(prefix="pat_")
    src = os.path.join(tmpdir, "in.pdf")
    out = os.path.join(tmpdir, "out.pdf")
    try:
        with open(src, "wb") as f:
            f.write(data)
        kept = True
        try:
            rc = subprocess.run(
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
            if rc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) < len(data):
                kept = False
        except (subprocess.TimeoutExpired, FileNotFoundError):
            kept = True

        with open(src if kept else out, "rb") as f:
            out_bytes = f.read()
        return Response(
            content=out_bytes,
            media_type="application/pdf",
            headers=hdr(len(data), out_bytes, kept),
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

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

@app.post("/image/compress")
async def compress_image(
    file: UploadFile = File(...),
    level: str = Form("medium"),
    target_kb: int = Form(0),
):
    data = await file.read()
    check_file(file, data)
    img = load_image(data)
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
            if len(save_image(cur, "jpg", FLOOR)) <= target:
                best = save_image(cur, "jpg", FLOOR)
                lo, hi = FLOOR, 95
                for _ in range(7):
                    mid = (lo + hi) // 2
                    out = save_image(cur, "jpg", mid)
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
            cur = img.resize((w, h), Image.LANCZOS)
            did_resize = True
        if best is None:
            # already at the readability limit — fall back to quality search
            lo, hi = 5, 95
            for _ in range(7):
                mid = (lo + hi) // 2
                out = save_image(cur, "jpg", mid)
                if len(out) <= target:
                    best = out
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best is None:
                best = save_image(cur, "jpg", 5)
        out_bytes = best
        kept = len(out_bytes) >= len(data)
        if kept:
            out_bytes = data
    else:
        q = IMG_QUALITY.get(level, IMG_QUALITY["medium"])
        out_bytes = save_image(img, "jpg", q)
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

@app.post("/image/convert")
async def convert_image(file: UploadFile = File(...), format: str = Form("jpg")):
    data = await file.read()
    check_file(file, data)
    fmt = (format or "jpg").lower()
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in ("jpg", "png", "webp"):
        raise HTTPException(400, "supported formats: jpg, png, webp")

    img = load_image(data)
    out_bytes = save_image(img, fmt, 90)
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[fmt]
    return Response(
        content=out_bytes,
        media_type=mime,
        headers=hdr(len(data), out_bytes, False, {"X-Format": fmt}),
    )


# ---------------- Image: resize ----------------

@app.post("/image/resize")
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

    img = load_image(data)
    w, h = img.size
    if width > 0 and height > 0:
        nw, nh = width, height
    elif width > 0:
        nw = width
        nh = max(1, round(h * width / w))
    else:
        nh = height
        nw = max(1, round(w * height / h))

    img = img.resize((nw, nh), Image.LANCZOS)

    fmt = (format or "").lower()
    if fmt not in ("jpg", "png", "webp"):
        fmt = "png" if img.mode in ("RGBA", "LA", "P") else "jpg"
    out_bytes = save_image(img, fmt, 90)
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[fmt]
    return Response(
        content=out_bytes,
        media_type=mime,
        headers=hdr(len(data), out_bytes, False, {"X-Format": fmt}),
    )


# ---------------- PDF: merge ----------------

@app.post("/pdf/merge")
async def merge_pdfs(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "no files provided")
    if len(files) > MAX_FILES:
        raise HTTPException(400, "too many files (max 20)")

    datas = []
    for f in files:
        d = await f.read()
        check_file(f, d)
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

@app.post("/pdf/split")
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


@app.post("/pdf/from-images")
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

    for f in files:
        d = await f.read()
        check_file(f, d)
        img = load_image(d)
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
            img = img.resize((nw, nh), Image.LANCZOS)

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
