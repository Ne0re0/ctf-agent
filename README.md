# CTF Agent

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM manages the competition while **solver swarms** attack individual challenges. Each swarm runs multiple models simultaneously — the first to find the flag wins.

```
                        +-----------------+
                        |  CTFd Platform  |
                        +--------+--------+
                                 |
                        +--------v--------+
                        |  Poller (5s)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        | (Claude/Codex)  |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     |  Opus (medium)  | |  Opus (medium) | |     ...        |
     |  Opus (max)     | |  Opus (max)    | |                |
     |  GPT-6.1 Sol    | |  GPT-6.1 Sol   | |                |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     |                 |  |                |
     | pwntools, r2,   |  | pwntools, r2,  |
     | gdb, python...  |  | gdb, python... |
     +-----------------+  +----------------+
```

Each solver runs in its own Docker container with CTF tools pre-installed. Solvers never give up — they keep trying different approaches until the flag is found.

There are two ways to run it:

- **Single challenge** (`ctf-solve --challenge DIR`): one swarm on one local challenge
  directory. No coordinator, and no CTFd needed with `--no-submit`.
- **Competition mode** (no `--challenge`): the coordinator polls a CTFd instance, downloads
  each new challenge and starts a swarm for it, until you press Ctrl+C.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs the required Python 3.14 for you)
- Docker, usable by your user without `sudo`
- At least one solver backend:
  - the `claude` CLI (Claude Code), logged in, for `claude-sdk/*` models and the Claude coordinator
  - the `codex` CLI, logged in, for `codex/*` models and the Codex coordinator
  - or API credentials for the API-backed providers (Bedrock, Azure OpenAI, OpenCode Zen, Google)
- A CTFd account or API token, only for competition mode or to submit flags for real

## Installation

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# or: pipx install uv   /   brew install uv
```

Open a new shell so `~/.local/bin` is on your `PATH`, then check with `uv --version`.

### 2. Get the code and install the dependencies

```bash
git clone https://github.com/Ne0re0/ctf-agent.git
cd ctf-agent
uv sync
```

`uv sync` creates `.venv/` with the exact versions pinned in `uv.lock` (downloading Python
3.14 if your system doesn't have it) and installs the project in editable mode, so later
`git pull`s and code edits apply without reinstalling. Rerun `uv sync` when dependencies change.

From the repo directory, run the tools with `uv run ctf-solve ...` and `uv run ctf-msg ...`.

### 3. Build the sandbox image

Run this from the repo root (the build copies `sandbox/sandbox-tools.txt`):

```bash
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
```

The image is large (SageMath, angr, PyTorch...), so the first build takes a while. Solvers use
`ctf-sandbox` by default; pass `--image NAME` to use another tag.

If `docker` only works with `sudo`, add yourself to the `docker` group and log in again:

```bash
sudo usermod -aG docker "$USER"
```

### 4. Log in to Claude Code and/or Codex

```bash
npm install -g @anthropic-ai/claude-code
claude            # log in when prompted, then exit

