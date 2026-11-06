# LinkedIn reply bot on a server: Chromium in a virtual screen (Xvfb) and a VNC server for
# signing in to LinkedIn by hand. Builds on amd64 and arm64; the Playwright image already
# holds a Chromium that matches the Playwright version in uv.lock.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc fluxbox tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --break-system-packages \
      "httpx>=0.28.1" "playwright==1.64.1" "python-dotenv>=1.2.4"

# The server user's uid (id -u), so ./data stays readable on the host.
ARG APP_UID=1000
RUN (getent passwd ${APP_UID} | cut -d: -f1 | xargs -r userdel -r 2>/dev/null || true) \
 && useradd --create-home --uid ${APP_UID} app && mkdir /data && chown app:app /data
COPY bot /app/bot
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

USER app
WORKDIR /app
ENV DISPLAY=:99 \
    BOT_DATA_DIR=/data \
    BOT_BROWSER_CHANNEL=chromium \
    BOT_CHROMIUM_SANDBOX=1 \
    PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=15s --start-period=180s --retries=3 \
  CMD ["python3", "-m", "bot", "health"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python3", "-m", "bot", "run"]
