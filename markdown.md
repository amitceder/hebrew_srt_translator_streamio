📝 SRT Translation Project: Overview & Progress
🎯 Goal

Developing a Hybrid SRT Translator that combines the speed of Google Translate with the contextual intelligence of LLMs (Llama 3.1 8B via Groq) to fix gender, grammar, and professional flow in Hebrew subtitles.
🛠️ Tech Stack

    Backend: Python (Flask) + srt library.

    Translation 1: deep-translator (Google) for an instant literal baseline.

    Translation 2: Groq API for contextual "AI Polish" (default: llama-3.1-8b-instant; configurable via GROQ_MODEL).

    Frontend: Simple HTML/Tailwind with real-time progress polling.

🚀 Key Improvements Implemented

    **Parallel Google Phase:** Batches are kept under 4500 chars (Google’s 5k limit) and run in parallel (default 6 workers). Phase 1 is much faster and avoids oversized-request issues.

    Multi-Line Preservation: Solved the issue where AI would delete the 2nd line of a subtitle. We now use Regex (re.split) to capture the entire block of text per ID.

    Context Optimization: AI batch size default 10 lines (configurable via AI_BATCH_SIZE). Enough context for speaker/gender without slowdowns.

    Professional Prompting: The AI is now instructed to:

        Fix gender based on names in brackets (e.g., [Luke]).

        Adjust punctuation directionality (Hebrew RTL).

        Translate slang naturally (e.g., "Move" -> "זוזי" instead of "מהלך").

        Never output "Note:", "I assumed", or any translator commentary; a post-step strips these if they appear. Prefer Hebrew subtitle terms (e.g. עוקץ, הפללתי, בלוות'ר, זוטרופוליס).

    **Parallel AI Polish:** Multiple batches sent to Groq concurrently (default 8 workers). Phase 2 is faster on long files.

    **Configurable model & speed:** Use env vars GROQ_MODEL, AI_PARALLEL_WORKERS, AI_MAX_TOKENS (see section below).

📂 Current File Structure

    app.py: Flask server, Google batching, parallel Groq AI polisher (ThreadPoolExecutor), and ID/TEXT regex parsing.

    index.html: The UI that handles file uploads and shows progress for both phases.

⚡ Speed & accuracy: env vars

    **Phase 1 (Google):** `GOOGLE_PARALLEL_WORKERS=6` (default). Google allows ~5 req/s; if you see rate limits, lower to 3–4.

    **Phase 2 (AI):** `AI_PARALLEL_WORKERS=8` (default). `AI_BATCH_SIZE=10` (smaller = more batches = more parallelism). Faster models: `GROQ_MODEL=openai/gpt-oss-20b` or `meta-llama/llama-4-scout-17b-16e-instruct`.

    **Output cap:** `AI_MAX_TOKENS=1024` to avoid long tail and speed up responses.

⚠️ Open Points for Next Session

    Refinement: Monitor if the AI still merges two short lines into one (even when meaning is preserved).

    Rate limits: If you see Groq rate-limit errors, lower `AI_PARALLEL_WORKERS` or add retries with backoff.
