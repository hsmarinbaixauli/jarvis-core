# Jarvis's browser (target "browser"): Playwright MCP over HTTP with Chromium. It only holds the browser profile:
# no database, no API keys. If a web page ever broke out of Chromium, this is all it would find.
FROM node:22-bookworm-slim AS browser

ARG PLAYWRIGHT_MCP_VERSION=0.0.83
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp \
    NPM_CONFIG_UPDATE_NOTIFIER=false

RUN npm install -g "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" \
 && cd "$(npm root -g)/@playwright/mcp" \
 && npx playwright-core install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.npm \
 && groupadd --gid 10002 browser \
 && useradd --uid 10002 --gid browser --no-create-home --shell /usr/sbin/nologin browser \
 && mkdir -p /profile /output \
 && chown browser:browser /profile /output

USER browser
ENTRYPOINT ["playwright-mcp"]
# Chromium's own sandbox needs privileges a hardened container doesn't give; the container is the sandbox.
CMD ["--port", "8931", "--host", "0.0.0.0", "--allowed-hosts", "browser:8931", "--headless", \
     "--browser", "chromium", "--no-sandbox", "--no-webmcp", "--user-data-dir", "/profile", "--output-dir", "/output"]


# One-off window to sign in to Jarvis's accounts by hand: the same Chromium and profile, shown through
# noVNC on http://127.0.0.1:6080 (published on the host's loopback only). Built with --target login.
FROM browser AS login
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc fluxbox novnc websockify openssl \
 && rm -rf /var/lib/apt/lists/*
COPY login.sh /usr/local/bin/jarvis-login
RUN chmod 755 /usr/local/bin/jarvis-login
USER browser
EXPOSE 6080
ENTRYPOINT ["jarvis-login"]
CMD []
