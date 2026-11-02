# Jarvis's core: the Python app and the Claude Code CLI (bundled in the claude-agent-sdk wheel).
# No browser and no Node here: the browser lives in its own container (browser.Dockerfile).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JARVIS_IN_CONTAINER=1 \
    JARVIS_CONFIG=/config/jarvis.yaml \
    JARVIS_SECRETS_DIR=/secrets \
    HOME=/tmp/home

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 jarvis \
 && useradd --uid 10001 --gid jarvis --no-create-home --shell /usr/sbin/nologin jarvis \
 && mkdir -p /data /secrets /work /memory /vault /config \
 && chown jarvis:jarvis /data /secrets /work /memory /vault \
 && chmod 700 /secrets

WORKDIR /app
COPY requirements.txt pyproject.toml ./
COPY packages/callgate ./packages/callgate
RUN pip install --no-cache-dir -r requirements.txt
COPY jarvis ./jarvis
COPY playbooks ./playbooks

USER jarvis
EXPOSE 8765
ENTRYPOINT ["python", "-m", "jarvis"]
CMD ["service"]
