# Stremio Hebrew AI subtitle addon

The Flask service exposes a Stremio subtitle addon at:

```text
https://YOUR_PUBLIC_HOST/manifest.json
```

Stremio sends the video hash, file size, filename, and IMDb id. The addon finds
a source subtitle through OpenSubtitles, verifies it is the correct movie
(runtime/hash based), then runs the Google baseline + Groq contextual polish
pipeline and returns a cached Hebrew SRT. It prefers English/Arabic sources and
falls back to other languages only when they are the ones actually synced to the
played release (the output is Hebrew either way).

## Required server settings

Set these in `HTPC_ENV` (git-ignored) before starting the container:

```bash
export GROQ_API_KEY='your-groq-key'
export OPEN_SUBTITLES_API_KEY='your-opensubtitles-api-key'
export OPEN_SUBTITLES_USERNAME='your-opensubtitles-username'   # higher quota
export OPEN_SUBTITLES_PASSWORD='your-opensubtitles-password'
export OPEN_SUBTITLES_LANGUAGES='en,ar,hr,el,tr'
export STREAMIO_PRIMARY_SOURCE_LANGUAGES='en,ar'
export STREAMIO_PUBLIC_BASE_URL='https://your-public-host'      # no trailing slash
export STREAMIO_TOKEN_SECRET='a-long-random-secret'
```

`STREAMIO_PUBLIC_BASE_URL` must be the HTTPS URL that Stremio on the phone, TV,
and PC can reach. Legacy `STREMIO_*` names are still accepted as a fallback.

## Start the addon container

```bash
NO_LOGS=1 sudo ./run_streamio.sh
```

This builds/starts only the `srt-streamio` container (the separate local
`srt-app` container is never touched). Verify:

```bash
curl -fsS http://localhost:5055/healthz         # addon_version + config flags
curl -sS -D - -o /dev/null http://localhost:5055/manifest.json | grep -i access-control
```

The manifest response includes `Access-Control-Allow-Origin: *`, which is
required for the Stremio desktop app and Stremio Web to install the addon.

## Public HTTPS (Cloudflare)

Two options:

- **Quick tunnel (no domain):** keep it running with
  `nohup ./run_quick_tunnel.sh > quick_tunnel.out 2>&1 &`. The full paste-ready
  manifest URL is written to `CURRENT_ADDON_URL.txt`. The URL changes on every
  restart and dies if the tunnel process stops, so reinstall it in Stremio each
  time.
- **Named tunnel (stable domain):** run `./setup_tunnel.sh yourdomain.com` once
  (requires the domain's DNS to be active on Cloudflare), then keep it up with
  `./run_tunnel.sh`. This gives a permanent URL you install once.

## Install in Stremio

Paste the manifest URL (the full line in `CURRENT_ADDON_URL.txt`) into the
Stremio Addons search/URL field on any device. It appears as `Hebrew AI`.

- Works on phone, TV, and PC/Web (PC/Web requires the CORS headers above and a
  live public URL).
- If a wrong/old subtitle is cached on a client, fully close and reopen the
  Stremio app so it re-fetches.

## Manual source override

If OpenSubtitles has no synced source for a specific release, drop a known-good
`.srt` into `manual_sources/` named by IMDb id (e.g. `manual_sources/tt0427340.srt`).
The addon translates that exact file instead of guessing.

## Troubleshooting

See `STREMIO_DEBUG_SUMMARY.md` for the full history of subtitle-selection and
tunnel/CORS issues and how they were resolved.
