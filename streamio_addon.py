"""Stremio subtitle integration for the hybrid Hebrew SRT translator.

Stremio sends subtitle requests with the media file hash/size and filename,
but it does not send the source subtitle text. This module resolves an
English/Arabic source subtitle through OpenSubtitles, then delegates the
Google + Groq translation pipeline to ``app.process_translation``.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote_plus, urlencode, urljoin
from urllib.request import Request, urlopen

from flask import Response, jsonify, request


LOGGER = logging.getLogger(__name__)


def _env(name: str, default: str = "", legacy: Optional[str] = None) -> str:
    """Read an env var, with optional legacy name fallback."""
    value = os.environ.get(name)
    if value:
        return value
    if legacy:
        value = os.environ.get(legacy)
        if value:
            return value
    return default


ADDON_VERSION = "0.12.12"
OPEN_SUBTITLES_BASE_URL = os.environ.get(
    "OPEN_SUBTITLES_BASE_URL", "https://api.opensubtitles.com/api/v1"
).rstrip("/")
OPEN_SUBTITLES_API_KEY = os.environ.get("OPEN_SUBTITLES_API_KEY", "")
OPEN_SUBTITLES_USERNAME = os.environ.get("OPEN_SUBTITLES_USERNAME", "").strip()
OPEN_SUBTITLES_PASSWORD = os.environ.get("OPEN_SUBTITLES_PASSWORD", "")
OPEN_SUBTITLES_USER_AGENT = os.environ.get(
    "OPEN_SUBTITLES_USER_AGENT", "HebrewAIStreamioAddon v0.1"
)
SOURCE_LANGUAGES = tuple(
    part.strip()
    for part in os.environ.get("OPEN_SUBTITLES_LANGUAGES", "en,ar,hr,el,tr").split(",")
    if part.strip()
)
PRIMARY_SOURCE_LANGUAGES = tuple(
    part.strip().lower()
    for part in os.environ.get("STREAMIO_PRIMARY_SOURCE_LANGUAGES", "en,ar").split(",")
    if part.strip()
)
HTTP_TIMEOUT_SECONDS = int(os.environ.get("OPEN_SUBTITLES_TIMEOUT", "30"))
PUBLIC_BASE_URL = _env("STREAMIO_PUBLIC_BASE_URL", "", "STREMIO_PUBLIC_BASE_URL").rstrip("/")
CACHE_DIR = Path(
    _env(
        "STREAMIO_CACHE_DIR",
        str(Path(__file__).resolve().parent / "streamio_cache"),
        "STREMIO_CACHE_DIR",
    )
)
TOKEN_SECRET = _env("STREAMIO_TOKEN_SECRET", "", "STREMIO_TOKEN_SECRET")
if TOKEN_SECRET:
    TOKEN_SECRET_BYTES = TOKEN_SECRET.encode("utf-8")
else:
    # A process-local secret is safe for local testing. Production deployments
    # should set STREAMIO_TOKEN_SECRET so subtitle URLs survive restarts.
    TOKEN_SECRET_BYTES = secrets.token_bytes(32)

try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    LOGGER.warning("Could not create addon cache directory: %s", CACHE_DIR)


_JOBS: Dict[str, "TranslationJob"] = {}
_JOBS_GUARD = threading.Lock()
_STREAM_CONTEXTS: Dict[str, Dict[str, str]] = {}
_STREAM_GUARD = threading.Lock()
_STREAM_COND = threading.Condition(_STREAM_GUARD)
HASH_WAIT_LIST_SECONDS = float(_env("STREAMIO_HASH_WAIT_LIST", "3", "STREMIO_HASH_WAIT_LIST"))
HASH_WAIT_DOWNLOAD_SECONDS = float(
    _env("STREAMIO_HASH_WAIT_DOWNLOAD", "20", "STREMIO_HASH_WAIT_DOWNLOAD")
)
GOOGLE_WAIT_TIMEOUT = int(_env("STREAMIO_GOOGLE_TIMEOUT", "180", "STREMIO_GOOGLE_TIMEOUT"))
POLISH_WAIT_TIMEOUT = int(_env("STREAMIO_POLISH_TIMEOUT", "900", "STREMIO_POLISH_TIMEOUT"))

# Cache cleanup: delete cached subtitles older than TTL, and cap total size.
CACHE_TTL_DAYS = float(_env("STREAMIO_CACHE_TTL_DAYS", "30", "STREMIO_CACHE_TTL_DAYS"))
CACHE_MAX_MB = float(_env("STREAMIO_CACHE_MAX_MB", "500", "STREMIO_CACHE_MAX_MB"))
CACHE_CLEAN_INTERVAL_HOURS = float(
    _env("STREAMIO_CACHE_CLEAN_INTERVAL_HOURS", "12", "STREMIO_CACHE_CLEAN_INTERVAL_HOURS")
)
_CLEANUP_STARTED = False
_CLEANUP_GUARD = threading.Lock()

# Source-subtitle disambiguation (rev 0.4): verify the OpenSubtitles result
# actually matches the movie/episode Stremio asked for. This fixes cases where
# two different films share a name/year and the wrong subtitle gets picked.
#
# NOTE: runtime verification requires DOWNLOADING candidates to inspect their
# length, and the free OpenSubtitles API key allows only ~5 downloads/day. So
# now that login is configured, it is ON by default. Source cache prevents
# future re-downloads; runtime check prevents bad/misfiled OpenSubtitles files.
VERIFY_RUNTIME = _env("STREAMIO_VERIFY_RUNTIME", "1", "STREMIO_VERIFY_RUNTIME") not in ("0", "false", "")
MAX_VERIFY_DOWNLOADS = int(_env("STREAMIO_MAX_VERIFY_DOWNLOADS", "8", "STREMIO_MAX_VERIFY_DOWNLOADS"))
RUNTIME_TOLERANCE_MIN = float(_env("STREAMIO_RUNTIME_TOLERANCE_MIN", "8", "STREMIO_RUNTIME_TOLERANCE_MIN"))
RUNTIME_TOLERANCE_PCT = float(_env("STREAMIO_RUNTIME_TOLERANCE_PCT", "0.15", "STREMIO_RUNTIME_TOLERANCE_PCT"))
SOURCE_CACHE_RULES_VERSION = _env("STREAMIO_SOURCE_RULES_VERSION", "16", "STREMIO_SOURCE_RULES_VERSION")
CINEMETA_BASE_URL = _env(
    "STREAMIO_CINEMETA_URL", "https://v3-cinemeta.strem.io", "STREMIO_CINEMETA_URL"
).rstrip("/")

# Manual source drop-in: put a known-good English/Arabic .srt here and the addon
# will translate THAT file directly instead of guessing via OpenSubtitles. This
# is the reliable escape hatch when OpenSubtitles has no synced match for your
# exact release (e.g. a YTS WEBRip with no moviehash entry).
MANUAL_SOURCE_DIR = Path(
    _env(
        "STREAMIO_MANUAL_SOURCE_DIR",
        str(Path(__file__).resolve().parent / "manual_sources"),
        "STREMIO_MANUAL_SOURCE_DIR",
    )
)
try:
    MANUAL_SOURCE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    LOGGER.warning("Could not create manual source directory: %s", MANUAL_SOURCE_DIR)


class SubtitleProviderError(RuntimeError):
    """Raised when a usable source subtitle cannot be resolved."""


class QuotaExceededError(SubtitleProviderError):
    """Raised when the OpenSubtitles daily download quota is exhausted."""


def _first_arg(*names: str) -> str:
    for name in names:
        value = request.args.get(name, "").strip()
        if value:
            return value
    return ""


_OST_SESSION_FILE = CACHE_DIR / ".opensubtitles_session.json"
_OST_TOKEN: Optional[str] = None
_OST_TOKEN_EXPIRES_AT = 0.0
_OST_AUTH_GUARD = threading.Lock()


def _ost_login() -> str:
    """Authenticate with OpenSubtitles and return a bearer token."""
    if not OPEN_SUBTITLES_USERNAME or not OPEN_SUBTITLES_PASSWORD:
        raise SubtitleProviderError(
            "OPEN_SUBTITLES_USERNAME and OPEN_SUBTITLES_PASSWORD are not configured"
        )
    if not OPEN_SUBTITLES_API_KEY:
        raise SubtitleProviderError("OPEN_SUBTITLES_API_KEY is not configured")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Api-Key": OPEN_SUBTITLES_API_KEY,
        "User-Agent": OPEN_SUBTITLES_USER_AGENT,
    }
    payload = json.dumps(
        {"username": OPEN_SUBTITLES_USERNAME, "password": OPEN_SUBTITLES_PASSWORD}
    ).encode("utf-8")
    raw = _open_url(
        f"{OPEN_SUBTITLES_BASE_URL}/login",
        method="POST",
        payload=payload,
        headers=headers,
    )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubtitleProviderError("OpenSubtitles login returned invalid JSON") from exc
    token = parsed.get("token") if isinstance(parsed, dict) else None
    if not token:
        raise SubtitleProviderError("OpenSubtitles login did not return a token")
    return str(token)


def _ost_load_cached_token() -> Optional[str]:
    global _OST_TOKEN, _OST_TOKEN_EXPIRES_AT
    try:
        if not _OST_SESSION_FILE.is_file():
            return None
        data = json.loads(_OST_SESSION_FILE.read_text(encoding="utf-8"))
        token = data.get("token")
        expires_at = float(data.get("expires_at", 0))
        if token and expires_at > time.time() + 60:
            _OST_TOKEN = str(token)
            _OST_TOKEN_EXPIRES_AT = expires_at
            return _OST_TOKEN
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        LOGGER.warning("Could not read OpenSubtitles session cache")
    return None


def _ost_save_token(token: str) -> None:
    global _OST_TOKEN, _OST_TOKEN_EXPIRES_AT
    # OpenSubtitles tokens are valid for ~24h; refresh a little early.
    expires_at = time.time() + 23 * 3600
    _OST_TOKEN = token
    _OST_TOKEN_EXPIRES_AT = expires_at
    try:
        _OST_SESSION_FILE.write_text(
            json.dumps({"token": token, "expires_at": expires_at}),
            encoding="utf-8",
        )
    except OSError:
        LOGGER.warning("Could not persist OpenSubtitles session cache")


def _ost_bearer_token(*, force_login: bool = False) -> Optional[str]:
    """Return a valid bearer token when username/password are configured."""
    if not OPEN_SUBTITLES_USERNAME or not OPEN_SUBTITLES_PASSWORD:
        return None
    with _OST_AUTH_GUARD:
        if not force_login:
            if _OST_TOKEN and _OST_TOKEN_EXPIRES_AT > time.time() + 60:
                return _OST_TOKEN
            cached = _ost_load_cached_token()
            if cached:
                return cached
        token = _ost_login()
        _ost_save_token(token)
        LOGGER.info("OpenSubtitles login OK for user %s", OPEN_SUBTITLES_USERNAME)
        return token


def _request_headers(*, for_download: bool = False) -> Dict[str, str]:
    headers = {
        "Accept": "*/*" if for_download else "application/json",
        "Api-Key": OPEN_SUBTITLES_API_KEY,
        "User-Agent": OPEN_SUBTITLES_USER_AGENT,
    }
    token = _ost_bearer_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _open_url(url: str, method: str = "GET", payload: Optional[bytes] = None,
              headers: Optional[Dict[str, str]] = None) -> bytes:
    req_headers = headers or {}
    request_obj = Request(url, data=payload, headers=req_headers, method=method)
    try:
        with urlopen(request_obj, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as exc:
        endpoint = url.split("?", 1)[0]
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        # HTTP 406 on /download means either the daily download quota is
        # exhausted or the request is otherwise unacceptable. Surface the
        # provider's own message so the cause is obvious in logs and to Stremio.
        if exc.code == 406:
            message = ""
            try:
                data = json.loads(body) if body else {}
                if isinstance(data, dict):
                    message = str(data.get("message", ""))
            except (json.JSONDecodeError, ValueError):
                message = body.strip()
            LOGGER.warning("Subtitle provider HTTP 406 for %s: %s", endpoint, message or "(no body)")
            if "download" in message.lower() and "allowed" in message.lower():
                raise QuotaExceededError(
                    message or "OpenSubtitles daily download quota reached"
                ) from exc
            raise SubtitleProviderError(
                message or "Subtitle provider rejected the download request (406)"
            ) from exc
        LOGGER.warning("Subtitle provider HTTP %s for %s: %s", exc.code, endpoint, body[:200])
        raise SubtitleProviderError("Subtitle provider request failed") from exc
    except URLError as exc:
        LOGGER.warning("Subtitle provider network error for %s: %s", url.split("?", 1)[0], exc)
        raise SubtitleProviderError("Subtitle provider is unreachable") from exc


def _get_json(url: str) -> Dict[str, Any]:
    raw = _open_url(url, headers=_request_headers())
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubtitleProviderError("Subtitle provider returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise SubtitleProviderError("Subtitle provider returned an unexpected response")
    return parsed


def _post_json(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = _request_headers()
    headers["Content-Type"] = "application/json"
    raw = _open_url(
        url,
        method="POST",
        payload=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubtitleProviderError("Subtitle provider returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise SubtitleProviderError("Subtitle provider returned an unexpected response")
    return parsed


def _decode_subtitle_bytes(raw: bytes) -> str:
    """Decode a direct SRT or an OpenSubtitles ZIP response."""
    if raw.startswith(b"PK"):
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                candidates = [
                    name for name in archive.namelist() if name.lower().endswith(".srt")
                ]
                if not candidates:
                    raise SubtitleProviderError("Downloaded archive contains no SRT file")
                raw = archive.read(candidates[0])
        except (zipfile.BadZipFile, KeyError) as exc:
            raise SubtitleProviderError("Downloaded subtitle archive is invalid") from exc

    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1255", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding).replace("\ufeff", "")
        except UnicodeDecodeError:
            continue
    raise SubtitleProviderError("Downloaded subtitle encoding is not supported")


def _imdb_context(video_id: str, subtitle_id: str) -> Tuple[str, str, str]:
    candidate = video_id or subtitle_id
    match = re.search(r"(tt\d+)(?::(\d+):(\d+))?", candidate, flags=re.IGNORECASE)
    if not match:
        return "", "", ""
    return match.group(1), match.group(2) or "", match.group(3) or ""


def _parse_extra_segment(subtitle_id: str) -> Dict[str, str]:
    """Extract Stremio "extra" props embedded in the subtitle id path.

    Stremio passes optional metadata (videoHash, videoSize, filename, videoId)
    NOT as query args but as an extra path segment before .json, e.g.:
        tt0427340/videoHash=8e24...&videoSize=1234567&filename=Movie.WEBRip.mkv
    The whole tail lands in <path:subtitle_id>, so we split on "/" and parse any
    segment that looks like url-encoded key=value pairs.
    """
    extra: Dict[str, str] = {}
    for segment in subtitle_id.split("/"):
        if "=" not in segment:
            continue
        for key, values in parse_qs(segment, keep_blank_values=False).items():
            if values and values[0]:
                extra[key] = unquote_plus(values[0])
    return extra


def _request_context(content_type: str, subtitle_id: str) -> Dict[str, str]:
    # Extra props may arrive either as query args OR embedded in the id path.
    extra = _parse_extra_segment(subtitle_id)

    def pick(*names: str) -> str:
        value = _first_arg(*names)
        if value:
            return value
        for name in names:
            if extra.get(name):
                return extra[name]
        return ""

    video_id = pick("videoId", "video_id", "videoid")
    video_hash = pick("videoHash", "video_hash", "moviehash")
    video_size = pick("videoSize", "video_size", "moviebytesize")
    filename = pick("filename", "fileName", "file_name")

    # The leading path component is the real content id (strip the extra tail).
    base_id = subtitle_id.split("/", 1)[0]

    if not video_id and base_id.lower().startswith("tt"):
        video_id = base_id
    if not video_hash and re.fullmatch(r"[0-9a-fA-F]{32}", base_id):
        video_hash = base_id

    imdb_id, season, episode = _imdb_context(video_id, base_id)
    return _normalize_context({
        "type": content_type,
        "subtitle_id": base_id,
        "video_id": video_id,
        "video_hash": video_hash,
        "video_size": video_size,
        "filename": filename,
        "imdb_id": imdb_id,
        "season": season,
        "episode": episode,
    })


_CONTEXT_FIELDS = (
    "type",
    "subtitle_id",
    "video_id",
    "video_hash",
    "video_size",
    "filename",
    "imdb_id",
    "season",
    "episode",
)


def _normalize_context(context: Dict[str, str]) -> Dict[str, str]:
    """Guarantee all standard keys exist so bracket access never KeyErrors."""
    normalized = {field: str(context.get(field, "") or "") for field in _CONTEXT_FIELDS}
    # Preserve any extra keys we don't know about, as strings.
    for key, value in context.items():
        if key not in normalized:
            normalized[key] = str(value or "")
    return normalized


def _context_score(context: Dict[str, str]) -> int:
    """Higher means more metadata for OpenSubtitles hash/release matching."""
    score = 0
    if context.get("video_hash"):
        score += 8
    if context.get("video_size"):
        score += 4
    if context.get("filename"):
        score += 2
    if context.get("imdb_id"):
        score += 1
    return score


def _merge_context(base: Dict[str, str], incoming: Dict[str, str]) -> Dict[str, str]:
    merged = dict(base)
    for key, value in incoming.items():
        if value:
            merged[key] = value
    return merged


def _stream_key(context: Dict[str, str]) -> str:
    """Stable identity for one played stream (hash/no-hash requests share this)."""
    payload = {
        "type": context.get("type", ""),
        "imdb_id": context.get("imdb_id", ""),
        "season": context.get("season", ""),
        "episode": context.get("episode", ""),
        "filename": context.get("filename", ""),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _requires_hash_for_source(context: Dict[str, str]) -> bool:
    """When Stremio sends a release filename, moviehash is how v3 gets sync."""
    return bool(context.get("filename"))


def _remember_context(context: Dict[str, str]) -> Dict[str, str]:
    """Merge this request into the best-known context for the played stream."""
    key = _stream_key(context)
    with _STREAM_COND:
        previous = _STREAM_CONTEXTS.get(key)
        merged = _normalize_context(_merge_context(previous or {}, context))
        _STREAM_CONTEXTS[key] = merged
        if _context_score(merged) > _context_score(previous or {}):
            LOGGER.info(
                "Stream context upgraded stream=%s hash=%s size=%s filename=%r",
                key[:8],
                merged.get("video_hash") or "-",
                merged.get("video_size") or "-",
                (merged.get("filename") or "")[:72],
            )
        _STREAM_COND.notify_all()
    return merged


def _wait_for_hash_context(
    context: Dict[str, str], timeout: float
) -> Dict[str, str]:
    """Wait briefly for a concurrent Stremio request that includes videoHash."""
    if context.get("video_hash") or not _requires_hash_for_source(context):
        return context
    key = _stream_key(context)
    deadline = time.time() + max(0.0, timeout)
    with _STREAM_COND:
        while time.time() < deadline:
            stored = _STREAM_CONTEXTS.get(key, context)
            if stored.get("video_hash"):
                LOGGER.info(
                    "Received videoHash after %.1fs for stream=%s",
                    timeout - (deadline - time.time()),
                    key[:8],
                )
                return stored
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            _STREAM_COND.wait(timeout=min(0.15, remaining))
        return _STREAM_CONTEXTS.get(key, context)


def _resolve_playback_context(
    context: Dict[str, str], *, mode: str = "list"
) -> Dict[str, str]:
    """Canonical context for tokens, jobs, and OpenSubtitles lookups."""
    merged = _remember_context(context)
    if _requires_hash_for_source(merged) and not merged.get("video_hash"):
        timeout = HASH_WAIT_LIST_SECONDS if mode == "list" else HASH_WAIT_DOWNLOAD_SECONDS
        merged = _wait_for_hash_context(merged, timeout)
    if _requires_hash_for_source(merged) and not merged.get("video_hash"):
        LOGGER.warning(
            "Proceeding without videoHash for %s (%r); sync may be imperfect",
            merged.get("imdb_id", "?"),
            (merged.get("filename") or "")[:72],
        )
    return merged


def _search_params(context: Dict[str, str]) -> Dict[str, str]:
    params: Dict[str, str] = {"languages": ",".join(SOURCE_LANGUAGES)}
    if context["video_hash"]:
        params["moviehash"] = context["video_hash"]
    if context["video_size"]:
        params["moviebytesize"] = context["video_size"]
    if context["imdb_id"]:
        params["imdb_id"] = context["imdb_id"]
    if context["season"]:
        params["season_number"] = context["season"]
    if context["episode"]:
        params["episode_number"] = context["episode"]
    if not context["video_hash"] and not context["imdb_id"] and context["filename"]:
        params["query"] = re.sub(r"\.[^.]+$", "", context["filename"])
    return params


def _release_query(filename: str) -> str:
    """Filename query for OpenSubtitles, without only the media extension."""
    return re.sub(r"\.[^.]+$", "", filename or "").strip()


def _search_param_variants(context: Dict[str, str]) -> Iterable[Tuple[str, Dict[str, str]]]:
    """Search OpenSubtitles like a subtitle addon would: exact file first.

    A single IMDb search can miss release-specific entries or return a generic
    title match. Querying the actual played filename/release gives
    OpenSubtitles a chance to return the same synced source that its v3 addon
    shows in Stremio.
    """
    base: Dict[str, str] = {"languages": ",".join(SOURCE_LANGUAGES)}
    seen = set()

    def emit(label: str, params: Dict[str, str]):
        cleaned = {key: value for key, value in params.items() if value}
        key = tuple(sorted(cleaned.items()))
        if key in seen:
            return
        seen.add(key)
        yield label, cleaned

    filename_query = _release_query(context.get("filename", ""))

    # Frame-exact path, but do not constrain by IMDb because OpenSubtitles
    # sometimes files the correct hash under the wrong title.
    if context.get("video_hash"):
        params = dict(base)
        params["moviehash"] = context["video_hash"]
        if context.get("video_size"):
            params["moviebytesize"] = context["video_size"]
        yield from emit("hash", params)

    # Release-name path, closest to how users search/select subtitles manually
    # and how the OpenSubtitles addon can surface file-specific results.
    if filename_query:
        params = dict(base)
        params["query"] = filename_query
        yield from emit("filename", params)

    # Hybrid path: same title plus exact release string.
    if filename_query and context.get("imdb_id"):
        params = dict(base)
        params["imdb_id"] = context["imdb_id"]
        params["query"] = filename_query
        if context.get("season"):
            params["season_number"] = context["season"]
        if context.get("episode"):
            params["episode_number"] = context["episode"]
        yield from emit("filename+imdb", params)

    # Broad fallback, preserving the previous behavior.
    yield from emit("broad", _search_params(context))


def _search_subtitles(context: Dict[str, str]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {"data": []}
    seen_file_ids = set()
    errors = []
    for label, params in _search_param_variants(context):
        query = urlencode(params)
        try:
            response = _get_json(f"{OPEN_SUBTITLES_BASE_URL}/subtitles?{query}")
        except SubtitleProviderError as exc:
            errors.append(f"{label}: {exc}")
            LOGGER.warning("OpenSubtitles %s search failed: %s", label, exc)
            continue

        added = 0
        for item in response.get("data", []) or []:
            attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
            files = attributes.get("files", []) or []
            item_file_ids = [
                subtitle_file.get("file_id")
                for subtitle_file in files
                if isinstance(subtitle_file, dict) and subtitle_file.get("file_id") is not None
            ]
            if item_file_ids and all(file_id in seen_file_ids for file_id in item_file_ids):
                continue
            for file_id in item_file_ids:
                seen_file_ids.add(file_id)
            merged["data"].append(item)
            added += 1
        LOGGER.info("OpenSubtitles %s search added %d candidate(s)", label, added)

    if not merged["data"] and errors:
        raise SubtitleProviderError("; ".join(errors))
    return merged


def _imdb_to_int(value: str) -> Optional[int]:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _feature_imdb_ids(attributes: Dict[str, Any]) -> set:
    """IMDb ids (as ints) tied to a subtitle's feature, incl. parent series."""
    details = attributes.get("feature_details") or {}
    ids = set()
    if isinstance(details, dict):
        for field in ("imdb_id", "parent_imdb_id"):
            parsed = _imdb_to_int(details.get(field, ""))
            if parsed is not None:
                ids.add(parsed)
    return ids


