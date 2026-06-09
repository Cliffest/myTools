#!/usr/bin/env bash
# =============================================================================
# SSH Reverse Tunnel Manager — Robust Against Unstable Networks
# Bash 3.x compatible, key-based ed25519 auth, no sshpass needed
#
# Key features:
#   • Auto-reconnect on network drop
#   • Auto-cleanup of orphan sshd-session processes on remote
#     (the real "port stuck" fix — works WITHOUT sudo and WITHOUT ss PID info)
#   • Graceful shutdown to minimize orphans in the first place
#   • Per-server worker subprocesses with manager watchdog
#
# Usage:
#   ./ssh-R.sh [config_file]            Start manager
#   ./ssh-R.sh status [config_file]     Show status
#   ./ssh-R.sh clean  [config_file]     Force-clean remote ports & exit
# =============================================================================

set -uo pipefail

# ─── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m';  GREEN='\033[0;32m';  YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m';  BOLD='\033[1m'; NC='\033[0m'

# ─── Script Location ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# ─── Global Defaults ─────────────────────────────────────────────────────────
RETRY_INTERVAL="10"
SERVER_ALIVE_INTERVAL="30"
SERVER_ALIVE_COUNT_MAX="3"
MAX_RETRIES="0"
LOG_DIR="/tmp/ssh_tunnels"
PID_DIR="${SCRIPT_DIR}/pids"
LOG_LEVEL="INFO"
STABLE_AFTER="60"

# ─── Server name list (space-separated) ──────────────────────────────────────
SERVER_NAMES=""

# =============================================================================
# COMPAT: Simulate associative array via prefixed flat variables
# =============================================================================

set_cfg() {
    local section="$1" key="$2" value="$3"
    section="${section//-/_}"
    key="${key//-/_}"
    eval "_CFG__${section}__${key}=\"\$value\""
}

get_cfg() {
    local section="$1" key="$2" default="${3:-}"
    section="${section//-/_}"
    key="${key//-/_}"
    local varname="_CFG__${section}__${key}"
    eval "echo \"\${${varname}:-\$default}\""
}

# =============================================================================
# LOGGING
# =============================================================================

_log() {
    local level="$1" tag="$2" color="$3" screen_summary="$4"
    shift 4
    local msg="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local entry="[$timestamp][$level][$tag] $msg"

    mkdir -p "$LOG_DIR"

    # Per-tag detail log: always full fidelity
    echo "$entry" >> "${LOG_DIR}/${tag}.log"

    # combined.log: skip the periodic heartbeat — it drowns out everything else
    if [[ "$msg" != Heartbeat* ]]; then
        echo "$entry" >> "${LOG_DIR}/combined.log"
    fi

    # Decide screen visibility:
    #   - manager-tagged messages    → always shown
    #   - server-tagged summary      → shown (initial start / stable / went-down)
    #   - server-tagged everything   → file only (see <server>.log for detail)
    #   - DEBUG                      → only when LOG_LEVEL=DEBUG
    local show=0
    if [[ "$level" == "DEBUG" ]]; then
        [[ "$LOG_LEVEL" == "DEBUG" ]] && show=1
    elif [[ "$tag" == "manager" ]] || [[ "$screen_summary" == "1" ]]; then
        show=1
    fi

    if [[ "$show" == "1" ]]; then
        echo -e "${color}${entry}${NC}"
        # screen.log mirrors what the user sees, minus ANSI colors
        echo "$entry" >> "${LOG_DIR}/screen.log"
    fi
}

log_info()  { _log "INFO " "$1" "${GREEN}"  "0" "${@:2}"; }
log_warn()  { _log "WARN " "$1" "${YELLOW}" "0" "${@:2}"; }
log_error() { _log "ERROR" "$1" "${RED}"    "1" "${@:2}" >&2; }
log_debug() { _log "DEBUG" "$1" "${CYAN}"   "0" "${@:2}"; }

