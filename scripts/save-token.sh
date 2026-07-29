#!/usr/bin/env bash
# Save the BirdNET-Cloud station token to ~/.config/birdnetcloud/token (0600),
# then check it against the live API.
#
# Run from a REAL terminal (the token prompt needs a TTY):
#     ./scripts/save-token.sh
#
# Non-interactive alternatives:
#     ./scripts/save-token.sh <token>
#     echo <token> | ./scripts/save-token.sh
#
# The token is never echoed, never printed back, and never passed on a command
# line by the interactive path — so it stays out of shell history and logs.
set -euo pipefail

DEST="${BIRDNET_TOKEN_FILE:-$HOME/.config/birdnetcloud/token}"
ENDPOINT="${BIRDNET_CLOUD_ENDPOINT:-https://api.birdnetcloud.com}"

TOKEN=""
if [ "$#" -ge 1 ]; then
    TOKEN="$1"
elif [ -t 0 ]; then
    printf 'Paste the BirdNET-Cloud station token (input hidden), then Enter:\n> '
    IFS= read -rs TOKEN
    printf '\n'
else
    # piped stdin
    IFS= read -r TOKEN || true
fi

# Trim whitespace/CR that survives a copy-paste.
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"

if [ -z "$TOKEN" ]; then
    echo "No token received." >&2
    if [ ! -t 0 ] && [ "$#" -eq 0 ]; then
        echo "  (stdin was not a terminal — run this from a real terminal," >&2
        echo "   or pass the token as an argument.)" >&2
    fi
    exit 1
fi

mkdir -p "$(dirname "$DEST")"
old_umask=$(umask); umask 077
printf '%s' "$TOKEN" > "$DEST"
umask "$old_umask"
chmod 600 "$DEST"

echo "saved: $DEST (mode $(stat -c %a "$DEST"), ${#TOKEN} chars)"

# Validate against the API. A heartbeat is the cheapest authenticated call and
# creates no detection data. 401 => wrong/rotated token; anything else means the
# token was accepted.
if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found — skipping validation."
    exit 0
fi

echo -n "checking token against ${ENDPOINT} ... "
code=$(curl -sS --max-time 20 -o /tmp/bnc-token-check.$$ -w '%{http_code}' \
    -X POST "${ENDPOINT}/api/v1/devices/heartbeat" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{"version":"token-check","queue_depth":0}' 2>/dev/null) || code="000"

body="$(head -c 200 /tmp/bnc-token-check.$$ 2>/dev/null || true)"
rm -f /tmp/bnc-token-check.$$

case "$code" in
    401|403) echo "REJECTED (http $code: $body)"
             echo "  The token is wrong, expired, or was rotated in the dashboard."
             exit 1 ;;
    000)     echo "could not reach the API (network?). Token saved anyway." ;;
    2*)      echo "OK (http $code) — station accepted the heartbeat." ;;
    *)       echo "unexpected http $code: $body"
             echo "  Token saved; the probe will show more detail." ;;
esac