# Release tags that most strongly determine subtitle timing/sync. A subtitle
# made for the same source (WEBRip vs HDTS vs BluRay) is far more likely to be
# in sync with the played file than one from a different source.
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
    return {
        family
        for family, tags in _SOURCE_FAMILIES.items()
        if tokens & tags
    }


def _release_tokens(name: str) -> set:
    """Normalize a release/file name into comparable lowercase tokens."""
    if not name:
        return set()
    lowered = re.sub(r"\.[a-z0-9]{2,4}$", "", name.lower())  # strip extension
    # webdl / web-dl / web.dl should all normalize to the same token.
    lowered = lowered.replace("web-dl", "webdl").replace("web dl", "webdl")
    lowered = lowered.replace("blu-ray", "bluray").replace("x.264", "x264")
    tokens = set(re.split(r"[^a-z0-9]+", lowered))
    return {t for t in tokens if t}


def _release_distance(context: Dict[str, str], attributes: Dict[str, Any]) -> int:
    """Lower is a better sync match to the played file's release name.

    Compares the played filename against the subtitle's `release` and its
    individual file names, rewarding matching source (WEBRip/BluRay/...),
    resolution, and release group.
    """
    played = context.get("filename", "")
    played_tokens = _release_tokens(played)
    if not played_tokens:
        return 50  # neutral distance when Stremio gave us no filename

    # Collect candidate name strings from the subtitle metadata.
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
        # Source mismatch is the biggest sync risk. Treat WEBRip/WEB-DL and
        # provider tags (AMZN/NF/etc.) as one WEB family; they are usually much
        # closer to each other than any CAM/HDTS release.
        sub_source = sub_tokens & _SOURCE_TAGS
        played_family = _source_families(played_source)
        sub_family = _source_families(sub_source)
        if played_source:
            if played_source & sub_source:
                distance += 0
            elif played_family and played_family & sub_family:
                distance += 2
            elif sub_source:
                distance += 35  # known but different source family
            else:
                distance += 8   # unknown source
        # Resolution mismatch is a weaker but real signal.
        if played_res:
            if played_res & (sub_tokens & _RESOLUTION_TAGS):
                distance += 0
            elif sub_tokens & _RESOLUTION_TAGS:
                distance += 4
        # Reward general token overlap (release group, codec, etc.).
        overlap = len(played_tokens & sub_tokens)
        distance += max(0, 10 - overlap)
        best = min(best, distance)
    return best


