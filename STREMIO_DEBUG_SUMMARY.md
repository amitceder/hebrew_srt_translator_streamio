# Stremio Addon Debugging Summary

Running log of the bugs we hit and how the addon behaves now. Current addon
version: **0.12.12**.

## Goal

Translate the **correct, synced** source subtitle to Hebrew for the played
release, and never silently translate the wrong same-name movie.

Reference problem cases used throughout debugging:

- `tt0427340` "Masters of the Universe" (2026) — collides with `tt41087705`
  "Master of the Universe" (2026). OpenSubtitles misfiles one under the other.
- `tt29355505` "Toy Story 5" (2026) — used to catch the fallback-language
  regression below.

## Source-selection model (current)

Identity is decided by **runtime**, not by release name or IMDb tag, because
OpenSubtitles misfiles same-name films under each other's IMDb id.

Priority when picking a source subtitle:

1. Manual drop-in `.srt` in `manual_sources/` (highest, bypasses OpenSubtitles).
2. Candidate with `moviehash_match=True` (byte-for-byte the same video).
3. Candidate whose subtitle runtime matches the expected feature length
   (rejects the wrong 80-min film for a ~140-min movie), ranked by:
   - matching release/source family first,
   - **primary source language (English/Arabic) before fallback languages**,
   - closest release name,
   - closest runtime.

If nothing verifiable is found, the addon **fails closed** instead of guessing.

## Key bugs and fixes (chronological)

1. **Wrong same-name movie selected.** A noisy filename search returned a
   different film (`imdb_match=False`, no feature imdb). Fix: non-hash
   candidates must positively match the target IMDb id (v0.12.6).

2. **Runtime hole.** If Cinemeta returned no runtime, a misfiled 80-min wrong
   movie could pass. Fix: in strict mode a non-hash candidate is accepted only
   when runtime is present AND matches; otherwise fail closed (v0.12.7).

3. **Correct movie discarded as "wrong family".** The only correct-movie
   English sub was an HDTS telesync; a family guard wrongly dropped it. Fix:
   source family only affects sync ranking, never identity — a correct-movie
   source is acceptable even if timing is imperfect (v0.12.8).

4. **No synced English/Arabic for some releases.** For `tt0427340`, only
   Greek/Turkish/Croatian subs are actually synced to the YTS WEBRip. Added
   those as fallback source languages so we can still produce a synced Hebrew
   track (v0.12.9–0.12.10). Since we output Hebrew, source language does not
   need to be English.

5. **Fallback languages hijacked normal titles.** Toy Story 5 picked a Greek
   source over a perfectly good English one because "closest release" beat
   language. Fix: English/Arabic are explicit **primary** languages and win
   over fallback languages when both are valid/synced
   (`STREAMIO_PRIMARY_SOURCE_LANGUAGES`, v0.12.11).

6. **PC / Stremio Web could not install the addon.** The addon returned no CORS
   headers. Desktop/Web enforce CORS; the native mobile app ignores it (hence
   "works on phone, not PC"). Fix: permissive CORS headers on every response
   (v0.12.12).

7. **Dead public URL (Cloudflare 530).** Quick-tunnel URLs change on every
   restart and die if the tunnel process stops. The saved URL pointed at a dead
   tunnel, so the web app could not fetch the manifest. Not an addon bug — start
   a fresh quick tunnel and reinstall the new URL. A named tunnel
   (`hebrew-subs.streamio-amit.com`) is prepared but pending DNS.

Earlier fixes still in force: context merge/normalize (no more
`KeyError: 'season'`), always advertise a track so the addon never disappears
from the picker, source cache verified against metadata, cache cleanup.

## Manual source escape hatch

For a guaranteed synced subtitle, drop a known-good `.srt` here:

```text
manual_sources/tt0427340.srt
```

Expected log line when used:

```text
Using MANUAL source subtitle: tt0427340.srt
```

## Tunnel / public URL notes

- Quick tunnel (current): run and keep it alive with
  `nohup ./run_quick_tunnel.sh > quick_tunnel.out 2>&1 &`.
- The full paste-ready manifest link is written to `CURRENT_ADDON_URL.txt`
  (now including `/manifest.json`).
- The URL changes on every restart; reinstall it in Stremio each time until the
  stable named-tunnel DNS is ready.

## Verification commands

Deployed version:

```bash
curl -fsS http://localhost:5055/healthz | grep addon_version   # expect 0.12.12
```

CORS header present (locally and over the tunnel):

```bash
curl -sS -D - -o /dev/null http://localhost:5055/manifest.json | grep -i access-control
```

Watch source selection:

```bash
sudo docker logs -f srt-streamio 2>&1 | grep -E "Resolving source|OST candidate|Runtime-verified|Skipping subtitle|Using|failed"
```

## Confidence statement

Safety property held: the addon should not translate the wrong same-name film.
It prefers English/Arabic, falls back to other synced languages only when
needed, and fails closed when it cannot verify the correct movie.
