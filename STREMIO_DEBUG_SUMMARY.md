# Stremio Subtitle Debugging Summary

## Goal

The addon should translate the correct synced source subtitle to Hebrew, especially for:

- IMDb: `tt0427340`
- Played file: `Masters.Of.The.Universe.2026.RETAIL.DKSUBS.1080p.WEBRip.x264.AAC-[YTS.GG - YTS.BZ].mp4`

The important requirement after debugging is: **never silently translate the wrong same-name movie/release**.

## What Went Wrong

Several different issues happened during the debugging loop:

1. The addon originally selected OpenSubtitles `file_id=12611930`, release:
   `Masters.Of.The.Universe.2026.1080p.HDTS.H264-OnlyFlix`.

   This was wrong for the played file, which is a `WEBRip/YTS` release. `HDTS`/cam timing does not match `WEBRip`.

2. Stremio sometimes called the subtitle list endpoint with only:
   `filename=...`

   and sometimes with:
   `filename=...&videoHash=...&videoSize=...`

   The addon sometimes acted on the weaker no-hash context.

3. A later attempted fix let a "close release match" bypass runtime/identity checks. That caused a regression back toward wrong same-title selection.

4. A context-merge bug dropped empty keys like `season` and `episode`, causing `KeyError: 'season'` and HTTP 502 when the subtitle file was requested.

5. Returning an empty subtitle list while waiting for hash made the addon disappear from the TV subtitle picker.

## Final Behavior

As of `v0.12.5`, the addon is designed to **fail closed**.

Trusted source paths:

- Manual source `.srt` in `manual_sources/`.
- OpenSubtitles candidate with `moviehash_match=True`.
- Candidate that passes runtime validation and does not conflict with the played release family.

Rejected/unsafe paths:

- `HDTS`/`CAM`/`TS` sources for a `WEBRip` played file.
- Close release-name match alone as proof of correctness.
- Fallback source selection when no moviehash is available and runtime cannot verify the candidate.
- Wrong same-name title/release even if it has plausible popularity or IMDb metadata.

## Current Verified Case

For the problematic movie, the verifier confirms:

```text
OLD logic pick (runtime only): ('runtime_ok', 12611930)
restored safety: HDTS is rejected for WEBRip playback
NEW logic pick (v0.12.5): none
```

Meaning: the addon should no longer choose the wrong `HDTS` source. If no safe source is available, it should fail rather than show wrong Hebrew subtitles.

## Manual Source Escape Hatch

For a guaranteed synced subtitle, put the known-good English `.srt` here:

```text
manual_sources/tt0427340.srt
```

Then restart:

```bash
NO_LOGS=1 sudo ./run_streamio.sh
```

Expected log:

```text
Using MANUAL source subtitle: tt0427340.srt
```

This is the most reliable path because it translates the exact source file that is already known to be synced.

## Verification Commands

Check deployed version:

```bash
curl -fsS http://localhost:5055/healthz | grep addon_version
```

Expected:

```text
0.12.5
```

Run local verifier:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 verify_subtitle_selection.py
```

Watch source selection:

```bash
sudo docker logs -f srt-streamio 2>&1 | grep -E "Resolving source|OST candidate|Skipping subtitle|Using|failed"
```

## Confidence Statement

The code is now ironclad for the most important safety property:

**It should not translate the wrong same-name/HDTS source for this WEBRip file.**

It is not guaranteed to automatically find the exact same synced subtitle as OpenSubtitles v3, because Stremio does not expose v3's chosen file to this addon and our requests may not include a usable moviehash. In that case, the addon should fail closed or use a manual source file.