def _release_family_mismatch(context: Dict[str, str], attributes: Dict[str, Any]) -> bool:
    """True when played file and subtitle are from clearly different sources."""
    played_families = _source_families(_release_tokens(context.get("filename", "")) & _SOURCE_TAGS)
    if not played_families:
        return False
    candidate_names = []
    release = attributes.get("release") or attributes.get("moviehash") or ""
    if isinstance(release, str):
        candidate_names.append(release)
    for subtitle_file in attributes.get("files", []) or []:
        if isinstance(subtitle_file, dict) and isinstance(subtitle_file.get("file_name"), str):
            candidate_names.append(str(subtitle_file["file_name"]))
    for name in candidate_names:
        sub_families = _source_families(_release_tokens(name) & _SOURCE_TAGS)
        if not sub_families:
            continue
        if played_families & sub_families:
            return False
    return any(_source_families(_release_tokens(name) & _SOURCE_TAGS) for name in candidate_names)


def _candidate_files(
    search_response: Dict[str, Any],
    expected_imdb: Optional[int],
    context: Optional[Dict[str, str]] = None,
):
    """Yield candidate subtitles as (score, file_id, attributes).

    Lower score sorts first. Priority: exact movie-hash match (frame-accurate
    sync), correct IMDb feature match, closest release name (source/resolution
    for sync), requested source-language order, non-SDH, download history.
    Candidates whose feature IMDb id is known and does NOT match the requested
    title are dropped outright (this is what fixes same-name/year collisions).
    """
    context = context or {}
    language_rank = {language.lower(): index for index, language in enumerate(SOURCE_LANGUAGES)}
    for item in search_response.get("data", []):
        attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
        language = str(attributes.get("language", "")).lower()
        if language not in language_rank:
            continue

        hash_match = bool(attributes.get("moviehash_match", False))
        feature_ids = _feature_imdb_ids(attributes)
        imdb_matches = expected_imdb is not None and expected_imdb in feature_ids
        # Hard filter: when we know the target IMDb id, a non-hash candidate MUST
        # positively match that IMDb id to be trusted. The filename search is noisy
        # and regularly returns subtitles for a completely different film (e.g. a
        # same-name collision or an entry whose feature_details has no imdb_id at
        # all). Accepting those on runtime alone is exactly how the wrong movie got
        # translated, so we now require a positive IMDb match for every non-hash
        # candidate and drop unknown/mismatched ones. A moviehash match is ground
        # truth (byte-for-byte the same video) and bypasses this filter.
        if (
            not hash_match
            and expected_imdb is not None
            and not imdb_matches
        ):
            continue

        hearing_impaired = bool(attributes.get("hearing_impaired", False))
        download_count = int(attributes.get("download_count", 0) or 0)
        release_distance = _release_distance(context, attributes)
        files = attributes.get("files", []) or []
        release_name = str(attributes.get("release") or "")
        if not release_name and files and isinstance(files[0], dict):
            release_name = str(files[0].get("file_name") or "")
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
            LOGGER.info(
                "OST candidate file_id=%s lang=%s hash_match=%s imdb_match=%s "
                "rel_dist=%s dls=%s release=%r",
                file_id, language, hash_match, imdb_matches,
                release_distance, download_count, release_name[:80],
            )
            yield score, int(file_id), attributes