# Server-summary helpers — surface to screen + screen.log.
# Used only for: initial worker start, tunnel stable, tunnel went down.
log_summary()      { _log "INFO " "$1" "${BOLD}${GREEN}"  "1" "${@:2}"; }
log_summary_warn() { _log "WARN " "$1" "${BOLD}${YELLOW}" "1" "${@:2}"; }

# =============================================================================
# CONFIG PARSER  (INI-style, Bash 3 compatible)
# =============================================================================

parse_config() {
    local config_file="$1"

    if [[ ! -f "$config_file" ]]; then
        echo -e "${RED}Config file not found: $config_file${NC}"
        exit 1
    fi

    local current_section=""

    while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
        local line="${raw_line%%#*}"
        while [[ "$line" == [[:space:]]* ]]; do line="${line#?}"; done
        while [[ "$line" == *[[:space:]] ]]; do line="${line%?}"; done
        [[ -z "$line" ]] && continue

        # ── Section header ────────────────────────────────────────────────
        if [[ "$line" == \[*\] ]]; then
            current_section="${line#[}"
            current_section="${current_section%]}"
            while [[ "$current_section" == [[:space:]]* ]]; do
                current_section="${current_section#?}"
            done
            while [[ "$current_section" == *[[:space:]] ]]; do
                current_section="${current_section%?}"
            done

            if [[ "$current_section" != "global" ]]; then
                local already=0
                for s in $SERVER_NAMES; do
                    [[ "$s" == "$current_section" ]] && already=1 && break
                done
                [[ $already -eq 0 ]] && \
                    SERVER_NAMES="${SERVER_NAMES}${SERVER_NAMES:+ }${current_section}"
            fi
            continue
        fi

        # ── Key = Value ───────────────────────────────────────────────────
        if [[ "$line" == *=* ]]; then
            local key="${line%%=*}"
            local value="${line#*=}"

            while [[ "$key"   == [[:space:]]* ]]; do key="${key#?}";     done
            while [[ "$key"   == *[[:space:]] ]]; do key="${key%?}";     done
            while [[ "$value" == [[:space:]]* ]]; do value="${value#?}"; done
            while [[ "$value" == *[[:space:]] ]]; do value="${value%?}"; done

            [[ -z "$current_section" ]] && continue

            if [[ "$current_section" == "global" ]]; then
                case "$key" in
                    retry_interval)   RETRY_INTERVAL="$value"         ;;
                    server_alive)     SERVER_ALIVE_INTERVAL="$value"  ;;
                    server_alive_max) SERVER_ALIVE_COUNT_MAX="$value" ;;
                    max_retries)      MAX_RETRIES="$value"            ;;
                    stable_after)     STABLE_AFTER="$value"           ;;
                    log_dir)          LOG_DIR="$value"                ;;
                    log_level)        LOG_LEVEL="${value}"            ;;
                esac
            else
                set_cfg "$current_section" "$key" "$value"
            fi
        fi

    done < "$config_file"
}

# =============================================================================
# DEPENDENCY CHECK
# =============================================================================

check_deps() {
    local missing=""
    for dep in ssh; do
        command -v "$dep" &>/dev/null || missing="$missing $dep"
    done

    if [[ -n "$missing" ]]; then
        echo -e "${RED}Missing dependencies:${missing}${NC}"
        exit 1
    fi
}

# =============================================================================
# REMOTE CLEANUP — kill orphan sshd-session processes holding the forwarded port
# =============================================================================
# DIAGNOSIS (from real-world debug):
#
#   On this remote, an `ps -fu $USER | grep sshd` shows two kinds of entries:
#
#     PID  ... sshd-session: user@pts/N    ← LIVE session (has a TTY)
#     PID  ... sshd-session: user          ← ORPHAN (no TTY, dead tunnel)
#
#   When the network drops and the local SSH client is killed ungracefully,
#   the remote sshd doesn't notice for hours/days. The orphan keeps holding
#   the forwarded TCP port. Tools like ss/lsof/fuser cannot show its PID to
#   us because we lack sudo — but we CAN see it via ps under our own user.
#
#   The reliable, sudo-free signature of an orphan tunnel sshd is:
#       command contains "sshd-session:"
#       command does NOT contain "@pts"
#       PID is NOT our own cleanup-session sshd ($PPID)
#
# STRATEGY:
#   1. List all "sshd-session" processes owned by us (via ps -fu).
#   2. Skip the one running this cleanup (our $PPID).
#   3. Skip any that contain "@pts" (those are live interactive sessions
#      belonging to other terminals — leave them alone).
#   4. Kill everything else (TERM then KILL).
#   5. Optionally verify the port can be bound with python3 (no sudo needed).
# =============================================================================

