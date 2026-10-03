# PixelAbs Tools API

Server-side processing API for [tools.pixelabs.in](https://tools.pixelabs.in).
Users choose **Local (private, in-browser)** or **Cloud** — this is the cloud side.

## Where it runs

Self-hosted on an Azure VM (`backend-vm`, Ubuntu 24.04) — no managed platform
any more. The request path, in order:

```
visitor
  → Cloudflare (TLS for tools.pixelabs.in)
  → Cloudflare Worker  (/api/* proxied to the backend)
  → Caddy on the VM    (api.pixelabs.in, Let's Encrypt cert, Full-strict TLS)
  → 127.0.0.1:10000    (this container, loopback only)
```

**Keep this shape when changing anything deployment-related:**

- The container is published on the **loopback interface only**
  (`-p 127.0.0.1:10000:10000`). Caddy owns the public ports (80/443) — binding
  port 80 in the container fails with *address already in use*.
- `/etc/caddy/Caddyfile` points the upstream at **`localhost:10000`**, never at a
  container IP: Docker assigns a new IP every time the container is recreated,
  which happens on every deploy (a hardcoded `172.17.x.x` gives 502s).
- `portainer.pixelabs.in` sits behind Cloudflare Access (Zero Trust, admin-only).
- The VM has 2 GB of swap (`/swapfile`, persisted in `/etc/fstab`) so a burst of
  big jobs cannot OOM-kill the API.

## Deploy

Every push to `main` runs `.github/workflows/deploy.yml`: it SSHes into the VM
(repository secrets `VM_HOST`, `VM_USER`, `SSH_PRIVATE_KEY`), pulls this repo,
rebuilds the image, restarts the container with `--restart=always`, and reports
success only once `/health` answers on `127.0.0.1:10000`.

## Endpoints

- `GET /health` — liveness. Also reports the binaries the cloud tools need
  (`deps`: ghostscript, ffmpeg, heic) plus a `degraded` flag.
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
- `POST /audio/trim`, `POST /audio/speed`, `POST /audio/join` — ffmpeg (MP3).
- `POST /video/gif` — video to GIF (ffmpeg).

Limits: 50 MB per file, 20 files per request, **150 MB in total per request**.
Files are processed in memory or in temporary directories that are deleted
immediately — nothing is stored or logged.

The server answers 503 beyond `--limit-concurrency 2` rather than piling
simultaneous heavy jobs into memory.

## Privacy

Files stream through Cloudflare and Caddy to this container, are processed, and
are discarded right away. No file contents, names, or IP addresses are stored or
logged by the application.
