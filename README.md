# PixelAbs Tools API

Server-side processing API for [tools.pixelabs.in](https://tools.pixelabs.in).
Users choose **Local (private, in-browser)** or **Cloud** — this is the cloud
side. Deployed on Render.com (free tier).

## Endpoints

- `GET /health` — uptime check
- `POST /compress` — PDF compression (Ghostscript). Form: `file`, `level`
  (`high`/`medium`/`low`). Headers: `X-Original-Size`, `X-Result-Size`, `X-Kept`.
- `POST /image/compress` — image compression. Form: `file`, `level`, optional
  `target_kb` (binary-search JPEG quality to hit a size).
- `POST /image/convert` — form: `file`, `format` (`jpg`/`png`/`webp`). Decodes
  JPG, PNG, WEBP, TIFF, BMP, GIF and HEIC (iPhone photos).
- `POST /image/resize` — form: `file`, `width` and/or `height`, optional `format`.
- `POST /pdf/merge` — form: multiple `files` fields. Returns merged PDF.
- `POST /pdf/split` — form: `file`, `pages` (`all` or `1-3,5`).
- `POST /pdf/from-images` — form: multiple `files`, `page_size`
  (`a4`/`letter`/`fit`), `fit` (`contain`/`cover`).

Limits: 50 MB per file, 20 files per request. Files are processed in memory
and temporary directories are deleted immediately — nothing is stored or
logged.

## Deploy

Connect this repo on [render.com](https://render.com) as a Blueprint
(`render.yaml` is included) — a free web service is created automatically.