cleanup_remote_port() {
    local server="$1"
    local ssh_alias="$2"
    local remote_port="$3"

    log_warn "$server" "Cleaning remote port $remote_port on $ssh_alias ..."

    # Build the remote command. Use a quoted heredoc to keep $ literal, then
    # substitute the port number with simple textual replacement.
    local remote_cmd
    remote_cmd=$(cat <<'REMOTE_EOF'
PORT_TO_FREE="__PORT__"
MY_SSHD_PID=$PPID
USER_NAME=$(id -un)

echo "[cleanup] user=$USER_NAME  my_sshd=$MY_SSHD_PID  port=$PORT_TO_FREE"
echo "[cleanup] --- current sshd processes for $USER_NAME ---"
ps -fu "$USER_NAME" 2>/dev/null | grep -E 'sshd' | grep -v grep || true

# -------------------------------------------------------------------------
# Step 1: identify orphan sshd-session processes
# -------------------------------------------------------------------------
# Orphan signature:
#   - command line contains "sshd-session:"
#   - command line does NOT contain "@pts"
#   - PID is NOT our own cleanup-session sshd
# -------------------------------------------------------------------------
ORPHAN_PIDS=""
while IFS= read -r line; do
    [ -z "$line" ] && continue
    pid=$(echo "$line" | awk '{print $2}')
    [ -z "$pid" ] && continue
    [ "$pid" = "$MY_SSHD_PID" ] && continue

    # Extract the command portion (everything after the time/tty fields).
    # We just check the whole line for the signatures — it's robust enough.
    if echo "$line" | grep -q 'sshd-session:' && \
       ! echo "$line" | grep -q '@pts' && \
       ! echo "$line" | grep -q 'grep'; then
        ORPHAN_PIDS="$ORPHAN_PIDS $pid"
    fi
done <<EOF
$(ps -fu "$USER_NAME" 2>/dev/null)
EOF

# Trim leading space
ORPHAN_PIDS=$(echo "$ORPHAN_PIDS" | sed 's/^ *//')

if [ -z "$ORPHAN_PIDS" ]; then
    echo "[cleanup] no orphan sshd-session processes found"
else
    echo "[cleanup] orphan PIDs detected: $ORPHAN_PIDS"
    for p in $ORPHAN_PIDS; do
        CMD=$(ps -p "$p" -o command= 2>/dev/null | head -1)
        echo "[cleanup] killing PID=$p  cmd=$CMD"
        kill -TERM "$p" 2>/dev/null || true

        # Wait up to 2 seconds for graceful exit
        n=0
        while kill -0 "$p" 2>/dev/null && [ $n -lt 20 ]; do
            sleep 0.1
            n=$((n+1))
        done

        if kill -0 "$p" 2>/dev/null; then
            echo "[cleanup]   SIGTERM ignored → SIGKILL PID=$p"
            kill -KILL "$p" 2>/dev/null || true
        fi
    done

    # Give the kernel a moment to release the port allocation
    sleep 2
fi

# -------------------------------------------------------------------------
# Step 2: verify port is bindable (no sudo needed — just try to bind)
# -------------------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    python3 - <<PYEOF
import socket, sys
port = $PORT_TO_FREE
ok = True
for host in ('127.0.0.1', '::1'):
    try:
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.close()
        print(f"[cleanup]   {host}:{port} ✓ free")
    except OSError as e:
        print(f"[cleanup]   {host}:{port} ✗ {e}")
        ok = False
sys.exit(0 if ok else 2)
PYEOF
    rc=$?
    if [ $rc -eq 0 ]; then
        echo "[cleanup] port $PORT_TO_FREE is FREE"
    else
        echo "[cleanup] WARNING: port $PORT_TO_FREE still held (rc=$rc)"
    fi
fi

exit 0
REMOTE_EOF
)

    # Substitute the port number (textual, since heredoc was quoted)
    remote_cmd="${remote_cmd//__PORT__/$remote_port}"

    if ssh \
        -o "BatchMode=yes" \
        -o "ConnectTimeout=10" \
        -o "StrictHostKeyChecking=no" \
        -o "ServerAliveInterval=5" \
        -o "ServerAliveCountMax=2" \
        "$ssh_alias" \
        "bash -c $(printf '%q' "$remote_cmd")" \
        >> "${LOG_DIR}/${server}.log" 2>&1
    then
        log_info "$server" "Remote cleanup OK — waiting 3s..."
        sleep 3
        return 0
    else
        local rc=$?
        log_warn "$server" "Remote cleanup SSH failed (rc=$rc) — will retry tunnel anyway"
        return 1
    fi
}