npm install -g @openai/codex
codex login
```

The `claude-sdk/*` and `codex/*` solvers run these commands themselves, so they use the same
logins, settings and proxies as your terminal. No API key is needed. If `claude` is a shell
alias or a wrapper on your machine, see [Claude and Codex commands](#claude-and-codex-commands).

### 5. (Optional) Run `ctf-solve` from any directory

Link the venv's commands into a directory on your `PATH`:

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/.venv/bin/ctf-solve" ~/.local/bin/ctf-solve
ln -s "$PWD/.venv/bin/ctf-msg" ~/.local/bin/ctf-msg
```

Then put your settings in `~/.config/ctf-agent/config.env` (see [Configuration](#configuration))
so they apply wherever you run from. The rest of this README writes `ctf-solve`; inside the
repo without the links, use `uv run ctf-solve`.

### 6. Check the setup

```bash
ctf-solve --models
```

This prints the default solver lineup, the Claude models your `claude` CLI and local Claude
Code catalog advertise, and the Codex models your account can use. A section that says
`unavailable (...)` means that CLI isn't installed, isn't on `PATH`, or couldn't be queried.

## Solving a local challenge

### Challenge directory

```
my-chall/
├── metadata.yml     # optional: generated automatically when absent
└── distfiles/       # optional: the files handed out with the challenge
```

If `metadata.yml` is absent, `ctf-solve` creates one using the directory name and the
description `Find the flag.`. This works with both `--challenge` and directories preloaded
from `--challenges-dir`. For live submission, make sure the directory name exactly matches
the CTFd challenge name, or provide an explicit `name` in `metadata.yml`.

`metadata.yml` describes the challenge. Every field is optional, but the solvers only know
what you put here, so provide more detail when it is available:

```yaml
name: baby-rsa
category: crypto
value: 100
description: |
  We intercepted this ciphertext. Can you decrypt it?
  Flag format: FCSC{...}
connection_info: nc localhost 1337   # only if there is a live service
tags: [rsa]
hints:
  - content: e is very small
```

Inside each sandbox, `distfiles/` is mounted read-only at `/challenge/distfiles/`,
`metadata.yml` at `/challenge/metadata.yml`, and every solver gets its own writable
`/challenge/workspace/`. Docker container names include the challenge and model names,
followed by a unique suffix so concurrent runs do not collide.

**Services on your machine:** `localhost` and `127.0.0.1` in `connection_info` are rewritten to
`host.docker.internal`, which the sandboxes resolve to your host. The service must listen on an
address the containers can reach (`0.0.0.0` or a Docker-published port, e.g.
`docker run -p 1337:1337 ...`), not only on `127.0.0.1`.

### Run it

```bash
ctf-solve --challenge ./my-chall --no-submit
```

- **`--no-submit`** (dry run) needs no CTFd. The first flag any solver reports is accepted
  **without verification** and stops the other solvers, so check it yourself.
- **Without `--no-submit`**, flags are submitted to CTFd (`CTFD_URL` with `CTFD_TOKEN`, or
  `CTFD_USER`/`CTFD_PASS`) under the challenge's `name`, and a solver only wins when CTFd
  accepts its flag. Use this for a challenge pulled from a live CTFd (see below).

The run ends with `FLAG FOUND: ...` (or `No flag found.`) and a cost summary per solver.
Press Ctrl+C to stop early. Pick the models with `--models` (see [Choosing models](#choosing-models)):

```bash
ctf-solve --challenge ./my-chall --no-submit \
  --models claude-sdk/claude-opus-4-6/max \
  --models codex/gpt-6.1-sol \
  -v
```

While it runs you can watch the traces in `./logs/` and send hints with `ctf-msg`
(see [Watching and talking to solvers](#watching-and-talking-to-solvers)).

### Generate a winner writeup

Add `--writeup` to keep the first successful solver's conversation and sandbox alive long
enough for one final turn. Sibling solvers are stopped first, then the winner writes a detailed,
reproducible Markdown solution to `writeup.md` in the challenge directory:

```bash
ctf-solve --challenge ./my-chall --writeup --no-submit
```

The confirmed flag is printed in green immediately, before sibling cleanup and writeup generation,
so it can be submitted to the competition platform without waiting for the document.
Writeup generation is bounded to five minutes and does not prevent normal cleanup if it fails.
The winner trace records `writeup_start`, `writeup_complete`, or `writeup_failed`. A writeup from
`--no-submit` explicitly marks the flag as unverified because dry-run flags are not checked by
CTFd. `--writeup` also works in coordinator mode, but cannot be combined with `--benchmark`.

### Benchmark models

Add `--benchmark` to a single-challenge run to measure every requested model independently:

```bash
ctf-solve --challenge ./my-chall --benchmark --no-submit \
  --models claude-sdk/claude-opus-4-6/max \
  --models codex/gpt-6.1-sol
```

The first solver to find a flag starts a 10-minute grace period. Other solvers keep working
until they find it too or the grace period expires. Sibling findings are disabled in benchmark
mode so one model cannot copy another model's flag. The final table reports solve order, elapsed
wall-clock time, result, and whether each model encountered a provider safety guard.

Without `--no-submit`, the first accepted flag is verified by CTFd and later submissions are
checked locally against that confirmed flag. With `--no-submit`, reported flags cannot be
verified, just like an ordinary dry run. `--benchmark` currently requires `--challenge`.

## Competition mode (CTFd)

```bash
ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 10
```

- The poller checks CTFd every 5 seconds. Each new challenge is downloaded into
  `<challenges-dir>/<slug>/` (same layout as above) and gets a swarm, with at most
  `--max-challenges` challenges being solved at once.
- Challenge directories already in `--challenges-dir` are loaded at startup.
- CTFd login: an API token (`--ctfd-token` / `CTFD_TOKEN`) is used when set, otherwise
  `CTFD_USER` / `CTFD_PASS`.
- `--no-submit` still reads challenges from CTFd but never submits a flag.
- It runs until Ctrl+C, then prints the flags found and the total cost.
- Every solver has its own container (2 CPUs, up to `CONTAINER_MEMORY_LIMIT`, 16g by default),
  so up to `--max-challenges` × number of models containers run at once. Size both to your host.

### Coordinator backend

```bash
ctf-solve --coordinator claude ...   # default, runs through your claude CLI
ctf-solve --coordinator codex ...    # runs through `codex app-server` (JSON-RPC)
```

The coordinator model is set with `--coordinator-model` (see [Coordinator model](#coordinator-model)).

### Downloading challenges without solving them

`pull_challenges.py` writes the same layout, which is handy to read the challenges first or
to solve one by hand:

```bash
uv run python pull_challenges.py --url https://ctf.example.com --token ctfd_your_token --output challenges
# or: --username myteam --password s3cr3t

ctf-solve --challenge challenges/<slug>     # submits to CTFd; add --no-submit for a dry run
```

## Choosing models

### Model specs

A solver is selected by a spec `provider/model[/effort]`:

| Provider | Example | Runs through | Credentials |
|----------|---------|--------------|-------------|
| `claude-sdk` | `claude-sdk/claude-opus-4-6/max` | your `claude` CLI | Claude Code login |
| `codex` | `codex/gpt-6.1-sol` | your `codex` CLI | Codex login |
| `bedrock` | `bedrock/us.anthropic.claude-opus-4-6-v1` | AWS Bedrock | `AWS_BEARER_TOKEN` or your default AWS credentials; `AWS_REGION` |
| `azure` | `azure/gpt-5.4` | Azure OpenAI | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` |
| `zen` | `zen/gpt-5.3-codex-spark` | OpenCode Zen | `OPENCODE_ZEN_API_KEY` |
| `google` | `google/gemini-3-flash-preview` | Google AI Studio | `GEMINI_API_KEY` |

**Effort** (third part) is `low`, `medium`, `high` or `max`, and is passed to the `claude` CLI
for `claude-sdk` specs. Any other value is ignored and the model's default effort is used.
Codex ignores the suffix: its reasoning effort is set per model in `REASONING_EFFORT` in
`backend/agents/codex_solver.py`.

A ChatGPT-login Codex only accepts the models your account lists; older ids such
as `gpt-5.4` are rejected. Use the ids that `ctf-solve --models` prints.

### Listing available models

```bash
ctf-solve --models
```

`--models` without a value prints the configured defaults, the Claude aliases advertised by
your `claude` CLI, the versioned models in your local Claude Code catalog, and the Codex models
available to your account, with the effort levels each one supports.

### Per run

Repeat `--models` to race a custom lineup. It replaces the default lineup entirely:

```bash
ctf-solve --challenge ./my-chall --no-submit \
  --models claude-sdk/claude-opus-4-6/max \
  --models codex/gpt-6-astra
```

### Changing the defaults

The default lineup is `DEFAULT_MODELS` in `backend/models.py`:

```python
DEFAULT_MODELS: list[str] = [
    "claude-sdk/claude-opus-4-6/medium",
    "claude-sdk/claude-opus-4-6/max",
    "codex/gpt-6.1-sol",
]
```

It can't be set from `.env`, `config.env` or an environment variable. Since the project is
installed in editable mode, an edit applies on the next run.

When you add a model id that isn't listed yet, also add it in the same file to:

- `VISION_MODELS` if it accepts images (Codex and API-backed solvers only send images to
  models listed there)
- `CONTEXT_WINDOWS` with its context size (otherwise 200k tokens is assumed)

### Coordinator model

| `--coordinator` | Default model | Change it per run | Change the default |
|-----------------|---------------|-------------------|--------------------|
| `claude` (default) | `claude-opus-4-6` | `--coordinator-model ID` | `backend/agents/claude_coordinator.py` (`resolved_model`) |
| `codex` | `gpt-6.1-sol` | `--coordinator-model ID` | `DEFAULT_CODEX_COORDINATOR_MODEL` in `backend/models.py` |

### Quota fallback

When a CLI reports that its quota is exhausted, a solver whose spec is in `QUOTA_FALLBACK`
(`backend/agents/swarm.py`) continues on the matching API-backed model in the same sandbox,
e.g. `claude-sdk/claude-opus-4-6` → `bedrock/us.anthropic.claude-opus-4-6-v1`. Only exact
specs match, so a spec with an effort suffix has no fallback.

## Configuration

Settings are read from, lowest to highest priority: `~/.config/ctf-agent/config.env`
(so a linked `ctf-solve` works from any directory), `./.env`, then environment variables.
Command-line flags override all of them. Start from the example:

```bash
cp .env.example .env                                   # per directory
# or
mkdir -p ~/.config/ctf-agent && cp .env.example ~/.config/ctf-agent/config.env
```

| Variable | Default | Used for |
|----------|---------|----------|
| `CTFD_URL` | `http://localhost:8000` | CTFd base URL (`--ctfd-url`) |
| `CTFD_TOKEN` | | CTFd API token (`--ctfd-token`); preferred over user/password |
| `CTFD_USER` / `CTFD_PASS` | `admin` / `admin` | CTFd login when no token is set |
| `CLAUDE_CLI` | `claude` | command behind `claude-sdk/*` and the Claude coordinator (`--claude-cli`) |
| `CODEX_CLI` | `codex` | command behind `codex/*` and the Codex coordinator (`--codex-cli`) |
| `CONTAINER_MEMORY_LIMIT` | `16g` | memory cap of each sandbox container |
| `AWS_REGION` | `us-east-1` | `bedrock/*` |
| `AWS_BEARER_TOKEN` | | `bedrock/*` (otherwise your default AWS credentials) |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` | | `azure/*` |
| `OPENCODE_ZEN_API_KEY` | | `zen/*` |
| `GEMINI_API_KEY` | | `google/*` |

`ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in `.env.example` are not read by ctf-agent itself:
the `claude` and `codex` CLIs use their own logins (and see your shell environment as usual).
The sandbox image and concurrency are set with `--image` and `--max-challenges`.

### Claude and Codex commands

The `claude-sdk/*` and `codex/*` solvers run your own `claude` and `codex` commands,
so they use the same logins, settings and proxies as your terminal.

```env
# ~/.config/ctf-agent/config.env
CLAUDE_CLI=claude   # command on PATH or a path
CODEX_CLI=codex
```

Shell aliases are invisible to programs: if `claude` is an alias for a wrapper
(e.g. one that sets `ANTHROPIC_BASE_URL`), set `CLAUDE_CLI` to that wrapper. A
one-off override is `--claude-cli` / `--codex-cli`. The resolved commands are
printed at startup, and a command that can't be found stops the run with the setting to fix.

## Command reference

`ctf-solve [OPTIONS]`

| Option | Default | Description |
|--------|---------|-------------|
| `--challenge DIR` | | Solve one local challenge directory (no coordinator) |
| `--no-submit` | off | Dry run: never submit flags to CTFd |
| `--writeup` | off | Keep the winner alive for one final turn and save `<challenge>/writeup.md` |
| `--benchmark` | off | Keep all models running for up to 10 minutes after the first solve; requires `--challenge` |
| `--models SPEC` | `DEFAULT_MODELS` | Solver model; repeat to race several. Without a value: list available models |
| `--challenges-dir DIR` | `challenges` | Competition mode: where challenges are downloaded |
| `--max-challenges N` | `10` | Max challenges solved at once |
| `--coordinator claude\|codex` | `claude` | Coordinator backend (competition mode) |
| `--coordinator-model ID` | see above | Coordinator model |
| `--ctfd-url URL` | `CTFD_URL` | CTFd URL |
| `--ctfd-token TOKEN` | `CTFD_TOKEN` | CTFd API token |
| `--image NAME` | `ctf-sandbox` | Sandbox Docker image |
| `--claude-cli CMD` | `CLAUDE_CLI` | Claude Code command or path |
| `--codex-cli CMD` | `CODEX_CLI` | Codex command or path |
| `--msg-port N` | `0` (random free port) | Port for operator messages |
| `-v`, `--verbose` | off | Debug logging |

`ctf-msg [--port N] [--host HOST] "message"` sends a message to a running `ctf-solve`
(default host `127.0.0.1`; use the port the run printed).

## Watching and talking to solvers

- **Traces:** `./logs/trace-<challenge>-<model>-<time>.jsonl` (in the directory you ran from)
  records each solver's messages (`model_response`), tool calls and results, delivered
  findings and its session id (`session`).
- **Full transcripts:** the CLIs keep their own. Claude: `~/.claude/projects/<run dir>/<session>.jsonl`,
  Codex: `~/.codex/sessions/<date>/rollout-*-<thread>.jsonl`. The ids are logged at startup.
  After a run, `<CLAUDE_CLI> --resume <session>` (from the same directory) or
  `codex resume <thread>` reopens the conversation interactively. Tools then run on
  your machine, not in the sandbox, so don't let them execute challenge files.
- **Live messages:** every run prints a port; `ctf-msg --port <port> "hint"` posts a
  message that every solver receives within about five tool calls.

## Running several challenges at once

Separate `ctf-solve --challenge ...` processes can run side by side. Each sandbox
container is labelled with the process that created it, and a starting run only
removes containers whose process is gone. Each run gets its own message port.
All runs share your Claude/Codex quotas and the Docker host's memory.

```bash
for d in ./challs/rsa-1 ./challs/heap-2; do
  ctf-solve --challenge "$d" --no-submit > "$(basename "$d").log" 2>&1 &
done
wait
```

## Provider safety filters

A provider can stop a turn as a safety risk (e.g. OpenAI's "flagged for possible
cybersecurity risk"). Such a solver is restarted in a fresh session up to twice
(after 30s, then 60s); if it is blocked again it stops and the other models carry on.
For fewer blocks on OpenAI models, apply for OpenAI's cyber access program (the
link is in the error message).

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category | Tools |
|----------|-------|
| **Binary** | radare2, GDB, objdump, binwalk, strings, readelf |
| **Pwn** | pwntools, ROPgadget, angr, unicorn, capstone |
| **Crypto** | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs |
| **Forensics** | volatility3, Sleuthkit (mmls/fls/icat), foremost, exiftool |
| **Stego** | steghide, stegseek, zsteg, ImageMagick, tesseract OCR |
| **Web** | curl, nmap, Python requests, flask |
| **Misc** | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman |

The full list is in `sandbox/sandbox-tools.txt` (also at `/tools.txt` inside the container).

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously
- **Auto-spawn** — new challenges detected and attacked automatically
- **Coordinator LLM** — reads solver traces, crafts targeted technical guidance
- **Cross-solver insights** — findings shared between models via message bus
- **Docker sandboxes** — isolated containers with full CTF tooling
- **Operator messaging** — send hints to running solvers mid-competition

## Development

```bash
uv sync                 # also installs the dev tools (pytest, ruff, ty)
uv run pytest
uv run ruff check .
```

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`
