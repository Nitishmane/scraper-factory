FROM python:3.11-slim

# Node 20+ for the Bright Data CLI, which is how this project talks to Scraper Studio.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @brightdata/cli

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && opentelemetry-bootstrap -a install

COPY src/ src/
COPY scripts/ scripts/
COPY CLAUDE.md Makefile ./
RUN chmod +x scripts/*.sh

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["opentelemetry-instrument", "python", "-m", "uvicorn", "factory.app:app", \
     "--host", "0.0.0.0", "--port", "8000"]