# =============================================================================
# DETECT "remote port forwarding failed" IN LOG
# =============================================================================

port_forward_failed() {
    local server="$1"
    local log_file="${LOG_DIR}/${server}.log"
    [[ -f "$log_file" ]] || return 1

    tail -n 30 "$log_file" 2>/dev/null \
        | grep -q "remote port forwarding failed"
}

# =============================================================================
# TUNNEL WORKER  (one subprocess per server)
# =============================================================================

run_tunnel_worker() {
    local server="$1"

    local ssh_alias remote_port local_port local_host
    local retry_interval server_alive server_alive_max max_retries stable_after

    ssh_alias=$(get_cfg        "$server" "ssh_alias"        "$server")
    remote_port=$(get_cfg      "$server" "remote_port"      "2222")
    local_port=$(get_cfg       "$server" "local_port"       "22")
    local_host=$(get_cfg       "$server" "local_host"       "localhost")
    retry_interval=$(get_cfg   "$server" "retry_interval"   "$RETRY_INTERVAL")
    server_alive=$(get_cfg     "$server" "server_alive"     "$SERVER_ALIVE_INTERVAL")
    server_alive_max=$(get_cfg "$server" "server_alive_max" "$SERVER_ALIVE_COUNT_MAX")
    max_retries=$(get_cfg      "$server" "max_retries"      "$MAX_RETRIES")
    stable_after=$(get_cfg     "$server" "stable_after"     "$STABLE_AFTER")

    local retry_count=0
    local tunnel_pid=""
    local tunnel_started_at=0
    local tunnel_announced_up=0
    local last_fail_was_port_conflict=0
    # Pre-clean on first launch if we suspect leftover state from a previous run
    local first_launch=1

    log_summary "$server" "Worker started → ${ssh_alias}:${remote_port} ← ${local_host}:${local_port}"

    # ── Kill current SSH child GRACEFULLY ─────────────────────────────────────
    # SIGTERM first → SSH sends "close port forward" to remote sshd → sshd
    # releases the port immediately. Only SIGKILL if SSH refuses to exit.
    _kill_tunnel() {
        if [[ -n "$tunnel_pid" ]] && kill -0 "$tunnel_pid" 2>/dev/null; then
            kill -TERM "$tunnel_pid" 2>/dev/null || true

            # Wait up to 3 seconds for clean exit
            local n=0
            while kill -0 "$tunnel_pid" 2>/dev/null && [[ $n -lt 30 ]]; do
                sleep 0.1
                n=$(( n + 1 ))
            done

            if kill -0 "$tunnel_pid" 2>/dev/null; then
                log_warn "$server" "SSH didn't exit gracefully — SIGKILL"
                kill -KILL "$tunnel_pid" 2>/dev/null || true
            fi

            wait "$tunnel_pid" 2>/dev/null || true
            log_debug "$server" "Killed SSH PID $tunnel_pid"
        fi
        tunnel_pid=""
        tunnel_started_at=0
        tunnel_announced_up=0
        rm -f "${PID_DIR}/${server}.pid"
    }

    # ── Start one SSH tunnel (key-based) ──────────────────────────────────────
    _start_tunnel() {
        ssh \
            -o "ServerAliveInterval=${server_alive}" \
            -o "ServerAliveCountMax=${server_alive_max}" \
            -o "ExitOnForwardFailure=yes" \
            -o "StrictHostKeyChecking=no" \
            -o "BatchMode=yes" \
            -o "ConnectTimeout=15" \
            -o "TCPKeepAlive=yes" \
            -o "Compression=yes" \
            -N \
            -R "${remote_port}:${local_host}:${local_port}" \
            "${ssh_alias}" \
            2>> "${LOG_DIR}/${server}.log" &

        tunnel_pid=$!
        tunnel_started_at=$(date '+%s')
        tunnel_announced_up=0
        mkdir -p "$PID_DIR"
        echo "$tunnel_pid" > "${PID_DIR}/${server}.pid"
        log_debug "$server" "SSH launched (PID: $tunnel_pid)"
    }

    _is_alive() {
        [[ -n "$tunnel_pid" ]] && kill -0 "$tunnel_pid" 2>/dev/null
    }

    _tunnel_age() {
        local now
        now=$(date '+%s')
        echo $(( now - tunnel_started_at ))
    }

    # ── Cleanup on signal ─────────────────────────────────────────────────────
    trap '_kill_tunnel; log_info "$server" "Worker stopped."; exit 0' \
        SIGTERM SIGINT SIGHUP

    # =========================================================================
    # RETRY LOOP
    # =========================================================================
    while true; do

        if ! _is_alive; then
            if [[ -n "$tunnel_pid" ]]; then
                local age
                age=$(_tunnel_age)
                retry_count=$(( retry_count + 1 ))
                if [[ "$tunnel_announced_up" -eq 1 ]]; then
                    log_summary_warn "$server" \
                        "Tunnel went down after ${age}s, reconnecting (attempt $retry_count)"
                else
                    log_warn "$server" \
                        "SSH exited before tunnel became stable after ${age}s (attempt $retry_count)"
                fi
            fi

            # ── Max retries check ─────────────────────────────────────────────
            if [[ "$max_retries" -gt 0 ]] && \
               [[ "$retry_count" -ge "$max_retries" ]]; then
                log_error "$server" \
                    "Reached max retries ($max_retries). Worker stopping."
                exit 1
            fi

            # ── Back-off wait (skip on very first attempt) ────────────────────
            if [[ "$retry_count" -gt 0 ]]; then
                log_warn "$server" \
                    "Tunnel down. Retry #${retry_count} in ${retry_interval}s..."
                sleep "$retry_interval"
            fi

            _kill_tunnel

            # ── Pre-clean on first launch (leftover from previous manager) ────
            if [[ "$first_launch" -eq 1 ]]; then
                log_info "$server" "First launch — pre-cleaning remote port (in case of leftover from previous run)"
                cleanup_remote_port "$server" "$ssh_alias" "$remote_port" || true
                first_launch=0
            fi

            # ── Targeted cleanup if last failure was port conflict ────────────
            if [[ "$last_fail_was_port_conflict" -eq 1 ]]; then
                log_warn "$server" "Last failure was port conflict — cleaning remote"
                cleanup_remote_port "$server" "$ssh_alias" "$remote_port" || true
                last_fail_was_port_conflict=0
            fi

            # ── Launch the tunnel ─────────────────────────────────────────────
            _start_tunnel
            log_info "$server" \
                "SSH started (PID: $tunnel_pid) — confirming tunnel stability..."

            # Grace period — give SSH a chance to fail fast
            sleep 3

            if _is_alive; then
                log_debug "$server" "Startup probe OK (PID: $tunnel_pid)"
                last_fail_was_port_conflict=0
            else
                local age
                age=$(_tunnel_age)
                retry_count=$(( retry_count + 1 ))
                if port_forward_failed "$server"; then
                    log_warn "$server" \
                        "Port $remote_port still held — will clean up on next retry"
                    last_fail_was_port_conflict=1
                else
                    log_warn "$server" \
                        "SSH exited during startup after ${age}s (attempt $retry_count) — see ${LOG_DIR}/${server}.log"
                    last_fail_was_port_conflict=0
                fi
                _kill_tunnel
                continue
            fi
        fi

        if [[ "$tunnel_announced_up" -eq 0 ]]; then
            local age
            age=$(_tunnel_age)
            if [[ "$age" -ge "$stable_after" ]]; then
                log_summary "$server" "✓ Tunnel stable for ${age}s (PID: $tunnel_pid)"
                retry_count=0
                tunnel_announced_up=1
            fi
        fi

        log_debug "$server" "Heartbeat OK (PID: $tunnel_pid)"
        sleep "$retry_interval"
    done
}

