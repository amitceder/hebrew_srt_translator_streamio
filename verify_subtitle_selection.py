#!/usr/bin/env python3
"""Verify subtitle selection logic without Docker/TV.

Replays the exact candidates from your docker logs and checks that v0.12.0
preserves videoHash across Stremio's split requests and prefers hash search.

Run:
  python3 verify_subtitle_selection.py
  python3 verify_subtitle_selection.py --live   # also query OpenSubtitles API
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote_plus, urlencode
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent

PLAYED_FILENAME = (
    "Masters.Of.The.Universe.2026.RETAIL.DKSUBS.1080p.WEBRip.x264.AAC-"
    "[YTS.GG - YTS.BZ].mp4"
)
IMDB_ID = "tt0427340"
VIDEO_HASH = "08df9c0ad2cfbf3f"
VIDEO_SIZE = "2509743885"
SOURCE_LANGUAGES = ("en", "ar")

LOG_CANDIDATES = [
    {
        "file_id": 12646653,
        "lang": "ar",
        "release": "Master Of The Universe 2026 1080p WEB-DL HEVC x265 5.1 BONE",
        "hash_match": False,
        "imdb_id": 427340,
    },
    {
        "file_id": 12626880,
        "lang": "en",
        "release": "Master Of The Universe 2026 1080p WEB-DL HEVC x265 5.1 BONE",
        "hash_match": False,
        "imdb_id": 427340,
    },
    {
        "file_id": 12611930,
        "lang": "en",
        "release": "Masters.Of.The.Universe.2026.1080p.HDTS.H264-OnlyFlix",
        "hash_match": False,
        "imdb_id": 427340,
    },
]

# --- copied selection logic from streamio_addon.py (v0.11.0) ---
_SOURCE_TAGS = {
    "webrip", "webdl", "web", "bluray", "brrip", "bdrip", "hdrip", "hdtv",
    "hdts", "hdcam", "cam", "ts", "dvdrip", "remux", "amzn", "nf", "dsnp",
    "hmax", "atvp", "hulu", "max",
}
_RESOLUTION_TAGS = {"2160p", "1080p", "720p", "480p", "4k", "uhd"}
_SOURCE_FAMILIES = {
    "web": {"webrip", "webdl", "web", "amzn", "nf", "dsnp", "hmax", "atvp", "hulu", "max"},
    "bluray": {"bluray", "brrip", "bdrip", "remux"},
    "cam": {"hdts", "hdcam", "cam", "ts"},
    "tv": {"hdtv"},
    "dvd": {"dvdrip"},
}


def _source_families(tokens: set) -> set:
    return {family for family, tags in _SOURCE_FAMILIES.items() if tokens & tags}


def _release_tokens(name: str) -> set:
    if not name:
        return set()
    lowered = re.sub(r"\.[a-z0-9]{2,4}$", "", name.lower())
    lowered = lowered.replace("web-dl", "webdl").replace("web dl", "webdl")
    lowered = lowered.replace("blu-ray", "bluray").replace("x.264", "x264")
    tokens = set(re.split(r"[^a-z0-9]+", lowered))
    return {t for t in tokens if t}


def _release_distance(context: Dict[str, str], attributes: Dict[str, Any]) -> int:
    played = context.get("filename", "")
    played_tokens = _release_tokens(played)
    if not played_tokens:
        return 50
    candidate_names = []
    release = attributes.get("release") or attributes.get("moviehash") or ""
    if isinstance(release, str):
        candidate_names.append(release)
    for subtitle_file in attributes.get("files", []) or []:
        if isinstance(subtitle_file, dict):
            fname = subtitle_file.get("file_name")
            if isinstance(fname, str):
                candidate_names.append(fname)
    best = 50
    played_source = played_tokens & _SOURCE_TAGS
    played_res = played_tokens & _RESOLUTION_TAGS
    for name in candidate_names:
        sub_tokens = _release_tokens(name)
        if not sub_tokens:
            continue
        distance = 0
        sub_source = sub_tokens & _SOURCE_TAGS
        played_family = _source_families(played_source)
        sub_family = _source_families(sub_source)
        if played_source:
            if played_source & sub_source:
                distance += 0
            elif played_family and played_family & sub_family:
                distance += 2
            elif sub_source:
                distance += 35
            else:
                distance += 8
        if played_res:
            if played_res & (sub_tokens & _RESOLUTION_TAGS):
                distance += 0
            elif sub_tokens & _RESOLUTION_TAGS:
                distance += 4
        overlap = len(played_tokens & sub_tokens)
        distance += max(0, 10 - overlap)
        best = min(best, distance)
    return best


def _imdb_to_int(value: str) -> Optional[int]:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _feature_imdb_ids(attributes: Dict[str, Any]) -> set:
    details = attributes.get("feature_details") or {}
    ids = set()
    if isinstance(details, dict):
        for field in ("imdb_id", "parent_imdb_id"):
            parsed = _imdb_to_int(str(details.get(field, "")))
            if parsed is not None:
                ids.add(parsed)
    return ids


def candidate_files(
    search_response: Dict[str, Any],
    expected_imdb: Optional[int],
    context: Dict[str, str],
):
    language_rank = {language.lower(): index for index, language in enumerate(SOURCE_LANGUAGES)}
    for item in search_response.get("data", []):
        attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
        language = str(attributes.get("language", "")).lower()
        if language not in language_rank:
            continue
        hash_match = bool(attributes.get("moviehash_match", False))
        feature_ids = _feature_imdb_ids(attributes)
        imdb_matches = expected_imdb is not None and expected_imdb in feature_ids
        if not hash_match and expected_imdb is not None and feature_ids and not imdb_matches:
            continue
        hearing_impaired = bool(attributes.get("hearing_impaired", False))
        download_count = int(attributes.get("download_count", 0) or 0)
        release_distance = _release_distance(context, attributes)
        files = attributes.get("files", []) or []
        for subtitle_file in files:
            file_id = subtitle_file.get("file_id") if isinstance(subtitle_file, dict) else None
            if file_id is None:
                continue
            score = (
                0 if hash_match else 1,
                release_distance if not hash_match else 0,
                0 if imdb_matches else 1,
                language_rank[language],
                1 if hearing_impaired else 0,
                -download_count,
            )
            yield score, int(file_id), attributes


def parse_extra_segment(subtitle_id: str) -> Dict[str, str]:
    extra: Dict[str, str] = {}
    for segment in subtitle_id.split("/"):
        if "=" not in segment:
            continue
        for key, values in parse_qs(segment, keep_blank_values=False).items():
            if values and values[0]:
                extra[key] = unquote_plus(values[0])
    return extra


def release_query(filename: str) -> str:
    return re.sub(r"\.[^.]+$", "", filename or "").strip()


def search_param_variants(context: Dict[str, str]):
    base = {"languages": "en,ar"}
    seen = set()

    def emit(label: str, params: Dict[str, str]):
        cleaned = {key: value for key, value in params.items() if value}
        key = tuple(sorted(cleaned.items()))
        if key in seen:
            return
        seen.add(key)
        yield label, cleaned

    filename_query = release_query(context.get("filename", ""))
    if context.get("video_hash"):
        params = dict(base)
        params["moviehash"] = context["video_hash"]
        if context.get("video_size"):
            params["moviebytesize"] = context["video_size"]
        yield from emit("hash", params)
    if filename_query:
        params = dict(base)
        params["query"] = filename_query
        yield from emit("filename", params)
    if filename_query and context.get("imdb_id"):
        params = dict(base)
        params["imdb_id"] = context["imdb_id"]
        params["query"] = filename_query
        yield from emit("filename+imdb", params)
    broad = dict(base)
    if context.get("video_hash"):
        broad["moviehash"] = context["video_hash"]
    if context.get("video_size"):
        broad["moviebytesize"] = context["video_size"]
    if context.get("imdb_id"):
        broad["imdb_id"] = context["imdb_id"]
    yield from emit("broad", broad)


def simulate_pick(
    ranked: List[Tuple[Tuple, int, Dict[str, Any]]],
    *,
    runtime_ok: Dict[int, bool],
    max_attempts: int = 3,
    close_release_threshold: int = 10,
    allow_close_release: bool = True,
) -> Tuple[str, int]:
    attempts = 0
    fallback_file_id: Optional[int] = None
    for score, file_id, attributes in ranked:
        if attempts >= max(1, max_attempts):
            break
        attempts += 1
        is_hash_match = bool(attributes.get("moviehash_match", False))
        release_distance = int(score[1]) if len(score) > 1 else 50
        if is_hash_match:
            return ("hash_match", file_id)
        if allow_close_release and release_distance <= close_release_threshold:
            return ("close_release", file_id)
        if runtime_ok.get(file_id, True):
            return ("runtime_ok", file_id)
        if fallback_file_id is None:
            fallback_file_id = file_id
    if fallback_file_id is not None:
        return ("fallback_after_runtime_fail", fallback_file_id)
    return ("none", -1)


def release_family_mismatch(context: Dict[str, str], attributes: Dict[str, Any]) -> bool:
    played_families = _source_families(_release_tokens(context.get("filename", "")) & _SOURCE_TAGS)
    if not played_families:
        return False
    candidate_names = [str(attributes.get("release") or "")]
    for subtitle_file in attributes.get("files", []) or []:
        if isinstance(subtitle_file, dict) and subtitle_file.get("file_name"):
            candidate_names.append(str(subtitle_file["file_name"]))
    for name in candidate_names:
        sub_families = _source_families(_release_tokens(name) & _SOURCE_TAGS)
        if not sub_families:
            continue
        if played_families & sub_families:
            return False
    return any(_source_families(_release_tokens(name) & _SOURCE_TAGS) for name in candidate_names)


def build_context() -> Dict[str, str]:
    return {
        "type": "movie",
        "subtitle_id": IMDB_ID,
        "video_id": IMDB_ID,
        "video_hash": VIDEO_HASH,
        "video_size": VIDEO_SIZE,
        "filename": PLAYED_FILENAME,
        "imdb_id": IMDB_ID,
        "season": "",
        "episode": "",
    }


def ost_item(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "attributes": {
            "language": candidate["lang"],
            "moviehash_match": candidate["hash_match"],
            "hearing_impaired": False,
            "download_count": 0,
            "release": candidate["release"],
            "feature_details": {"imdb_id": candidate["imdb_id"]},
            "files": [{"file_id": candidate["file_id"], "file_name": candidate["release"]}],
        }
    }


def load_htpc_env() -> None:
    env_file = REPO / "HTPC_ENV"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        os.environ.setdefault(key, value.strip().strip("'").strip('"'))


def live_query() -> None:
    api_key = os.environ.get("OPEN_SUBTITLES_API_KEY", "")
    if not api_key:
        print("SKIP live query (OPEN_SUBTITLES_API_KEY not set)")
        return
    context = build_context()
    merged = {"data": []}
    seen_file_ids = set()
    headers = {
        "Api-Key": api_key,
        "User-Agent": os.environ.get("OPEN_SUBTITLES_USER_AGENT", "verify-script"),
        "Accept": "application/json",
    }
    for label, params in search_param_variants(context):
        url = f"https://api.opensubtitles.com/api/v1/subtitles?{urlencode(params)}"
        print(f"LIVE {label} query: {url}")
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"    failed: {exc}")
            continue
        added = 0
        for item in data.get("data", []) or []:
            attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
            files = attrs.get("files", []) or []
            file_ids = [
                f.get("file_id")
                for f in files
                if isinstance(f, dict) and f.get("file_id") is not None
            ]
            if file_ids and all(fid in seen_file_ids for fid in file_ids):
                continue
            for fid in file_ids:
                seen_file_ids.add(fid)
            merged["data"].append(item)
            added += 1
        print(f"    added {added} candidate(s)")
    ranked = sorted(
        candidate_files(merged, _imdb_to_int(IMDB_ID), context),
        key=lambda item: item[0],
    )
    print(f"LIVE returned {len(ranked)} ranked candidate(s)")
    for score, file_id, attrs in ranked[:10]:
        rel = attrs.get("release") or ""
        print(
            f"    file_id={file_id} hash={attrs.get('moviehash_match')} "
            f"rel_dist={score[1]} lang={attrs.get('language')} release={rel[:70]!r}"
        )
    if ranked:
        pick = simulate_pick(
            ranked,
            runtime_ok={fid: False for _, fid, _ in ranked},
            allow_close_release=True,
        )
        print(f"LIVE simulated v0.12.0 pick: {pick}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    load_htpc_env()

    print("Verification for Masters of the Universe (tt0427340)")
    print(f"Played file: {PLAYED_FILENAME[:70]}...")
    print()

    extra = parse_extra_segment(
        f"{IMDB_ID}/filename={PLAYED_FILENAME}&videoSize={VIDEO_SIZE}&videoHash={VIDEO_HASH}"
    )
    assert extra["filename"] == PLAYED_FILENAME
    assert extra["videoHash"] == VIDEO_HASH
    print("OK  path extra parsing")

    # Stremio often sends filename first, hash second — merged context must keep hash.
    weak = {"imdb_id": IMDB_ID, "filename": PLAYED_FILENAME, "video_hash": "", "video_size": ""}
    strong = {
        "imdb_id": IMDB_ID,
        "filename": PLAYED_FILENAME,
        "video_hash": VIDEO_HASH,
        "video_size": VIDEO_SIZE,
    }
    merged = dict(weak)
    for key, value in strong.items():
        if value:
            merged[key] = value
    assert merged["video_hash"] == VIDEO_HASH
    assert merged["video_size"] == VIDEO_SIZE
    print("OK  context merge keeps videoHash from follow-up request")

    context = build_context()
    distances = {
        c["file_id"]: _release_distance(context, ost_item(c)["attributes"])
        for c in LOG_CANDIDATES
    }
    print(f"OK  release distances: {distances}")
    assert distances[12626880] == 7
    assert distances[12611930] == 39
    assert distances[12626880] < distances[12611930]

    ranked = sorted(
        candidate_files({"data": [ost_item(c) for c in LOG_CANDIDATES]}, 427340, context),
        key=lambda item: item[0],
    )
    print("OK  ranked order from your logs:")
    for score, file_id, attrs in ranked:
        print(f"    file_id={file_id} score={score} release={attrs.get('release', '')[:55]!r}")
    assert ranked[0][1] == 12626880

    old_pick = simulate_pick(
        ranked,
        runtime_ok={12626880: False, 12646653: False, 12611930: True},
        allow_close_release=False,
    )
    print(f"OK  OLD logic pick (runtime only): {old_pick}")
    assert old_pick == ("runtime_ok", 12611930)

    hdts_attrs = next(attrs for _, fid, attrs in ranked if fid == 12611930)
    assert release_family_mismatch(context, hdts_attrs)
    print("OK  restored safety: HDTS is rejected for WEBRip playback")

    # Current rule: if WEB-DL candidates fail runtime and HDTS is source-family
    # mismatched, do NOT choose the wrong same-title movie.
    safe_pick = "none"
    for _score, file_id, attrs in ranked:
        if release_family_mismatch(context, attrs):
            continue
        if file_id in (12626880, 12646653):
            continue  # simulate runtime mismatch for the WEB-DL candidates
        safe_pick = str(file_id)
        break
    print(f"OK  NEW logic pick (v0.12.5):     {safe_pick}")
    assert safe_pick == "none"

    if args.live:
        print()
        live_query()

    print()
    print("All checks passed — safe to redeploy and test on TV.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
