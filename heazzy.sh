#!/usr/bin/env bash
# Race Claude Sonnet 5.5, Opus 5.5, Opus 5, Mythos 5.2 (all through ourclaude) and Grok 4.7 fast
# on one local challenge.
#
#   ./heazzy.sh <challenge-dir> [extra ctf-solve options...]
#
#   ./heazzy.sh ./mon-chall                  # dry run (--no-submit)
#   SUBMIT=1 ./heazzy.sh ./mon-chall         # submit flags to CTFd (CTFD_URL/CTFD_TOKEN)
#   IMAGE=ubuntu:24.04 ./heazzy.sh ./chall   # other sandbox image (default: ctf-sandbox)
#   ./heazzy.sh ./mon-chall --bench          # benchmark: every model keeps going up to 10 min
#                                            # after the first solve, no shared findings
#   ./heazzy.sh ./mon-chall -v               # extra options go to ctf-solve
#   ./heazzy.sh ./dossier --description "..." --connection-info "nc host 1337"
#                                            # folder without metadata.yml: its files are the distfiles
#
# Models can be overridden: CLAUDE_MODEL, OPUS_MODEL, OPUS5_MODEL, MYTHOS_MODEL, GROK_MODEL
# (full specs); CLAUDE_CLI picks the Claude Code binary for every claude-sdk/* agent (default ourclaude);
# OMP_MODEL=omp/... adds an omp agent (none by default);
# set one to an empty string to leave that agent out (e.g. MYTHOS_MODEL= ./heazzy.sh ./chall).
set -euo pipefail

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

CHALLENGE="$1"
shift
if [[ ! -d "$CHALLENGE" ]]; then
  echo "error: challenge directory $CHALLENGE not found" >&2
  exit 1
fi

CLAUDE_CLI="${CLAUDE_CLI:-ourclaude}"
CLAUDE_MODEL="${CLAUDE_MODEL-claude-sdk/claude-sonnet-5-5/medium}"
OPUS_MODEL="${OPUS_MODEL-claude-sdk/claude-opus-5-5/medium}"
OPUS5_MODEL="${OPUS5_MODEL-claude-sdk/claude-opus-5/medium}"
MYTHOS_MODEL="${MYTHOS_MODEL-claude-sdk/claude-mythos-5-2/medium}"
GROK_MODEL="${GROK_MODEL-grok/grok-4.7-build-fast}"
OMP_MODEL="${OMP_MODEL-}"
IMAGE="${IMAGE:-ctf-sandbox}"

if [[ "$OMP_MODEL" == omp/orcarouter/* && -z "${ORCA_KEY:-}" ]]; then
  echo "warning: ORCA_KEY is not set, the omp solver (OrcaRouter) will fail. export ORCA_KEY=..." >&2
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "error: sandbox image '$IMAGE' not found." >&2
  echo "  build it: docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox ." >&2
  echo "  or quick test: IMAGE=ubuntu:24.04 $0 $CHALLENGE" >&2
  exit 1
fi

MODELS=()
for spec in "$CLAUDE_MODEL" "$OPUS_MODEL" "$OPUS5_MODEL" "$MYTHOS_MODEL" "$GROK_MODEL" "$OMP_MODEL"; do
  [[ -n "$spec" ]] && MODELS+=(--models "$spec")
done
if [[ ${#MODELS[@]} -eq 0 ]]; then
  echo "error: every model is disabled" >&2
  exit 1
fi

# --bench is shorthand for ctf-solve --benchmark
EXTRA=()
for arg in "$@"; do
  if [[ "$arg" == "--bench" ]]; then
    EXTRA+=(--benchmark)
  else
    EXTRA+=("$arg")
  fi
done

SUBMIT_FLAG=(--no-submit)
[[ "${SUBMIT:-0}" == "1" ]] && SUBMIT_FLAG=()

# Run from the repo so `uv run` finds the project; logs/ lands in the current directory
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHALLENGE="$(cd "$CHALLENGE" && pwd)"

exec uv run --project "$REPO" ctf-solve \
  --challenge "$CHALLENGE" \
  --image "$IMAGE" \
  --claude-cli "$CLAUDE_CLI" \
  "${SUBMIT_FLAG[@]}" \
  "${MODELS[@]}" \
  ${EXTRA[@]+"${EXTRA[@]}"}