# =============================================================================
# MANAGER
# =============================================================================

set_wpid() { eval "_WPID__${1//-/_}=\"$2\""; }
get_wpid() { eval "echo \"\${_WPID__${1//-/_}:-}\""; }

spawn_worker() {
    local server="$1"
    run_tunnel_worker "$server" &
    local wpid=$!
    set_wpid "$server" "$wpid"
    log_info "manager" "Spawned worker '$server' (PID: $wpid)"
}

stop_all_workers() {
    log_info "manager" "Stopping all workers gracefully..."

    # Send SIGTERM to all workers
    for server in $SERVER_NAMES; do
        local pid
        pid=$(get_wpid "$server")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
            log_info "manager" "SIGTERM → worker '$server' (PID: $pid)"
        fi
    done

    # Wait up to 8s total for all workers to exit
    local waited=0
    while [[ $waited -lt 80 ]]; do
        local any_alive=0
        for server in $SERVER_NAMES; do
            local pid
            pid=$(get_wpid "$server")
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                any_alive=1
                break
            fi
        done
        [[ $any_alive -eq 0 ]] && break
        sleep 0.1
        waited=$(( waited + 1 ))
    done

    # Force-kill any holdouts
    for server in $SERVER_NAMES; do
        local pid
        pid=$(get_wpid "$server")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            log_warn "manager" "Worker '$server' (PID: $pid) — SIGKILL"
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done

    log_info "manager" "All workers stopped."
}

