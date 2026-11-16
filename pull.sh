#!/usr/bin/env bash
# Pull every challenge of a CTF platform (any platform, not only CTFd) with a headless
# Claude Code agent, into challenges/<slug>/{metadata.yml,distfiles/}.
#
#   PLATFORM_COOKIE='session=...' ./pull.sh https://ctf.example.com
#   PLATFORM_COOKIE='...' ./pull.sh https://ctf.example.com --out ./ecw
#   PLATFORM_COOKIE='...' ./pull.sh https://ctf.example.com --loop 300   # re-pull every 5 min
#
# PLATFORM_COOKIE: the Cookie header copied from your browser (DevTools > Network > a request
#   to the platform > Request Headers > Cookie). It goes into a temporary curlrc, never to the agent.
# PLATFORM_HEADERS: optional extra header, e.g. 'Authorization: Bearer ...'.
# PULL_MODEL (default claude-sonnet-5-5), PULL_EFFORT (default low).
#
# Then: ctf-solve --challenge challenges/<slug> --no-submit
set -euo pipefail

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

URL="$1"
shift
OUT="challenges"
LOOP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --loop) LOOP="$2"; shift 2 ;;
    *) echo "error: unknown option $1" >&2; exit 1 ;;
  esac
done

if [[ -z "${PLATFORM_COOKIE:-}" && -z "${PLATFORM_HEADERS:-}" ]]; then
  echo "error: set PLATFORM_COOKIE (or PLATFORM_HEADERS) with your browser session" >&2
  exit 1
fi

# Credentials go into a private curlrc: every curl the agent runs is authenticated and the
# agent never sees the cookie (its commands cannot expand environment variables anyway)
CURL_HOME="$(mktemp -d)"
trap 'rm -rf "$CURL_HOME"' EXIT
(
  umask 077
  {
    [[ -n "${PLATFORM_COOKIE:-}" ]] && printf 'header = "Cookie: %s"\n' "$PLATFORM_COOKIE"
    [[ -n "${PLATFORM_HEADERS:-}" ]] && printf 'header = "%s"\n' "$PLATFORM_HEADERS"
    printf 'silent\nshow-error\n'
  } > "$CURL_HOME/.curlrc"
)
export CURL_HOME
unset PLATFORM_COOKIE PLATFORM_HEADERS

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE="${CLAUDE_CLI:-claude}"
mkdir -p "$OUT"
cd "$OUT"

pull_once() {
  local done_list
  done_list="$(sed -n 's/^name: *//p' ./*/metadata.yml 2>/dev/null | sed 's/^/- /' || true)"
  "$CLAUDE" -p "Platform URL: $URL
Pull every challenge into the current directory.
Already pulled, skip them without fetching their details (report them as SKIPPED):
${done_list:-(none)}" \
    --append-system-prompt "$(cat "$REPO/prompts/pull_challenges.md")" \
    --model "${PULL_MODEL:-claude-sonnet-5-5}" \
    --effort "${PULL_EFFORT:-low}" \
    --permission-mode acceptEdits \
    --allowedTools "Bash(curl:*)" "Bash(mkdir:*)" "Bash(ls:*)" "Bash(file:*)" \
                   "Bash(head:*)" "Bash(grep:*)" "Bash(jq:*)" "Read" "Write" "Glob" "Grep" \
    --disallowedTools "WebFetch" "WebSearch" "Agent" \
    < /dev/null
}

if [[ "$LOOP" -gt 0 ]]; then
  while true; do
    echo "=== pull $(date +%H:%M:%S) ==="
    pull_once || echo "pull failed, retrying in ${LOOP}s" >&2
    sleep "$LOOP"
  done
else
  pull_once
fi
