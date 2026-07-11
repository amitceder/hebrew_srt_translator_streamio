🚀 Project Status: Hybrid SRT Translator

Current Architecture: Asynchronous Threading (Zero-Wait Handshake)
Primary Goal: Literal baseline (Google) + Contextual Polish (Groq AI)
🔍 1. Traceability Checklist

When a file is dropped and "nothing happens," we must trace the data through these four checkpoints:
Stage	Component	What to look for	Tool
1. Capture	Browser (JS)	File received: [name] in console	F12 > Console
2. Handshake	Network (POST)	POST /translate status 200	F12 > Network
3. Processing	Server (Python)	Job [ID]: Starting Google Phase	docker logs -f
4. Polling	Browser (GET)	GET /status/[ID] repeating every 2s	F12 > Network
🛠 2. Essential Debugging Commands
View Live AI Logic

To see exactly what the AI is "thinking" and how it's mapping the Hebrew IDs:
Bash

docker logs -f srt-app

Inspect Browser-Server Communication

If the progress bar stays at 0%:

    Press F12 in Firefox.

    Go to the Network tab.

    Look for the status requests.

    Click one and check Response. It should look like:
    {"ai_progress": 15, "google_status": "completed", ...}

🚧 3. Known "Friction Points" for Next Session
A. Memory Persistence

    Issue: Currently, jobs = {} is stored in RAM. If you restart the Docker container, any translation in progress is lost.

    Trace: If the browser asks for /status/12345 after a restart, it gets a 404.

    Fix: We may need to move jobs to a local JSON file or a SQLite database.

B. Google Translate Rate Limits

    Issue: deep-translator (Google) can sometimes block requests if a movie is very long (over 1,000 lines) in one go.

    Trace: Look for HTTP 429 in the Docker logs during the "Google Phase."

    Fix: Implement a 0.1s sleep between lines or batch the Google requests.

C. Groq Token Limits

    Issue: Large batches of text sent to Llama-3.1 might exceed the context window or rate limits.

    Trace: Look for groq.RateLimitError in logs.

📝 4. Planned Enhancements

    [ ] Session Recovery: Allow the user to refresh the page without losing the current translation progress.

    [ ] Visual Logs: Add a "Developer Console" inside the HTML UI to show the Python print statements to the user.

    [ ] Error Toast: Replace alert() with a nice sliding notification for better UX.