monitor_workers() {
    log_info "manager" "Monitoring [ $SERVER_NAMES ] ..."
    while true; do
        for server in $SERVER_NAMES; do
            local pid
            pid=$(get_wpid "$server")
            if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
                log_warn "manager" \
                    "Worker '$server' (PID: ${pid:-?}) gone — restarting..."
                spawn_worker "$server"
            fi
        done
        sleep 5
    done
}

# =============================================================================
# STATUS / CLEAN COMMANDS
# =============================================================================

show_status() {
    echo -e "\n${BOLD}${BLUE}════════════════════════════════════════${NC}"
    echo -e "${BOLD}${BLUE}   SSH Tunnel Status${NC}"
    echo -e "${BOLD}${BLUE}════════════════════════════════════════${NC}"

    for server in $SERVER_NAMES; do
        local pid_file="${PID_DIR}/${server}.pid"
        local ssh_alias remote_port local_port

        ssh_alias=$(get_cfg   "$server" "ssh_alias"   "$server")
        remote_port=$(get_cfg "$server" "remote_port" "?")
        local_port=$(get_cfg  "$server" "local_port"  "?")

        printf "  ${BOLD}%-20s${NC} " "$server"

        if [[ -f "$pid_file" ]]; then
            local pid
            pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                echo -e "${GREEN}● UP${NC}   SSH PID=$pid  ${ssh_alias}:${remote_port} ← localhost:${local_port}"
            else
                echo -e "${RED}✗ DOWN${NC}  stale PID=$pid"
            fi
        else
            echo -e "${YELLOW}? UNKNOWN${NC}  (no PID file)"
        fi
    done

    echo -e "${BOLD}${BLUE}════════════════════════════════════════${NC}\n"
}

