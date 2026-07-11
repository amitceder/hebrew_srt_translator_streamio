import os
import sys
import srt
import logging
import time
import threading
import re
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory
from groq import Groq
from deep_translator import GoogleTranslator

# Configure Logging
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Initialize API Clients
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# Model selection:
#   openai/gpt-oss-20b      ~1000 t/s  (default, best speed/quality balance)
#   meta-llama/llama-4-scout-17b-16e-instruct  ~750 t/s
#   llama-3.1-8b-instant    ~560 t/s   (fastest, slightly lower quality)
MODEL_NAME = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

# How many AI polish batches to run in parallel (higher = faster, but watch Groq rate limits)
AI_PARALLEL_WORKERS = int(os.environ.get("AI_PARALLEL_WORKERS", "10"))

# Max output tokens per batch.
# Each Hebrew subtitle line is ~20-60 chars. With 20-line batches, 2048 is safe.
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "2048"))

# AI batch size: number of subtitle lines sent per Groq request.
# Larger = more context for the AI = better gender/tone consistency.
# 20 is a good balance; 30 for long files if you have the token budget.
AI_BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "20"))

# Context overlap: how many lines from the previous batch to prepend as
# read-only context (not re-translated, just for continuity).
AI_CONTEXT_OVERLAP = int(os.environ.get("AI_CONTEXT_OVERLAP", "3"))

# How many times to retry a failed/incomplete batch before falling back to Google
AI_MAX_RETRIES = int(os.environ.get("AI_MAX_RETRIES", "2"))

# Google: max 5000 chars per request; we chunk and run batches in parallel
GOOGLE_MAX_CHARS = 4500
GOOGLE_PARALLEL_WORKERS = int(os.environ.get("GOOGLE_PARALLEL_WORKERS", "6"))

# ---------------------------------------------------------------------------
# SYSTEM PROMPT — v3
# Key changes vs v2:
#   • ORIG (English) is now the PRIMARY translation source — not Google
#   • Google is ONLY used as a secondary reference for nikud-free phrasing hints
#   • Explicit nikud (diacritics) removal rule
#   • Explicit [Name] tag preservation — never translate bracket names as words
#   • "right" / "all right" / "okay" disambiguation rule added
#   • Stronger mandate: translate from English, don't just validate Google
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an expert Hebrew subtitle translator working on a professional streaming production.

You receive each subtitle as:
  ID: the subtitle number
  ORIG: the ORIGINAL English (or Arabic) line — this is the authoritative source
  GOOGLE: a rough Google machine translation to Hebrew — use ONLY as a phrasing hint

══════════════════════════════════════════════
CORE MANDATE
══════════════════════════════════════════════
• Translate from ORIG (the English/Arabic original). Do NOT simply copy or validate Google.
• Google is often wrong: bad gender, wrong word sense, awkward phrasing, diacritics (nikud).
• Every subtitle must be a fresh, natural Hebrew translation — not a Google rubber-stamp.
• Ask yourself: "What would a native Hebrew speaker naturally say here?" — write that.

══════════════════════════════════════════════
OUTPUT FORMAT — STRICTLY ENFORCED
══════════════════════════════════════════════
• Output ONLY: ID: <number>\nTEXT: <hebrew text>
• NEVER add: "Note:", "I assumed", "I corrected", "Translator:", or ANY commentary.
• NEVER omit a subtitle. Every input ID must appear in your output.
• Multi-line subtitles: preserve the line break with a literal newline inside TEXT.

══════════════════════════════════════════════
CRITICAL TRANSLATION RULES
══════════════════════════════════════════════

NIKUD (DIACRITICS) — FORBIDDEN
• NEVER output Hebrew diacritics (nikud): ָ ֵ ִ ֹ ֻ ּ ְ etc.
• Google often adds nikud — always strip them. Subtitles must use plain Hebrew letters only.