def _download_subtitle_text(file_id: int) -> str:
    headers = _request_headers(for_download=True)
    headers["Content-Type"] = "application/json"
    raw = _open_url(
        f"{OPEN_SUBTITLES_BASE_URL}/download",
        method="POST",
        payload=json.dumps({"file_id": file_id, "sub_format": "srt"}).encode("utf-8"),
        headers=headers,
    )
    try:
        download_response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubtitleProviderError("Subtitle provider returned invalid JSON") from exc
    link = download_response.get("link") or download_response.get("url")
    if not link:
        raise SubtitleProviderError("Subtitle provider did not return a download link")
    link = urljoin("https://api.opensubtitles.com/", str(link))
    raw = _open_url(
        link,
        headers={"User-Agent": OPEN_SUBTITLES_USER_AGENT, "Accept": "*/*"},
    )
    return _decode_subtitle_bytes(raw)


def _cinemeta_runtime_minutes(context: Dict[str, str]) -> Optional[float]:
    """Expected runtime (minutes) from Stremio's Cinemeta metadata, if any."""
    imdb_id = context.get("imdb_id")
    if not imdb_id:
        return None
    meta_type = "series" if context.get("episode") else "movie"
    url = f"{CINEMETA_BASE_URL}/meta/{meta_type}/{imdb_id}.json"
    try:
        raw = _open_url(url, headers={"Accept": "application/json"})
        data = json.loads(raw.decode("utf-8"))
    except (SubtitleProviderError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    meta = data.get("meta") if isinstance(data, dict) else None
    if not isinstance(meta, dict):
        return None
    runtime = meta.get("runtime")
    if not runtime:
        return None
    # Cinemeta runtime is a string like "105 min" or "1 h 45 min".
    text = str(runtime)
    hours = re.search(r"(\d+)\s*h", text)
    minutes = re.search(r"(\d+)\s*min", text)
    total = 0.0
    if hours:
        total += int(hours.group(1)) * 60
    if minutes:
        total += int(minutes.group(1))
    if total == 0.0:
        plain = re.search(r"\d+", text)
        if plain:
            total = float(plain.group(0))
    return total or None


def _srt_last_minutes(text: str) -> Optional[float]:
    try:
        import srt

        subs = list(srt.parse(text))
    except Exception:
        return None
    if not subs:
        return None
    last = max(sub.end.total_seconds() for sub in subs)
    return last / 60.0


def _runtime_is_plausible(sub_text: str, expected_minutes: Optional[float]) -> bool:
    if not expected_minutes:
        return True
    last_minutes = _srt_last_minutes(sub_text)
    if last_minutes is None:
        return True
    tolerance = max(RUNTIME_TOLERANCE_MIN, expected_minutes * RUNTIME_TOLERANCE_PCT)
    # A subtitle's final cue is usually a little before the true end (credits),
    # so allow it to fall short by more than it may overshoot.
    return (expected_minutes - last_minutes) <= (tolerance + 5) and (
        last_minutes - expected_minutes
    ) <= tolerance


def _manual_source_names(context: Dict[str, str]) -> Iterable[str]:
    """Base filenames (without extension) to look for in the manual dir.

    Priority order: exact played filename, then IMDb id (with season/episode
    for series). We try common source-language suffixes and a bare name.
    """
    filename = context.get("filename", "")
    if filename:
        # Strip the media extension so "Movie.mkv" -> "Movie".
        yield re.sub(r"\.[^.]+$", "", filename)
        yield filename  # also allow the full name incl. extension

    imdb_id = context.get("imdb_id", "")
    if imdb_id:
        season = context.get("season", "")
        episode = context.get("episode", "")
        if season and episode:
            try:
                yield f"{imdb_id}.S{int(season):02d}E{int(episode):02d}"
            except (TypeError, ValueError):
                yield f"{imdb_id}.S{season}E{episode}"
        else:
            yield imdb_id


def _read_manual_source(context: Dict[str, str]) -> Optional[str]:
    """Return a manually-provided .srt for this title, if one exists.

    Looks in MANUAL_SOURCE_DIR for files matching the played filename or the
    IMDb id, with optional language suffixes, e.g.:
        manual_sources/tt0427340.srt
        manual_sources/tt0427340.en.srt
        manual_sources/Masters.Of.The.Universe.2026....WEBRip.x264.AAC.srt
    """
    if not MANUAL_SOURCE_DIR.is_dir():
        return None
    suffixes = ["", ".en", ".eng", ".english", ".ar", ".arabic"] + [
        f".{lang}" for lang in SOURCE_LANGUAGES
    ]
    for base in _manual_source_names(context):
        if not base:
            continue
        for suffix in suffixes:
            candidate = MANUAL_SOURCE_DIR / f"{base}{suffix}.srt"
            if candidate.is_file():
                try:
                    raw = candidate.read_bytes()
                except OSError as exc:
                    LOGGER.warning("Could not read manual source %s: %s", candidate, exc)
                    continue
                LOGGER.info("Using MANUAL source subtitle: %s", candidate.name)
                return _decode_subtitle_bytes(raw)
    return None


def _resolve_source_subtitle(context: Dict[str, str]) -> str:
    LOGGER.info(
        "Resolving source imdb=%s hash=%s size=%s filename=%r",
        context.get("imdb_id", ""),
        context.get("video_hash") or "-",
        context.get("video_size") or "-",
        (context.get("filename") or "")[:72],
    )
    # 1) Manual drop-in always wins: exactly the "do it myself" path.
    manual = _read_manual_source(context)
    if manual is not None:
        return manual

    if not OPEN_SUBTITLES_API_KEY:
        raise SubtitleProviderError(
            "OPEN_SUBTITLES_API_KEY is not configured on the addon server"
        )
    if not any(
        (context["video_hash"], context["imdb_id"], context["filename"])
    ):
        raise SubtitleProviderError("Stremio did not provide a video hash, IMDb ID, or filename")

    cached_source = _read_verified_source_cache(context)
    if cached_source is not None:
        return cached_source

    expected_imdb = _imdb_to_int(context.get("imdb_id", ""))
    search_response = _search_subtitles(context)
    candidates = sorted(
        _candidate_files(search_response, expected_imdb, context),
        key=lambda item: item[0],
    )
    if not candidates:
        raise SubtitleProviderError("No usable source subtitle was found")

    # IDENTITY IS RUNTIME, NOT SOURCE FAMILY.
    #
    # OpenSubtitles misfiles same-name movies under each other's IMDb id (the
    # 80-min tt41087705 "Master of the Universe" is filed under tt0427340), so
    # imdb_match cannot be trusted. The reliable signal for "is this the correct
    # movie" is the subtitle's total runtime vs the expected feature length.
    #
    # We therefore download every reasonable candidate, keep only the ones whose
    # runtime matches the expected feature length (this rejects the wrong 80-min
    # movie), and pick the best of those. Source family (WEBRip vs HDTS) only
    # affects SYNC quality, so it is a tie-breaker, NOT a reason to discard the
    # correct movie. A correct-movie HDTS beats no subtitle at all.
    expected_minutes = _cinemeta_runtime_minutes(context) if VERIFY_RUNTIME else None

    best_rank: Optional[Tuple[Any, ...]] = None
    best_text: Optional[str] = None
    best_file_id: Optional[int] = None
    loose_fallback_text: Optional[str] = None
    loose_fallback_file_id: Optional[int] = None
    saw_unverifiable = False
    attempts = 0
    for score, file_id, _attributes in candidates:
        is_hash_match = bool(_attributes.get("moviehash_match", False))
        release_distance = int(score[1]) if len(score) > 1 else 50
        language = str(_attributes.get("language", "")).lower()
        if attempts >= max(1, MAX_VERIFY_DOWNLOADS):
            break
        attempts += 1
        try:
            text = _download_subtitle_text(file_id)
        except QuotaExceededError:
            # No point trying more candidates; every download will 406 today.
            raise
        except SubtitleProviderError:
            continue
        # A movie-hash match is byte-for-byte the same video: perfect, accept it.
        if is_hash_match:
            LOGGER.info("Using moviehash-matched subtitle file_id=%s", file_id)
            _save_source_cache(context, text, file_id)
            return text

        family_mismatch = _release_family_mismatch(context, _attributes)

        if not VERIFY_RUNTIME:
            # Loose mode (verification disabled): first candidate wins, but still
            # prefer one that matches the played source family.
            if loose_fallback_text is None or not family_mismatch:
                loose_fallback_text = text
                loose_fallback_file_id = file_id
            continue

        if expected_minutes is None:
            # No runtime to verify identity against -> cannot prove correct movie.
            saw_unverifiable = True
            LOGGER.info(
                "Skipping subtitle (no runtime metadata to verify identity) file_id=%s",
                file_id,
            )
            continue

        last_minutes = _srt_last_minutes(text)
        if last_minutes is None or not _runtime_is_plausible(text, expected_minutes):
            LOGGER.info(
                "Skipping subtitle (runtime %.0f min vs expected %.0f min) file_id=%s",
                last_minutes or 0.0,
                expected_minutes,
                file_id,
            )
            continue

        # Runtime matches -> this IS the correct movie. Rank so that a source
        # that matches the played source family wins first, then prefer the
        # intended source languages (English/Arabic) over fallback sync-only
        # languages. This prevents a Greek/Turkish/Croatian workaround for one
        # bad title from taking over normal titles that have a valid English or
        # Arabic subtitle.
        rank = (
            1 if family_mismatch else 0,
            0 if language in PRIMARY_SOURCE_LANGUAGES else 1,
            release_distance,
            abs(last_minutes - expected_minutes),
        )
        LOGGER.info(
            "Runtime-verified candidate file_id=%s lang=%s last=%.0f min "
            "family_mismatch=%s primary_lang=%s rel_dist=%s",
            file_id, language, last_minutes, family_mismatch,
            language in PRIMARY_SOURCE_LANGUAGES, release_distance,
        )
        if best_rank is None or rank < best_rank:
            best_rank, best_text, best_file_id = rank, text, file_id

    if best_text is not None:
        LOGGER.info("Selected runtime-verified source file_id=%s", best_file_id)
        _save_source_cache(context, best_text, best_file_id)
        return best_text
    if loose_fallback_text is not None:
        _save_source_cache(context, loose_fallback_text, loose_fallback_file_id)
        return loose_fallback_text
    if saw_unverifiable:
        raise SubtitleProviderError(
            "Could not verify any candidate is the correct movie "
            "(no moviehash and no runtime metadata); refusing to guess"
        )
    raise SubtitleProviderError(
        "No source subtitle matched the expected movie runtime"
    )


def _token_for_context(context: Dict[str, str]) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(context, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(TOKEN_SECRET_BYTES, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _context_from_token(token: str) -> Dict[str, str]:
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(TOKEN_SECRET_BYTES, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        context = json.loads(decoded.decode("utf-8"))
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid subtitle token") from exc
    if not isinstance(context, dict):
        raise ValueError("invalid subtitle token")
    return _normalize_context({str(key): str(value or "") for key, value in context.items()})


def _context_from_token_resolved(token: str, *, mode: str = "download") -> Dict[str, str]:
    return _resolve_playback_context(_context_from_token(token), mode=mode)


def _cache_key(context: Dict[str, str]) -> str:
    """Cache key for translated outputs (invalidated on addon/hash bumps)."""
    cache_input = {
        "version": ADDON_VERSION,
        "languages": SOURCE_LANGUAGES,
        "stream": _stream_key(context),
        "video_hash": context.get("video_hash", ""),
        "video_size": context.get("video_size", ""),
    }
    return hashlib.sha256(
        json.dumps(cache_input, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _source_cache_key(context: Dict[str, str]) -> str:
    """Stable cache key for the raw OpenSubtitles source file (no addon version)."""
    cache_input = {
        "rules": SOURCE_CACHE_RULES_VERSION,
        "languages": SOURCE_LANGUAGES,
        "context": {
            key: context.get(key, "")
            for key in (
                "type",
                "imdb_id",
                "video_hash",
                "video_size",
                "filename",
                "season",
                "episode",
            )
        },
    }
    return hashlib.sha256(
        json.dumps(cache_input, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _source_paths(source_key: str) -> Tuple[Path, Path]:
    return (
        CACHE_DIR / f"{source_key}.source.srt",
        CACHE_DIR / f"{source_key}.source.meta.json",
    )


def _context_matches_source_meta(context: Dict[str, str], meta: Dict[str, Any]) -> bool:
    if meta.get("rules") != SOURCE_CACHE_RULES_VERSION:
        return False
    for field in ("imdb_id", "video_hash", "video_size", "season", "episode", "type"):
        expected = str(context.get(field, "") or "")
        cached = str(meta.get(field, "") or "")
        if expected and cached and expected != cached:
            return False
    expected_imdb = _imdb_to_int(context.get("imdb_id", ""))
    cached_imdb = _imdb_to_int(str(meta.get("imdb_id", "")))
    if expected_imdb and cached_imdb and expected_imdb != cached_imdb:
        return False
    if context.get("video_hash") and meta.get("video_hash"):
        if str(context["video_hash"]).lower() != str(meta["video_hash"]).lower():
            return False
    return True


def _read_verified_source_cache(context: Dict[str, str]) -> Optional[str]:
    """Return a previously downloaded OpenSubtitles source if it still matches."""
    source_key = _source_cache_key(context)
    source_path, meta_path = _source_paths(source_key)
    text = _read_cached_text(source_path)
    if text is None:
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.info("Dropping source cache %s (missing/invalid metadata)", source_key[:8])
        return None
    if not isinstance(meta, dict) or not _context_matches_source_meta(context, meta):
        LOGGER.info("Dropping source cache %s (context mismatch)", source_key[:8])
        return None
    if VERIFY_RUNTIME:
        expected_minutes = _cinemeta_runtime_minutes(context)
        if expected_minutes and not _runtime_is_plausible(text, expected_minutes):
            LOGGER.info("Dropping source cache %s (runtime mismatch)", source_key[:8])
            return None
    LOGGER.info("Reusing cached OpenSubtitles source for %s (no download)", source_key[:8])
    return text


def _save_source_cache(context: Dict[str, str], text: str, file_id: Optional[int] = None) -> None:
    source_key = _source_cache_key(context)
    source_path, meta_path = _source_paths(source_key)
    _atomic_write(source_path, text)
    meta = {
        "rules": SOURCE_CACHE_RULES_VERSION,
        "imdb_id": context.get("imdb_id", ""),
        "video_hash": context.get("video_hash", ""),
        "video_size": context.get("video_size", ""),
        "filename": context.get("filename", ""),
        "season": context.get("season", ""),
        "episode": context.get("episode", ""),
        "type": context.get("type", ""),
        "file_id": file_id,
        "cached_at": time.time(),
    }
    try:
        meta_path.write_text(
            json.dumps(meta, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        LOGGER.warning("Could not persist source cache metadata: %s", meta_path)


def _read_cached_text(path: Path) -> Optional[str]:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        LOGGER.warning("Could not read addon cache file: %s", path)
    return None


def _atomic_write(path: Path, content: str) -> None:
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(CACHE_DIR), delete=False
        ) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        os.replace(str(temp_path), str(path))
    except OSError:
        LOGGER.warning("Could not persist addon cache file: %s", path)


def _cleanup_cache_once() -> None:
    """Delete cached subtitles older than the TTL, then enforce a size cap."""
    try:
        files = [p for p in CACHE_DIR.glob("*.srt") if p.is_file()]
        meta_files = {p for p in CACHE_DIR.glob("*.source.meta.json") if p.is_file()}
    except OSError:
        LOGGER.warning("Could not scan addon cache directory: %s", CACHE_DIR)
        return

    now = time.time()
    entries = []
    removed = 0
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        age_days = (now - stat.st_mtime) / 86400.0
        if CACHE_TTL_DAYS > 0 and age_days > CACHE_TTL_DAYS:
            try:
                path.unlink()
                removed += 1
                if path.name.endswith(".source.srt"):
                    meta_path = path.with_name(path.name.replace(".srt", ".meta.json"))
                    if meta_path.is_file():
                        meta_path.unlink()
                        meta_files.discard(meta_path)
                continue
            except OSError:
                LOGGER.warning("Could not delete expired cache file: %s", path)
        entries.append((stat.st_mtime, stat.st_size, path))

    if CACHE_MAX_MB > 0:
        total_bytes = sum(size for _, size, _ in entries)
        cap_bytes = int(CACHE_MAX_MB * 1024 * 1024)
        if total_bytes > cap_bytes:
            # Evict oldest first until under the cap.
            for _mtime, size, path in sorted(entries, key=lambda item: item[0]):
                if total_bytes <= cap_bytes:
                    break
                try:
                    path.unlink()
                    total_bytes -= size
                    removed += 1
                    if path.name.endswith(".source.srt"):
                        meta_path = path.with_name(path.name.replace(".srt", ".meta.json"))
                        if meta_path.is_file():
                            meta_path.unlink()
                            meta_files.discard(meta_path)
                except OSError:
                    LOGGER.warning("Could not delete cache file for size cap: %s", path)

    if removed:
        LOGGER.info("Addon cache cleanup removed %d file(s)", removed)


def _start_cache_cleanup() -> None:
    global _CLEANUP_STARTED
    with _CLEANUP_GUARD:
        if _CLEANUP_STARTED:
            return
        _CLEANUP_STARTED = True

    def _loop() -> None:
        interval = max(0.5, CACHE_CLEAN_INTERVAL_HOURS) * 3600.0
        while True:
            _cleanup_cache_once()
            time.sleep(interval)

    threading.Thread(target=_loop, name="streamio-cache-cleanup", daemon=True).start()


class TranslationJob:
    """Background Google + Groq translation for one Stremio subtitle request."""

    def __init__(self, stream_id: str, context: Dict[str, str]):
        self.stream_id = stream_id
        self.context = context
        self.key = _cache_key(context)
        self.source_key = _source_cache_key(context)
        self.progress: Dict[str, Any] = {}
        self.condition = threading.Condition()
        self.error: Optional[Exception] = None
        self._started = False
        self._start_lock = threading.Lock()
        self.google_path = CACHE_DIR / f"{self.key}.google.srt"
        self.polished_path = CACHE_DIR / f"{self.key}.srt"

    def sync_context(self, context: Dict[str, str], process_translation) -> None:
        """Upgrade metadata when Stremio sends a richer follow-up request."""
        merged = _merge_context(self.context, context)
        if _context_score(merged) <= _context_score(self.context):
            self.context = merged
            return
        old_key = self.key
        self.context = merged
        self.source_key = _source_cache_key(merged)
        self.key = _cache_key(merged)
        self.google_path = CACHE_DIR / f"{self.key}.google.srt"
        self.polished_path = CACHE_DIR / f"{self.key}.srt"
        if self._started and self.key != old_key:
            LOGGER.info(
                "Restarting translation for stream=%s (hash now %s)",
                self.stream_id[:8],
                merged.get("video_hash") or "-",
            )
            self._started = False
            self.progress = {}
            self.error = None
            self.ensure_started(process_translation)

    def _notify(self, progress: Dict[str, Any]) -> None:
        with self.condition:
            self.progress = progress
            self.condition.notify_all()

    def ensure_started(self, process_translation) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            threading.Thread(
                target=self._run,
                args=(process_translation,),
                name=f"streamio-job-{self.key[:8]}",
                daemon=True,
            ).start()

    def _run(self, process_translation) -> None:
        progress: Dict[str, Any] = {}
        try:
            polished = _read_cached_text(self.polished_path)
            if polished is not None:
                google = _read_cached_text(self.google_path) or polished
                progress.update(
                    {
                        "google_status": "completed",
                        "google_progress": 100,
                        "google_result": google,
                        "ai_status": "completed",
                        "ai_progress": 100,
                        "ai_result": polished,
                    }
                )
                self._notify(progress)
                return

            google_cached = _read_cached_text(self.google_path)
            if google_cached is not None:
                progress.update(
                    {
                        "google_status": "completed",
                        "google_progress": 100,
                        "google_result": google_cached,
                        "ai_status": "running",
                        "ai_progress": 0,
                    }
                )
                self._notify(progress)

            import srt

            source_text = _resolve_source_subtitle(self.context)
            subtitles = list(srt.parse(source_text))
            if not subtitles:
                raise SubtitleProviderError("The source subtitle file is empty or invalid")

            def on_progress(updated: Dict[str, Any]) -> None:
                if (
                    updated.get("google_status") == "completed"
                    and updated.get("google_result")
                    and not self.google_path.is_file()
                ):
                    _atomic_write(self.google_path, str(updated["google_result"]))
                self._notify(updated)

            process_translation(subtitles, progress, on_progress=on_progress)
            if progress.get("ai_status") != "completed" or not progress.get("ai_result"):
                raise SubtitleProviderError("The Hebrew translation pipeline did not complete")
            _atomic_write(self.polished_path, str(progress["ai_result"]))
            self._notify(progress)
        except Exception as exc:
            LOGGER.exception("Streamio background translation failed for %s", self.key[:8])
            self.error = exc
            self._notify(progress)


def _get_job(
    context: Dict[str, str],
    process_translation,
    *,
    mode: str = "download",
) -> TranslationJob:
    # Callers pass an already stream-resolved context (via
    # _resolve_playback_context / _context_from_token_resolved), so we only
    # record it here rather than waiting again.
    context = _remember_context(context)
    stream_id = _stream_key(context)
    with _JOBS_GUARD:
        job = _JOBS.get(stream_id)
        if job is None:
            job = TranslationJob(stream_id, context)
            _JOBS[stream_id] = job
        else:
            job.sync_context(context, process_translation)
    job.ensure_started(process_translation)
    return job


def _peek_job(context: Dict[str, str]) -> Optional[TranslationJob]:
    """Return an existing job for status/labels without starting work."""
    stream_id = _stream_key(context)
    with _JOBS_GUARD:
        return _JOBS.get(stream_id)


def _wait_for_phase(
    job: TranslationJob,
    *,
    status_key: str,
    result_key: str,
    cache_path: Path,
    timeout: int,
) -> str:
    cached = _read_cached_text(cache_path)
    if cached is not None:
        return cached

    deadline = time.time() + timeout
    with job.condition:
        while True:
            if job.error is not None:
                raise SubtitleProviderError(str(job.error))
            if job.progress.get(status_key) == "completed" and job.progress.get(result_key):
                return str(job.progress[result_key])
            cached = _read_cached_text(cache_path)
            if cached is not None:
                return cached
            remaining = deadline - time.time()
            if remaining <= 0:
                raise SubtitleProviderError("Translation timed out")
            job.condition.wait(timeout=min(1.0, remaining))


def _polished_ready(job: TranslationJob) -> bool:
    return job.polished_path.is_file() or job.progress.get("ai_status") == "completed"


# CRITICAL: `lang` MUST be a valid ISO code, or TV/Android drops the track
# entirely (the addon "disappears" from the picker). So `lang` stays "heb".
# `label` carries the readable name for clients that honor it (desktop/web).
HEBREW_LANG_CODE = "heb"


def _google_label(job: TranslationJob) -> str:
    if _polished_ready(job):
        return "Hebrew FAST (Google)"
    pct = job.progress.get("ai_progress", 0)
    if job.progress.get("google_status") == "completed" or job.google_path.is_file():
        return f"Hebrew FAST (AI {pct}%)"
    return "Hebrew FAST (Google)"


def _polished_label(job: TranslationJob) -> str:
    return "Hebrew AI (polished)"


def _subtitle_list_cache_max_age(job: TranslationJob) -> int:
    if _polished_ready(job):
        return 3600
    # Keep this short so reopening the subtitle menu re-queries the server and
    # the polished track pops in as soon as it is ready.
    return int(_env("STREAMIO_LIST_CACHE_SECONDS", "15", "STREMIO_LIST_CACHE_SECONDS"))


def _job_status_payload(job: TranslationJob) -> Dict[str, Any]:
    return {
        "google_status": job.progress.get("google_status", "pending"),
        "google_progress": job.progress.get("google_progress", 0),
        "google_ready": job.google_path.is_file()
        or job.progress.get("google_status") == "completed",
        "ai_status": job.progress.get("ai_status", "pending"),
        "ai_progress": job.progress.get("ai_progress", 0),
        "polished_ready": job.polished_path.is_file()
        or job.progress.get("ai_status") == "completed",
        "error": str(job.error) if job.error else None,
    }


def _srt_response(content: str, filename: str) -> Response:
    return Response(
        content,
        status=200,
        mimetype="application/x-subrip",
        headers={
            "Content-Disposition": f"inline; filename={filename}",
            "Cache-Control": "public, max-age=86400",
        },
    )


def _handle_subtitle_errors(handler):
    def wrapper(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except ValueError:
            return Response("Invalid subtitle URL", status=400, mimetype="text/plain")
        except SubtitleProviderError as exc:
            LOGGER.warning("Streamio subtitle request failed: %s", exc)
            return Response(str(exc), status=502, mimetype="text/plain")
        except Exception:
            LOGGER.exception("Unexpected Streamio subtitle failure")
            return Response("Subtitle translation failed", status=500, mimetype="text/plain")

    wrapper.__name__ = handler.__name__
    return wrapper


def _public_url(path: str) -> str:
    base = PUBLIC_BASE_URL or request.url_root.rstrip("/")
    return f"{base}{path}"


def register_streamio_routes(flask_app, process_translation) -> None:
    """Register Stremio protocol and health routes on the existing Flask app."""

    _start_cache_cleanup()

    # Stremio's desktop app and Stremio Web load addons in a browser/Electron
    # context that enforces CORS. Without these headers the manifest and
    # subtitle endpoints are blocked on PC/Web (while the native mobile app,
    # which ignores CORS, still works). Setting a permissive policy on every
    # response is the standard Stremio addon requirement.
    @flask_app.after_request
    def _streamio_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    if OPEN_SUBTITLES_USERNAME and OPEN_SUBTITLES_PASSWORD:
        try:
            _ost_bearer_token()
        except SubtitleProviderError as exc:
            LOGGER.warning("OpenSubtitles login failed at startup: %s", exc)

    manifest = {
        "id": "org.amit.hebrew-ai-subtitles",
        "version": ADDON_VERSION,
        "name": "Hebrew AI",
        "description": (
            "Google baseline plus Groq contextual Hebrew subtitle translation. "
            "Created by Amit Cederbaum."
        ),
        "contactEmail": "amitceder@gmail.com",
        "resources": [
            {
                "name": "subtitles",
                "types": ["movie", "series"],
                "idPrefixes": ["tt"],
            }
        ],
        "types": ["movie", "series"],
        "catalogs": [],
        "behaviorHints": {"configurable": False, "adult": False},
    }

    @flask_app.get("/manifest.json")
    def streamio_manifest():
        return jsonify(manifest)

    @flask_app.get("/healthz")
    def streamio_health():
        return jsonify(
            {
                "status": "ok",
                "streamio": True,
                "addon_version": ADDON_VERSION,
                "open_subtitles_configured": bool(OPEN_SUBTITLES_API_KEY),
                "open_subtitles_auth_configured": bool(
                    OPEN_SUBTITLES_USERNAME and OPEN_SUBTITLES_PASSWORD
                ),
                "open_subtitles_logged_in": bool(
                    _OST_TOKEN or _OST_SESSION_FILE.is_file()
                ),
                "groq_configured": bool(os.environ.get("GROQ_API_KEY")),
                "public_base_url_configured": bool(PUBLIC_BASE_URL),
            }
        )

    @flask_app.get("/subtitles/<content_type>/<path:subtitle_id>.json")
    def streamio_subtitles(content_type: str, subtitle_id: str):
        if content_type not in {"movie", "series"}:
            return jsonify({"subtitles": []})

        context = _request_context(content_type, subtitle_id)
        if not any((context["video_hash"], context["imdb_id"], context["filename"])):
            return jsonify({"subtitles": []})

        # Merge/record this request. We ALWAYS advertise the track so the addon
        # never disappears from the picker (TV clients drop addons that return
        # an empty list). The token encodes the stream identity; the actual
        # videoHash is merged in at download time from the shared stream store
        # (Stremio sends a separate hash-bearing request for the same stream).
        context = _resolve_playback_context(context, mode="list")

        token = _token_for_context(context)
        job = _peek_job(context)
        subtitle_hash = hashlib.sha256(token.encode("ascii")).hexdigest()[:12]

        # lang MUST stay a valid ISO code ("heb") so the track shows on TV.
        # label carries the readable name for clients that honor it.
        subtitles = [
            {
                "id": f"hebrew-google-{subtitle_hash}",
                "url": _public_url(f"/streamio-subtitle/{token}/google.srt"),
                "lang": HEBREW_LANG_CODE,
                "label": _google_label(job) if job else "Hebrew FAST (Google)",
            }
        ]
        # Only advertise the polished track once it is actually ready, so that
        # its presence in the list means "the AI version is available now".
        if job and _polished_ready(job):
            subtitles.append(
                {
                    "id": f"hebrew-polished-{subtitle_hash}",
                    "url": _public_url(f"/streamio-subtitle/{token}/polished.srt"),
                    "lang": HEBREW_LANG_CODE,
                    "label": _polished_label(job),
                }
            )
        return jsonify(
            {
                "subtitles": subtitles,
                "cacheMaxAge": _subtitle_list_cache_max_age(job) if job else 15,
            }
        )

    @flask_app.get("/streamio-subtitle/<path:token>/status.json")
    def streamio_subtitle_status(token: str):
        try:
            context = _context_from_token_resolved(token, mode="download")
        except ValueError:
            return jsonify({"error": "invalid subtitle token"}), 400
        job = _get_job(context, process_translation, mode="download")
        return jsonify(_job_status_payload(job))

    @flask_app.get("/streamio-subtitle/<path:token>/google.srt")
    @_handle_subtitle_errors
    def streamio_subtitle_google(token: str):
        context = _context_from_token_resolved(token, mode="download")
        job = _get_job(context, process_translation, mode="download")
        text = _wait_for_phase(
            job,
            status_key="google_status",
            result_key="google_result",
            cache_path=job.google_path,
            timeout=GOOGLE_WAIT_TIMEOUT,
        )
        return _srt_response(text, "hebrew-google.srt")

    @flask_app.get("/streamio-subtitle/<path:token>/polished.srt")
    @_handle_subtitle_errors
    def streamio_subtitle_polished(token: str):
        context = _context_from_token_resolved(token, mode="download")
        job = _get_job(context, process_translation, mode="download")
        polished = _read_cached_text(job.polished_path)
        if polished is not None:
            return _srt_response(polished, "hebrew-ai-polished.srt")
        # Not ready yet: wait briefly, then fall back to Google so the player
        # never stalls waiting for the full polish to finish.
        try:
            text = _wait_for_phase(
                job,
                status_key="ai_status",
                result_key="ai_result",
                cache_path=job.polished_path,
                timeout=int(_env("STREAMIO_POLISHED_SELECT_WAIT", "45", "STREMIO_POLISHED_SELECT_WAIT")),
            )
            return _srt_response(text, "hebrew-ai-polished.srt")
        except SubtitleProviderError:
            google = _read_cached_text(job.google_path)
            if google is not None:
                return _srt_response(google, "hebrew-google.srt")
            raise

    @flask_app.get("/streamio-subtitle/<token>.srt")
    @_handle_subtitle_errors
    def streamio_subtitle_file(token: str):
        context = _context_from_token_resolved(token, mode="download")
        job = _get_job(context, process_translation, mode="download")
        polished = _read_cached_text(job.polished_path)
        if polished is not None:
            return _srt_response(polished, "hebrew-ai-polished.srt")
        text = _wait_for_phase(
            job,
            status_key="google_status",
            result_key="google_result",
            cache_path=job.google_path,
            timeout=GOOGLE_WAIT_TIMEOUT,
        )
        return _srt_response(text, "hebrew-google.srt")