do_clean() {
    echo -e "${BOLD}Force-cleaning remote ports for all servers...${NC}"
    for server in $SERVER_NAMES; do
        local ssh_alias remote_port
        ssh_alias=$(get_cfg   "$server" "ssh_alias"   "$server")
        remote_port=$(get_cfg "$server" "remote_port" "2222")
        echo -e "${CYAN}→ $server ($ssh_alias:$remote_port)${NC}"
        cleanup_remote_port "$server" "$ssh_alias" "$remote_port" || true
        # Show the result tail from the log for the user
        echo -e "${CYAN}  --- cleanup log tail ---${NC}"
        tail -n 25 "${LOG_DIR}/${server}.log" 2>/dev/null | sed 's/^/    /'
    done
    echo -e "${GREEN}Done.${NC}"
}

# =============================================================================
# MAIN
# =============================================================================

usage() {
    cat <<EOF
${BOLD}Usage:${NC}
  $0 [config_file]           Start tunnel manager
  $0 status [config_file]    Show tunnel status
  $0 clean  [config_file]    Force-clean stuck remote ports and exit
  $0 --help                  This message

${BOLD}Defaults:${NC}
  config_file = \$(dirname \$0)/tunnels.conf

${BOLD}Environment:${NC}
  LOG_LEVEL=DEBUG $0 ...     Verbose output

${BOLD}Stopping safely:${NC}
  Send SIGTERM (default 'kill', NOT 'kill -9') to manager PID:
    kill \$(cat ${PID_DIR}/manager.pid)
  This allows clean tunnel teardown on the remote.
EOF
}

graceful_shutdown() {
    log_info "manager" "Shutdown signal received."
    stop_all_workers
    rm -f "${PID_DIR}/manager.pid"
    exit 0
}

main() {
    local config_file=""
    local cmd="start"

    for arg in "$@"; do
        case "$arg" in
            -h|--help) usage; exit 0    ;;
            status)    cmd="status"     ;;
            clean)     cmd="clean"      ;;
            *)         config_file="$arg" ;;
        esac
    done

    config_file="${config_file:-$(dirname "$0")/tunnels.conf}"

    parse_config "$config_file"

    if [[ -z "$SERVER_NAMES" ]]; then
        echo -e "${RED}No server sections found in: $config_file${NC}"
        exit 1
    fi

    check_deps
    mkdir -p "$LOG_DIR" "$PID_DIR"

    case "$cmd" in
        status) show_status; exit 0 ;;
        clean)  do_clean;    exit 0 ;;
    esac

    # ── Prevent multiple managers ────────────────────────────────────────
    if [[ -f "${PID_DIR}/manager.pid" ]]; then
        local old_pid
        old_pid=$(cat "${PID_DIR}/manager.pid" 2>/dev/null || echo "")
        if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
            echo -e "${RED}Manager already running (PID: $old_pid)${NC}"
            echo -e "${YELLOW}Stop it first:  kill $old_pid${NC}"
            exit 1
        fi
    fi

    echo $$ > "${PID_DIR}/manager.pid"
    trap 'graceful_shutdown' SIGINT SIGTERM SIGHUP

    echo -e "${BOLD}${GREEN}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║    SSH Reverse Tunnel Manager            ║"
    echo "  ╠══════════════════════════════════════════╣"
    printf  "  ║  Servers : %-30s║\n" "$SERVER_NAMES"
    printf  "  ║  Log dir : %-30s║\n" "$LOG_DIR"
    printf  "  ║  PID     : %-30s║\n" "$$"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -e "${CYAN}Stop with:  kill $$${NC}"
    echo -e "${CYAN}Status:     $0 status $config_file${NC}\n"

    for server in $SERVER_NAMES; do
        spawn_worker "$server"
    done

    monitor_workers
}

main "$@"