NAMES IN [BRACKETS] — NEVER TRANSLATE
• [Boyd], [Ellis], [Jade], [Henry], [Sara], [Fatima], [Acosta], [Victor] etc. are CHARACTER NAMES.
• Keep the name in Hebrew transliteration inside the bracket: [בויד], [אליס], [ג'ייד] etc.
• NEVER translate a bracketed name as a common word. [Boyd] ≠ [בחור]. [Ellis] ≠ [אליס the word].

WORD SENSE / CONTEXT — USE ENGLISH TO DISAMBIGUATE
• "right" in English has multiple meanings — always check ORIG context:
  - Confirmation/agreement ("Right.", "All right.", "That's right") → בסדר / נכון / אוקיי
  - Direction ("turn right", "right over there") → ימינה / מימין
  - Emphasis ("right now", "right here") → עכשיו / ממש כאן
• "just" → רק / בדיוק / פשוט (context-dependent — never auto-translate)
• "get" → לקבל / לתפוס / להבין / לקחת (check ORIG)
• Sound/action cues in [brackets]: translate to natural Hebrew — [sobbing]→[בוכה בדמעות], [grunts]→[נאנח], [screaming]→[צועק/ת]

GENDER
• Infer speaker gender from [Name] tags or context in ORIG.
• All verbs, adjectives, address forms must match the speaker's gender.

REGISTER & NATURALNESS
• Casual speech → colloquial Israeli Hebrew. Formal speech → formal Hebrew.
• Profanity: use Hebrew phonetic equivalents (שיט, פאק, לעזאזל, בן זונה).
• Idioms → natural Hebrew equivalent, not literal translation.
• Arabic colloquialisms → natural Hebrew phonetic or common Israeli equivalent.

FORMATTING TAGS
• Preserve ALL formatting tags unchanged: <i>...</i>, {\\an8}, <font ...>
• Apply them in the same position in the Hebrew output.

PUNCTUATION
• Sentence-ending punctuation (. ? ! …) at the END of the string.
• Ellipsis for trailing/interrupted speech: …
• Em-dash for abrupt cuts: —
• Max ~42 chars/line, max 2 lines per subtitle.

CONSISTENCY
• Keep character names, key terms, and recurring phrases consistent across the batch.
• Context lines (CONTEXT:) are for reference only — do NOT output them.
"""

# In-memory storage for translation jobs
jobs = {}


def _detect_lang_hint(subs_sample):
    """Quick heuristic for the AI prompt's source-language hint."""
    sample_text = " ".join(s.content for s in subs_sample[:20])
    greek_chars = sum(1 for c in sample_text if '\u0370' <= c <= '\u03FF')
    arabic_chars = sum(1 for c in sample_text if '\u0600' <= c <= '\u06FF')
    lower = sample_text.lower()
    turkish_chars = sum(1 for c in lower if c in "çğıöşü")
    croatian_chars = sum(1 for c in lower if c in "čćđšž")
    length = max(len(sample_text), 1)
    if arabic_chars / length > 0.15:
        return "Arabic"
    if greek_chars / length > 0.15:
        return "Greek"
    if turkish_chars / length > 0.02:
        return "Turkish"
    if croatian_chars / length > 0.02:
        return "Croatian"
    return "English or auto-detected"


def _clean_polished_text(text):
    """Remove AI meta-notes (Note:, I assumed, etc.) from polished subtitle text."""
    if not text or not text.strip():
        return text
    drop_phrases = ("note:", "i assumed", "i corrected", "added ", "might not need", "this line seems",
                    "translator:", "translation note:", "context:")
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        low = line.strip().lower()
        if any(low.startswith(p) for p in ("note:", "translator:", "translation note:")):
            continue
        if any(p in low for p in drop_phrases[2:]):
            continue
        for sep in (" Note:", " note:", "\tNote:", "\tnote:", " - Note:", " - note:"):
            if sep in line:
                line = line.split(sep)[0].rstrip()
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _strip_nikud(text):
    """Remove Hebrew diacritics (nikud/vowel marks) from text."""
    if not text:
        return text
    # Unicode range for Hebrew points: U+05B0-U+05C7
    return re.sub(r'[\u05b0-\u05c7]', '', text)


def _build_batch_prompt(batch, context_subs, orig_subs_map, lang_hint):
    """
    Build the user prompt for one batch.
    context_subs: read-only preceding subs shown for continuity (not to be re-translated).
    orig_subs_map: dict of index -> original (pre-Google) subtitle content.
    lang_hint: "English" or "Arabic"
    """
    lines = [f"SOURCE LANGUAGE: {lang_hint}\n"]
    lines.append("IMPORTANT: Translate from the ORIG field. Google is only a secondary hint.\n")

    if context_subs:
        lines.append("CONTEXT (for continuity — do NOT output these):")
        for s in context_subs:
            orig_ctx = orig_subs_map.get(s.index, s.content)
            lines.append(f"  [{s.index}] {orig_ctx}")
        lines.append("")

    lines.append("TRANSLATE THESE (output ID: / TEXT: for each):")
    for s in batch:
        orig = orig_subs_map.get(s.index, "")
        google_hint = _strip_nikud(s.content)
        lines.append(f"ID: {s.index}")
        lines.append(f"ORIG: {orig}")
        lines.append(f"GOOGLE (hint only): {google_hint}")
        lines.append("")

    return "\n".join(lines)


def _parse_ai_response(response_text, batch):
    """Parse ID/TEXT blocks from AI response. Returns dict of index->text."""
    # Robust: handle both \n and \r\n, optional spaces around colon
    results = re.findall(r"ID\s*:\s*(\d+)\s*\nTEXT\s*:\s*(.*?)(?=\nID\s*:|\Z)", response_text, re.DOTALL)
    out = {}
    for rid, res_content in results:
        raw = res_content.strip()
        cleaned = _clean_polished_text(raw)
        final = _strip_nikud(cleaned if cleaned else raw)
        out[int(rid)] = final
    return out


def _polish_one_batch(batch, context_subs, orig_subs_map, lang_hint, retries=None):
    """
    Call Groq for one batch of subtitles.
    Returns list of (srt_index, polished_text).
    Retries on partial parse up to AI_MAX_RETRIES times.
    """
    if retries is None:
        retries = AI_MAX_RETRIES

    prompt = _build_batch_prompt(batch, context_subs, orig_subs_map, lang_hint)
    expected_ids = {s.index for s in batch}

    for attempt in range(retries + 1):
        try:
            chat_completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                model=MODEL_NAME,
                temperature=0.2,          # lower = more deterministic, fewer hallucinations
                max_tokens=AI_MAX_TOKENS,
            )
            response_text = chat_completion.choices[0].message.content or ""
            parsed = _parse_ai_response(response_text, batch)

            missing = expected_ids - set(parsed.keys())
            if not missing:
                # Full parse — success
                return [(idx, text) for idx, text in parsed.items() if idx in expected_ids]

            if attempt < retries:
                # Partial parse: retry only the missing IDs
                missing_batch = [s for s in batch if s.index in missing]
                logging.warning("Batch partial parse — %d missing IDs, retrying (attempt %d/%d): %s",
                                len(missing), attempt + 1, retries, sorted(missing))
                retry_results = _polish_one_batch(missing_batch, context_subs, orig_subs_map,
                                                  lang_hint, retries=0)
                # Merge
                retry_map = dict(retry_results)
                parsed.update(retry_map)
                return [(idx, text) for idx, text in parsed.items() if idx in expected_ids]
            else:
                # Give up on missing — keep Google for them, warn
                logging.warning("Giving up on %d IDs, keeping Google baseline: %s", len(missing), sorted(missing))
                out = [(idx, text) for idx, text in parsed.items() if idx in expected_ids]
                out += [(s.index, s.content) for s in batch if s.index in missing]
                return out

        except Exception as e:
            logging.error("AI Batch Error (attempt %d/%d): %s", attempt + 1, retries + 1, e)
            if attempt == retries:
                return [(s.index, s.content) for s in batch]  # fall back to Google
            time.sleep(1.5 * (attempt + 1))  # brief backoff before retry

    return [(s.index, s.content) for s in batch]


