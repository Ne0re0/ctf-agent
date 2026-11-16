#!/usr/bin/env bash
# Run ctf-solve on every pulled challenge of a folder, a few at a time.
#
#   solve_all.sh <challenges-root> [--jobs N] [--timeout MIN] [--only slug[,slug...]] [-- <ctf-solve options>]
#
#   --jobs N       challenges solved at once (default 2; each one starts a sandbox per model)
#   --timeout MIN  stop a challenge after MIN minutes (default 60, 0 = no limit: solvers never give up)
#   --only a,b     only these challenge folders
#   after --       ctf-solve options, replacing the default ones (see DEFAULT_SOLVE_ARGS below):
#                  no submission, writeups, ourcodex, 2 Claude and 3 Codex models
#
# Per challenge it writes <challenge>/solve.log, and <challenge>/flag.txt when a flag is found.
# A challenge with a flag.txt is skipped, so the script can be re-run after a new pull.
# <challenges-root>/results.tsv lists every challenge: slug, status, flag.
# CTFd credentials for flag submission come from the environment (CTFD_URL, CTFD_TOKEN or
# CTFD_USER/CTFD_PASS), never from the command line.
set -uo pipefail

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }
[[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]] && usage

ROOT="$(cd "$1" 2>/dev/null && pwd)" || { echo "error: no such folder: $1" >&2; exit 1; }
shift
JOBS=2
TIMEOUT_MIN=60
ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jobs) JOBS="$2"; shift 2 ;;
    --timeout) TIMEOUT_MIN="$2"; shift 2 ;;
    --only) ONLY=",$2,"; shift 2 ;;
    --) shift; break ;;
    *) echo "error: unknown option $1 (ctf-solve options go after --)" >&2; exit 1 ;;
  esac
done
# Used when nothing is given after --
DEFAULT_SOLVE_ARGS=(
  --codex-cli ourcodex --writeup --no-submit
  --models claude-sdk/claude-opus-5-5/max
  --models claude-sdk/claude-sonnet-4-6/max
  --models codex/gpt-6.1-sol/medium
  --models codex/gpt-6-astra/medium
  --models codex/gpt-5.6-sol
)
SOLVE_ARGS=("$@")
[[ ${#SOLVE_ARGS[@]} -eq 0 ]] && SOLVE_ARGS=("${DEFAULT_SOLVE_ARGS[@]}")

# The repo this skill lives in: ctf-solve runs from there so it reads its .env.
# Traces go to ctf-solve's default folder (/tmp/logs, or CTF_LOGS_DIR), where ctf-watch reads.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export COLUMNS=400  # keep "FLAG FOUND: ..." on one line in solve.log
RESULTS="$ROOT/results.tsv"

solve_one() {
  local dir="$1" slug status=0 flag
  slug="$(basename "$dir")"
  mkdir -p "$dir/distfiles"
  echo "[$(date +%H:%M:%S)] START   $slug"
  local cmd=(uv run --project "$REPO" ctf-solve --challenge "$dir" "${SOLVE_ARGS[@]}")
  if [[ "$TIMEOUT_MIN" -gt 0 ]]; then
    cmd=(timeout --signal=INT --kill-after=90 "${TIMEOUT_MIN}m" "${cmd[@]}")
  fi
  (cd "$REPO" && "${cmd[@]}") > "$dir/solve.log" 2>&1 < /dev/null || status=$?
  flag="$(sed -n 's/^FLAG FOUND: //p' "$dir/solve.log" | tail -1)"
  if [[ -n "$flag" ]]; then
    printf '%s\n' "$flag" > "$dir/flag.txt"
    echo "[$(date +%H:%M:%S)] SOLVED  $slug  $flag"
  elif [[ "$status" -eq 124 || "$status" -eq 137 ]]; then
    echo "[$(date +%H:%M:%S)] TIMEOUT $slug (${TIMEOUT_MIN} min, see $dir/solve.log)"
  else
    echo "[$(date +%H:%M:%S)] FAILED  $slug (exit $status, see $dir/solve.log)"
  fi
}

write_results() {
  local dir slug
  printf 'slug\tstatus\tflag\n' > "$RESULTS"
  for dir in "$ROOT"/*/; do
    dir="${dir%/}"
    [[ -f "$dir/metadata.yml" ]] || continue
    slug="$(basename "$dir")"
    if [[ -s "$dir/flag.txt" ]]; then
      printf '%s\tsolved\t%s\n' "$slug" "$(head -1 "$dir/flag.txt")"
    elif grep -q 'needs-instance' "$dir/metadata.yml" && grep -q 'instance not started' "$dir/metadata.yml"; then
      printf '%s\tneeds-instance\t\n' "$slug"
    elif [[ -f "$dir/solve.log" ]]; then
      printf '%s\tunsolved\t\n' "$slug"
    else
      printf '%s\tnot-run\t\n' "$slug"
    fi
  done >> "$RESULTS"
}

todo=()
for dir in "$ROOT"/*/; do
  dir="${dir%/}"
  slug="$(basename "$dir")"
  [[ -f "$dir/metadata.yml" ]] || continue
  [[ -z "$ONLY" || "$ONLY" == *",$slug,"* ]] || continue
  if [[ -s "$dir/flag.txt" ]]; then
    echo "SKIP    $slug (already solved)"
  elif grep -q 'needs-instance' "$dir/metadata.yml" && grep -q 'instance not started' "$dir/metadata.yml"; then
    echo "SKIP    $slug (instance not started: set connection_info in its metadata.yml)"
  else
    todo+=("$dir")
  fi
done

echo "${#todo[@]} challenge(s) to solve, $JOBS at a time, timeout ${TIMEOUT_MIN} min each. Watch: ctf-watch"
echo "ctf-solve options: ${SOLVE_ARGS[*]}"
running=0
for dir in "${todo[@]}"; do
  if [[ "$running" -ge "$JOBS" ]]; then
    wait -n || true
    running=$((running - 1))
    write_results
  fi
  solve_one "$dir" &
  running=$((running + 1))
done
wait
write_results
echo "Results: $RESULTS"
column -t -s $'\t' "$RESULTS"
