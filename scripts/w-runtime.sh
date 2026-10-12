#!/usr/bin/env bash
# KINTSUGI-Omega runtime for the autonomy window W, inside WSL2 Ubuntu (ruling D2, Stage 13a).
#
#   scripts/w-runtime.sh check    prerequisites and the runtime file, nothing started
#   scripts/w-runtime.sh up       Temporal dev server, Watchdog (crew + Surgeon pools), supervisor,
#                                 hit forwarder, ingress, poller, fleet; AuditAnchor at T0
#   scripts/w-runtime.sh status   what is running
#   scripts/w-runtime.sh down     fleet down, then everything else in reverse order
#
# Run it from the repository's WSL path (e.g. /mnt/c/myspace/hacckathons/kintsugi-omega-master).
# It installs nothing: `check` names what is missing. It is never started from inside the Claude
# desktop app (plan §2e.5). Keep the Windows host awake for the whole of W with
# scripts/keep-awake.ps1, run in a Windows terminal beside it.
#
# Environment:
#   KINTSUGI_RUNTIME   the runtime file (JSON, outside the repository; kintsugi/crew/runtime.py)
#   KINTSUGI_ENV_FILE  optional env file with NEATLOGS_* credentials (default: .env)
#   TEMPORAL_PORT      default 7233
#   INGRESS_PORT       default 8787
#   CHAOS_PORT         default 8790
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$REPO/.state/runtime"
LOGS="$STATE/logs"
PIDS="$STATE/pids"
TEMPORAL_PORT="${TEMPORAL_PORT:-7233}"
INGRESS_PORT="${INGRESS_PORT:-8787}"
CHAOS_PORT="${CHAOS_PORT:-8790}"
TARGET="127.0.0.1:${TEMPORAL_PORT}"
# A Linux virtualenv beside the Windows .venv would fight over the same directory.
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/kintsugi-omega-master}"
export ENTIRE_TEST_TTY=0 GIT_TERMINAL_PROMPT=0

# The processes, in start order. `down` stops them in reverse.
SERVICES=(temporal watchdog forward ingress poller fleet)

say() { printf '[w-runtime] %s\n' "$*"; }
die() { say "$*" >&2; exit 1; }

load_env() {
  local file="${KINTSUGI_ENV_FILE:-$REPO/.env}"
  if [[ -f "$file" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$file"
    set +a
  fi
}

need() {
  local missing=()
  for tool in "$@"; do command -v "$tool" >/dev/null 2>&1 || missing+=("$tool"); done
  ((${#missing[@]} == 0)) || die "missing: ${missing[*]} (install them in WSL first)"
}

check() {
  need git uv entire gh temporal docker
  [[ "$(uname -s)" == "Linux" ]] || die "run inside WSL2 Ubuntu (ruling D2)"
  docker info >/dev/null 2>&1 || die "the Docker engine in WSL is not running"
  docker info 2>/dev/null | grep -q runsc || say "warning: gVisor (runsc) is not registered with Docker"
  [[ -n "${KINTSUGI_RUNTIME:-}" ]] || die "KINTSUGI_RUNTIME must name the runtime file"
  [[ -f "$KINTSUGI_RUNTIME" ]] || die "$KINTSUGI_RUNTIME does not exist"
  case "$(realpath "$KINTSUGI_RUNTIME")" in
    "$REPO"/*) die "the runtime file must live outside the repository" ;;
  esac
  [[ -f "$REPO/.kintsugi/window.json" ]] || die "no window record: W starts with 'kintsugi t0 genesis'"
  [[ "$(git -C "$REPO" remote get-url origin)" == *"/kintsugi-omega-master.git" ]] \
    || die "origin is not the Master Edition repository"
  (cd "$REPO" && uv sync --frozen --quiet)
  (cd "$REPO" && uv run python -m kintsugi.crew.runtime check)
}

pid_of() { [[ -f "$PIDS/$1" ]] && cat "$PIDS/$1"; }
alive() { local pid; pid="$(pid_of "$1")" && [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; }

spawn() {  # spawn NAME COMMAND... : detached, logged, pid recorded
  local name="$1"; shift
  if alive "$name"; then say "$name already running (pid $(pid_of "$name"))"; return; fi
  (cd "$REPO" && setsid nohup "$@" >>"$LOGS/$name.log" 2>&1 & echo $! >"$PIDS/$name")
  say "$name started (pid $(pid_of "$name"), log $LOGS/$name.log)"
}

wait_port() {
  for _ in $(seq 1 60); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
    sleep 1
  done
  die "nothing listens on port $1"
}

up() {
  load_env
  check
  mkdir -p "$LOGS" "$PIDS"
  spawn temporal temporal server start-dev --ip 127.0.0.1 --port "$TEMPORAL_PORT" --headless \
    --db-filename "$STATE/temporal.sqlite"
  wait_port "$TEMPORAL_PORT"
  spawn watchdog uv run kintsugi control watchdog --target "$TARGET" \
    --crew kintsugi.crew.runtime:crew --chaos-port "$CHAOS_PORT" \
    --ingress-admin "http://127.0.0.1:${INGRESS_PORT}/admin/availability"
  (cd "$REPO" && uv run kintsugi control supervisor --target "$TARGET")
  (cd "$REPO" && uv run python -m kintsugi.crew.runtime start-anchors --target "$TARGET")
  spawn forward uv run kintsugi control forward --target "$TARGET"
  spawn ingress uv run kintsugi senses ingress --host 127.0.0.1 --port "$INGRESS_PORT"
  spawn poller uv run kintsugi senses poll
  spawn fleet uv run kintsugi fleet up
  say "up. Public ingress: point the tunnel (cloudflared) at 127.0.0.1:${INGRESS_PORT}, then"
  say "run 'kintsugi senses provision --public-url <tunnel>'. Anchor post texts: .state/anchors/."
}

status() {
  for name in "${SERVICES[@]}"; do
    if alive "$name"; then say "$name: running (pid $(pid_of "$name"))"; else say "$name: stopped"; fi
  done
}

down() {
  if alive fleet; then (cd "$REPO" && uv run kintsugi fleet down) || true; fi
  for ((i = ${#SERVICES[@]} - 1; i >= 0; i--)); do
    local name="${SERVICES[i]}"
    if alive "$name"; then
      kill -TERM -- "-$(pid_of "$name")" 2>/dev/null || kill -TERM "$(pid_of "$name")" || true
      say "$name stopped"
    fi
    rm -f "$PIDS/$name"
  done
}

case "${1:-}" in
  check) load_env; check ;;
  up) up ;;
  status) status ;;
  down) down ;;
  *) die "usage: $0 {check|up|status|down}" ;;
esac