def _notify(progress, on_progress):
    if callable(on_progress):
        on_progress(progress)


def _google_translate_one_batch(batch_subs):
    """Translate one batch of subs (combined with |||). Thread-safe."""
    if not batch_subs:
        return 0
    combined = "\n ||| \n".join([s.content for s in batch_subs])
    try:
        translator = GoogleTranslator(source='auto', target='iw')
        translated_block = translator.translate(combined)
    except Exception as e:
        logging.warning("Google Batch Error: %s", e)
        return len(batch_subs)
    parts = translated_block.split("|||")
    for idx, t_line in enumerate(parts):
        if idx < len(batch_subs):
            batch_subs[idx].content = _strip_nikud(t_line.strip())
    return len(batch_subs)


def process_translation(subs, progress=None, on_progress=None):
    """Run Google baseline + AI polish. Updates progress dict."""
    if progress is None:
        progress = {}
    progress.setdefault('google_status', 'running')
    progress.setdefault('google_progress', 0)
    progress.setdefault('ai_status', 'running')
    progress.setdefault('ai_progress', 0)
    _notify(progress, on_progress)

    try:
        total = len(subs)

        # Save original (pre-Google) content for AI context
        orig_subs_map = {s.index: s.content for s in subs}

        # Detect source language from original content
        lang_hint = _detect_lang_hint(subs)
        logging.info("Detected source language hint: %s", lang_hint)

        # ── PHASE 1: Google Translate (parallel) ─────────────────────────────
        logging.info("Starting Google Phase (workers=%s, max %s chars/batch)",
                     GOOGLE_PARALLEL_WORKERS, GOOGLE_MAX_CHARS)
        google_batches = []
        current, current_len = [], 0
        for s in subs:
            add = len(s.content) + 5
            if current_len + add > GOOGLE_MAX_CHARS and current:
                google_batches.append(current)
                current, current_len = [], 0
            current.append(s)
            current_len += add
        if current:
            google_batches.append(current)

        done = 0
        with ThreadPoolExecutor(max_workers=GOOGLE_PARALLEL_WORKERS) as executor:
            futures = {executor.submit(_google_translate_one_batch, b): b for b in google_batches}
            for future in as_completed(futures):
                done += future.result()
                progress['google_progress'] = int(min(done, total) / total * 100)
                _notify(progress, on_progress)

        progress['google_status'] = 'completed'
        progress['google_result'] = srt.compose(subs)
        _notify(progress, on_progress)

        # ── PHASE 2: AI Contextual Polish (parallel, with overlap context) ───
        logging.info("Starting AI Phase (workers=%s, batch=%s, overlap=%s, model=%s)",
                     AI_PARALLEL_WORKERS, AI_BATCH_SIZE, AI_CONTEXT_OVERLAP, MODEL_NAME)

        # Build batches with context overlap
        # Each entry: (batch_subs, context_subs)
        batched_work = []
        for i in range(0, total, AI_BATCH_SIZE):
            batch = subs[i:i + AI_BATCH_SIZE]
            # Context: the AI_CONTEXT_OVERLAP lines immediately before this batch
            context_start = max(0, i - AI_CONTEXT_OVERLAP)
            context_subs = subs[context_start:i]
            batched_work.append((batch, context_subs))

        index_to_sub = {s.index: s for s in subs}

        with ThreadPoolExecutor(max_workers=AI_PARALLEL_WORKERS) as executor:
            future_map = {
                executor.submit(_polish_one_batch, batch, ctx, orig_subs_map, lang_hint): batch
                for batch, ctx in batched_work
            }
            completed = 0
            num_batches = len(batched_work)
            for future in as_completed(future_map):
                for idx, content in future.result():
                    if idx in index_to_sub:
                        index_to_sub[idx].content = content
                completed += 1
                progress['ai_progress'] = int(completed / num_batches * 100)
                _notify(progress, on_progress)

        progress['ai_result'] = srt.compose(subs)
        progress['ai_status'] = 'completed'
        _notify(progress, on_progress)

    except Exception as e:
        logging.exception("System Error: %s", e)
        progress['ai_status'] = 'error'
        _notify(progress, on_progress)


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/config')
def config():
    """Expose AI engine config for the frontend."""
    return jsonify({
        "groq_model": MODEL_NAME,
        "ai_batch_size": AI_BATCH_SIZE,
        "ai_context_overlap": AI_CONTEXT_OVERLAP,
        "ai_parallel_workers": AI_PARALLEL_WORKERS,
    })


