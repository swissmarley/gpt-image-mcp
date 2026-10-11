#!/usr/bin/env bash
# Start the GPT Image MCP server + a Cloudflare Quick Tunnel and print the Claude.ai connector URL.
set -euo pipefail

cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

PORT=8000
LOG_DIR="logs"
SERVER_LOG="$LOG_DIR/server.log"
TUNNEL_LOG="$LOG_DIR/cloudflared.log"
mkdir -p "$LOG_DIR"

SERVER_PID=""
TUNNEL_PID=""

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "Shutting down..."
  [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  echo "Stopped server and tunnel."
}
on_signal() { cleanup; exit 0; }
trap on_signal INT TERM
trap cleanup EXIT

# --- prerequisites -----------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv not found. Install it: https://docs.astral.sh/uv/" >&2
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "cloudflared not found: installing with Homebrew..."
    brew install cloudflared
  else
    echo "ERROR: cloudflared not found and Homebrew is unavailable." >&2
    exit 1
  fi
fi

if [[ -f "$HOME/.cloudflared/config.yaml" || -f "$HOME/.cloudflared/config.yml" ]]; then
  echo "WARNING: ~/.cloudflared/config.yaml exists. Quick tunnels may not work while it is present;"
  echo "         rename it temporarily if the tunnel fails to start."
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example. Add your OPENAI_API_KEY to it."
fi
if ! grep -Eq '^OPENAI_API_KEY=.+' .env; then
  echo "WARNING: OPENAI_API_KEY is empty in .env. The tools will return an error until it is set."
fi

if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/"; then
  echo "ERROR: something is already listening on port $PORT. Stop it first." >&2
  exit 1
fi

# --- server --------------------------------------------------------------------
echo "Syncing dependencies..."
uv sync --quiet

echo "Starting MCP server on 127.0.0.1:$PORT (log: $SERVER_LOG)..."
uv run python server.py >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 60); do
  if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/"; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: server exited during startup. Last log lines:" >&2
    tail -n 20 "$SERVER_LOG" >&2
    exit 1
  fi
  sleep 0.5
done
if ! curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/"; then
  echo "ERROR: server did not respond on port $PORT within 30s." >&2
  exit 1
fi

# The server generates MCP_SECRET_PATH into .env on first run.
SECRET_PATH="$(grep -E '^MCP_SECRET_PATH=' .env | tail -n1 | cut -d= -f2- | tr -d '"'"'"' \r')"
if [[ -z "$SECRET_PATH" ]]; then
  echo "ERROR: MCP_SECRET_PATH missing from .env." >&2
  exit 1
fi
echo "Local endpoint: http://127.0.0.1:$PORT/$SECRET_PATH/mcp"

# --- tunnel ----------------------------------------------------------------------
echo "Starting Cloudflare Quick Tunnel (log: $TUNNEL_LOG)..."
: >"$TUNNEL_LOG"
cloudflared tunnel --no-autoupdate --url "http://localhost:$PORT" >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

TUNNEL_URL=""
for _ in $(seq 1 60); do
  TUNNEL_URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n1 || true)"
  [[ -n "$TUNNEL_URL" ]] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "ERROR: cloudflared exited. Last log lines:" >&2
    tail -n 20 "$TUNNEL_LOG" >&2
    exit 1
  fi
  sleep 0.5
done
if [[ -z "$TUNNEL_URL" ]]; then
  echo "ERROR: could not find a trycloudflare.com URL in cloudflared output." >&2
  tail -n 20 "$TUNNEL_LOG" >&2
  exit 1
fi

CONNECTOR_URL="$TUNNEL_URL/$SECRET_PATH/mcp"
echo "$CONNECTOR_URL" >"$LOG_DIR/connector_url.txt"
chmod 600 "$LOG_DIR/connector_url.txt"

cat <<EOF

==================================================================
 Claude.ai connector URL (valid until you press Ctrl+C):

   $CONNECTOR_URL

 Claude.ai -> Settings -> Connectors -> Add custom connector -> paste URL.
 This URL changes on every run; re-add the connector each time.
==================================================================

Press Ctrl+C to stop.
EOF

# Exit (and clean up) if either process dies.
while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$TUNNEL_PID" 2>/dev/null; do
  sleep 1 & wait $! 2>/dev/null || true   # interruptible, so Ctrl+C is handled immediately
done
echo "A process exited unexpectedly. See $LOG_DIR/." >&2
exit 1
