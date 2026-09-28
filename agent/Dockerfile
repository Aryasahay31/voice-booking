# Container image for the Glow Studio agent on LiveKit Cloud.
# Python 3.12: well supported by every dependency (your laptop's 3.14 is very new).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# LiveKit requires a non-root user
ARG UID=10001
RUN adduser --disabled-password --gecos "" --home /app --shell /sbin/nologin --uid ${UID} appuser

WORKDIR /app

# Install dependencies first so this layer is cached between code-only deploys
COPY requirements.txt .
RUN pip install -r requirements.txt

# Then the code (secrets are NOT copied: see .dockerignore; they're injected at runtime)
COPY . .
RUN chown -R appuser:appuser /app
USER appuser

# Download model files (turn detector fallback, noise cancellation) at build time,
# so calls don't wait for downloads at runtime
RUN python agent.py download-files

# "start" = production mode (no hot reload)
CMD ["python", "agent.py", "start"]