@app.route('/translate', methods=['POST'])
def translate():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file"}), 400
        file = request.files['file']
        content = file.read().decode('utf-8').replace('\ufeff', '')
        subs = list(srt.parse(content))
        job_id = str(int(time.time()))
        jobs[job_id] = {
            'google_status': 'running', 'google_progress': 0, 'google_result': None,
            'ai_progress': 0, 'ai_status': 'running', 'ai_result': None
        }
        threading.Thread(target=process_translation, args=(subs, jobs[job_id])).start()
        return jsonify({"job_id": job_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/status/<job_id>')
def status(job_id):
    return jsonify(jobs.get(job_id, {"error": "Not found"}))


def _cli_progress(progress):
    """Print a single updating line for Phase 1 and Phase 2 progress."""
    g = progress.get('google_progress', 0)
    a = progress.get('ai_progress', 0)
    g_status = progress.get('google_status', '')
    a_status = progress.get('ai_status', '')
    line = "Phase 1 (Google): %3d%% %s  |  Phase 2 (AI Polish): %3d%% %s" % (
        g, "✓" if g_status == 'completed' else "",
        a, "✓" if a_status == 'completed' else "",
    )
    sys.stdout.write("\r" + line + "   ")
    sys.stdout.flush()
    if a_status in ('completed', 'error'):
        sys.stdout.write("\n")
        sys.stdout.flush()


def _cmd_translate(args):
    input_path = os.path.abspath(args.input)
    if not os.path.isfile(input_path):
        sys.stderr.write("Error: not a file: %s\n" % input_path)
        sys.exit(1)
    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        base, ext = os.path.splitext(input_path)
        output_path = base + "_AI_POLISHED" + (ext or ".srt")
    with open(input_path, "r", encoding="utf-8") as f:
        content = f.read().replace("\ufeff", "")
    subs = list(srt.parse(content))
    progress = {}
    print("Translating %d subtitles → %s" % (len(subs), output_path))
    process_translation(subs, progress, on_progress=_cli_progress)
    if progress.get("ai_status") != "completed":
        sys.stderr.write("Translation failed (ai_status=%s).\n" % progress.get("ai_status"))
        sys.exit(1)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(progress["ai_result"])
    print("Saved: %s" % output_path)
    if args.literal and progress.get("google_result"):
        literal_path = (os.path.splitext(output_path)[0].replace("_AI_POLISHED", "_GOOGLE")
                        + os.path.splitext(output_path)[1])
        with open(literal_path, "w", encoding="utf-8") as f:
            f.write(progress["google_result"])
        print("Saved (literal): %s" % literal_path)


def _cmd_serve(args):
    port = int(args.port)
    app.run(host="0.0.0.0", debug=args.debug, port=port)


from streamio_addon import register_streamio_routes

register_streamio_routes(app, process_translation)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Hybrid SRT Translator (Google + Groq AI polish)")
    sub = parser.add_subparsers(dest="command", help="command")

    p_serve = sub.add_parser("serve", help="Run the web server (HTML front end)")
    p_serve.add_argument("--port", default="5000", help="Port (default: 5000)")
    p_serve.add_argument("--no-debug", action="store_false", dest="debug", help="Disable debug mode")
    p_serve.set_defaults(debug=True, func=_cmd_serve)

    p_t = sub.add_parser("translate", help="Translate an SRT file from the CLI")
    p_t.add_argument("input", help="Input .srt file path")
    p_t.add_argument("-o", "--output", help="Output .srt file (default: <input>_AI_POLISHED.srt)")
    p_t.add_argument("--literal", action="store_true", help="Also save Google-only (literal) translation")
    p_t.set_defaults(func=_cmd_translate)

    parsed = parser.parse_args()
    if not parsed.command:
        parsed.func = _cmd_serve
        parsed.port = "5000"
        parsed.debug = True
    parsed.func(parsed)
