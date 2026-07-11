FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install all necessary Python libraries
RUN pip install --no-cache-dir \
    flask \
    srt \
    google-genai \
    groq \
    deep-translator

COPY . .

EXPOSE 5000

ENV FLASK_DEBUG=0

CMD ["python", "app.py", "serve", "--no-debug", "--port", "5000"]
