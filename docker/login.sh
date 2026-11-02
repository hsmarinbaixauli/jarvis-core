#!/bin/sh
# Starts a virtual screen with Chromium on Jarvis's profile and serves it through noVNC.
# The VNC password is random on every run and only printed here: nothing to store.
set -eu
export DISPLAY=:99
mkdir -p "$HOME"
PASSWORD=$(openssl rand -hex 4)
x11vnc -storepasswd "$PASSWORD" "$HOME/vncpass" >/dev/null 2>&1

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp >/dev/null 2>&1 &
sleep 1
fluxbox >/dev/null 2>&1 &
x11vnc -display :99 -rfbauth "$HOME/vncpass" -localhost -forever -shared -quiet >/dev/null 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

echo ""
echo "  Abre  http://127.0.0.1:6080/vnc.html  en tu navegador."
echo "  Contraseña de esta sesión: $PASSWORD"
echo "  Inicia sesión en las cuentas de Jarvis y cierra la ventana de Chromium al terminar."
echo ""

CHROME=$(ls -d /ms-playwright/chromium-*/chrome-linux*/chrome | head -n 1)
exec "$CHROME" --no-sandbox --no-first-run --user-data-dir=/profile --start-maximized "${LOGIN_URL:-https://accounts.google.com}"
