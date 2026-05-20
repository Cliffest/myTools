#!/usr/bin/env bash
# =============================================================================
# SSH Reverse Tunnel Manager — Multi-Server Edition
# Supports ~/.ssh/config aliases and password authentication via sshpass
# Usage: ./ssh_tunnel.sh [config_file]
# =============================================================================

set -uo pipefail

# ─── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m';    GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m';   CYAN='\033[0;36m'; MAGENTA='\033[0;35m'
BOLD='\033[1m';      NC='\033[0m'

# ─── Global Defaults (can be overridden per-server in config) ─────────────────
RETRY_INTERVAL="${RETRY_INTERVAL:-10}"
SERVER_ALIVE_INTERVAL="${SERVER_ALIVE_INTERVAL:-30}"
SERVER_ALIVE_COUNT_MAX="${SERVER_ALIVE_COUNT_MAX:-3}"
MAX_RETRIES="${MAX_RETRIES:-0}"
LOG_DIR="${LOG_DIR:-/tmp/ssh_tunnels}"
PID_DIR="${PID_DIR:-/tmp/ssh_tunnels/pids}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

# =============================================================================
# LOGGING
# =============================================================================

_log() {
    local level="$1" tag="$2" color="$3"
    shift 3
    local msg="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local entry="[$timestamp][$level][${tag}] $msg"

    mkdir -p "$LOG_DIR"
    echo "$entry" >> "${LOG_DIR}/${tag}.log"
    echo "$entry" >> "${LOG_DIR}/combined.log"

    case "$level" in
        DEBUG) [[ "${LOG_LEVEL}" == "DEBUG" ]] || return 0 ;;
    esac

    echo -e "${color}${entry}${NC}"
}

log_info()  { _log "INFO " "$1" "${GREEN}"   "${@:2}"; }
log_warn()  { _log "WARN " "$1" "${YELLOW}"  "${@:2}"; }
log_error() { _log "ERROR" "$1" "${RED}"     "${@:2}" >&2; }
log_debug() { _log "DEBUG" "$1" "${CYAN}"    "${@:2}"; }

# =============================================================================
# CONFIG PARSING
# =============================================================================
# Config file format (INI-style):
#
#   [global]
#   retry_interval    = 10
#   server_alive      = 30
#   log_dir           = /tmp/ssh_tunnels
#
#   [server_alpha]
#   ssh_alias         = alpha              # name in ~/.ssh/config
#   remote_port       = 2222
#   local_port        = 22
#   local_host        = localhost
#   password          = secret123         # or use password_cmd
#   password_cmd      = pass show alpha   # command to retrieve password
#   retry_interval    = 5                 # override global
#
#   [server_beta]
#   ssh_alias         = beta
#   remote_port       = 2223
#   local_port        = 22
#   password_cmd      = cat /run/secrets/beta_pass
# =============================================================================

declare -A SERVERS          # SERVERS[name]=1
declare -A SERVER_CONFIG    # SERVER_CONFIG[name.key]=value

parse_config() {
    local config_file="$1"
    [[ -f "$config_file" ]] || { echo -e "${RED}Config file not found: $config_file${NC}"; exit 1; }

    local current_section=""
    local lineno=0

    while IFS= read -r line || [[ -n "$line" ]]; do
        (( lineno++ )) || true
        # Strip comments and trim whitespace
        line="${line%%#*}"
        line="${line//[$'\t' ]/}"
        line="${line## }"; line="${line%% }"
        [[ -z "$line" ]] && continue

        # Section header
        if [[ "$line" =~ ^\[(.+)\]$ ]]; then
            current_section="${BASH_REMATCH[1]}"
            if [[ "$current_section" != "global" ]]; then
                SERVERS["$current_section"]=1
            fi
            continue
        fi

        # Key=Value (allow spaces around =)
        if [[ "$line" =~ ^([^=]+)=(.*)$ ]]; then
            local key value
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            # trim spaces
            key="${key%"${key##*[![:space:]]}"}"
            value="${value#"${value%%[![:space:]]*}"}"

            if [[ "$current_section" == "global" ]]; then
                # Apply global defaults
                case "$key" in
                    retry_interval)    RETRY_INTERVAL="$value" ;;
                    server_alive)      SERVER_ALIVE_INTERVAL="$value" ;;
                    server_alive_max)  SERVER_ALIVE_COUNT_MAX="$value" ;;
                    max_retries)       MAX_RETRIES="$value" ;;
                    log_dir)           LOG_DIR="$value" ;;
                    log_level)         LOG_LEVEL="${value^^}" ;;
                esac
            elif [[ -n "$current_section" ]]; then
                SERVER_CONFIG["${current_section}.${key}"]="$value"
            fi
        fi
    done < "$config_file"
}

get_server_cfg() {
    local server="$1" key="$2" default="${3:-}"
    echo "${SERVER_CONFIG["${server}.${key}"]:-$default}"
}

# =============================================================================
# DEPENDENCY CHECK
# =============================================================================

check_deps() {
    local missing=()
    for dep in ssh sshpass; do
        command -v "$dep" &>/dev/null || missing+=("$dep")
    done

    if (( ${#missing[@]} > 0 )); then
        echo -e "${RED}Missing dependencies: ${missing[*]}${NC}"
        echo -e "${YELLOW}Install with: sudo apt install sshpass   # or brew install hudochenkov/sshpass/sshpass${NC}"
        exit 1
    fi
}

# =============================================================================
# PASSWORD RESOLUTION
# =============================================================================

resolve_password() {
    local server="$1"
    local password password_cmd

    password=$(get_server_cfg "$server" "password" "")
    password_cmd=$(get_server_cfg "$server" "password_cmd" "")

    if [[ -n "$password_cmd" ]]; then
        # Execute command to get password (e.g., pass, secret-tool, cat file)
        password=$(eval "$password_cmd" 2>/dev/null) || {
            log_error "$server" "Failed to retrieve password via: $password_cmd"
            return 1
        }
    fi

    if [[ -z "$password" ]]; then
        log_error "$server" "No password or password_cmd configured"
        return 1
    fi

    echo "$password"
}

# =============================================================================
# TUNNEL WORKER (runs as background subprocess per server)
# =============================================================================

run_tunnel_worker() {
    local server="$1"

    local ssh_alias retry_interval remote_port local_port local_host
    local server_alive server_alive_max max_retries

    ssh_alias=$(get_server_cfg    "$server" "ssh_alias"     "$server")
    remote_port=$(get_server_cfg  "$server" "remote_port"   "2222")
    local_port=$(get_server_cfg   "$server" "local_port"    "22")
    local_host=$(get_server_cfg   "$server" "local_host"    "localhost")
    retry_interval=$(get_server_cfg "$server" "retry_interval" "$RETRY_INTERVAL")
    server_alive=$(get_server_cfg "$server" "server_alive"  "$SERVER_ALIVE_INTERVAL")
    server_alive_max=$(get_server_cfg "$server" "server_alive_max" "$SERVER_ALIVE_COUNT_MAX")
    max_retries=$(get_server_cfg  "$server" "max_retries"   "$MAX_RETRIES")

    local retry_count=0
    local tunnel_pid=""

    log_info "$server" "Worker started — ${ssh_alias}:${remote_port} ← ${local_host}:${local_port}"

    # ── Inner: start one SSH tunnel ──────────────────────────────────────────
    _start_tunnel() {
        local password
        password=$(resolve_password "$server") || return 1

        log_debug "$server" "Launching SSH tunnel..."
        SSHPASS="$password" sshpass -e \
            ssh \
            -o "ServerAliveInterval=${server_alive}" \
            -o "ServerAliveCountMax=${server_alive_max}" \
            -o "ExitOnForwardFailure=yes" \
            -o "StrictHostKeyChecking=no" \
            -o "BatchMode=no" \
            -o "ConnectTimeout=15" \
            -o "TCPKeepAlive=yes" \
            -o "PasswordAuthentication=yes" \
            -N \
            -R "${remote_port}:${local_host}:${local_port}" \
            "${ssh_alias}" \
            2>>"${LOG_DIR}/${server}.log" &

        tunnel_pid=$!
        echo "$tunnel_pid" > "${PID_DIR}/${server}.pid"
        log_debug "$server" "SSH PID: $tunnel_pid"
    }

    # ── Inner: kill current tunnel ───────────────────────────────────────────
    _kill_tunnel() {
        if [[ -n "$tunnel_pid" ]] && kill -0 "$tunnel_pid" 2>/dev/null; then
            kill "$tunnel_pid" 2>/dev/null || true
            wait "$tunnel_pid" 2>/dev/null || true
            log_debug "$server" "Killed tunnel PID: $tunnel_pid"
        fi
        tunnel_pid=""
        rm -f "${PID_DIR}/${server}.pid"
    }

    # ── Inner: is tunnel process alive? ──────────────────────────────────────
    _is_alive() {
        [[ -n "$tunnel_pid" ]] && kill -0 "$tunnel_pid" 2>/dev/null
    }

    # ── Cleanup on worker exit ───────────────────────────────────────────────
    trap '_kill_tunnel; log_info "$server" "Worker exiting."; exit 0' SIGTERM SIGINT

    # ── Main retry loop ──────────────────────────────────────────────────────
    while true; do
        if ! _is_alive; then
            if (( max_retries > 0 && retry_count >= max_retries )); then
                log_error "$server" "Max retries (${max_retries}) reached. Worker stopping."
                exit 1
            fi

            if (( retry_count > 0 )); then
                log_warn "$server" "Tunnel lost. Retry #${retry_count} in ${retry_interval}s..."
                sleep "$retry_interval"
            fi

            _kill_tunnel
            _start_tunnel || {
                (( retry_count++ )) || true
                log_warn "$server" "Failed to start tunnel. Will retry."
                sleep "$retry_interval"
                continue
            }

            # Grace period — detect immediate failures
            sleep 3
            if _is_alive; then
                log_info "$server" "✓ Tunnel UP (PID: $tunnel_pid)"
                retry_count=0
            else
                (( retry_count++ )) || true
                log_warn "$server" "Tunnel exited immediately (attempt $retry_count)"
                continue
            fi
        fi

        log_debug "$server" "Heartbeat OK (PID: $tunnel_pid)"
        sleep "$retry_interval"
    done
}

# =============================================================================
# MANAGER: spawn / monitor worker processes
# =============================================================================

declare -A WORKER_PIDS   # WORKER_PIDS[server]=pid

spawn_worker() {
    local server="$1"
    run_tunnel_worker "$server" &
    WORKER_PIDS["$server"]=$!
    log_info "manager" "Spawned worker for '${server}' (PID: ${WORKER_PIDS[$server]})"
}

stop_all_workers() {
    log_info "manager" "Stopping all tunnel workers..."
    for server in "${!WORKER_PIDS[@]}"; do
        local pid="${WORKER_PIDS[$server]}"
        if kill -0 "$pid" 2>/dev/null; then
            kill -SIGTERM "$pid" 2>/dev/null || true
            log_info "manager" "Sent SIGTERM to worker '${server}' (PID: $pid)"
        fi
    done

    # Wait for all workers
    for server in "${!WORKER_PIDS[@]}"; do
        wait "${WORKER_PIDS[$server]}" 2>/dev/null || true
    done

    log_info "manager" "All workers stopped."
}

monitor_workers() {
    while true; do
        for server in "${!WORKER_PIDS[@]}"; do
            local pid="${WORKER_PIDS[$server]}"
            if ! kill -0 "$pid" 2>/dev/null; then
                log_warn "manager" "Worker '${server}' (PID: $pid) died — restarting..."
                spawn_worker "$server"
            fi
        done
        sleep 5
    done
}

# =============================================================================
# STATUS DISPLAY
# =============================================================================

show_status() {
    local config_file="${1:-}"
    if [[ -n "$config_file" && -f "$config_file" ]]; then
        parse_config "$config_file"
    fi

    echo -e "\n${BOLD}${BLUE}══════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${BLUE}  SSH Tunnel Status${NC}"
    echo -e "${BOLD}${BLUE}══════════════════════════════════════════════════${NC}"

    for server in "${!SERVERS[@]}"; do
        local pid_file="${PID_DIR}/${server}.pid"
        local ssh_alias remote_port local_port
        ssh_alias=$(get_server_cfg  "$server" "ssh_alias"   "$server")
        remote_port=$(get_server_cfg "$server" "remote_port" "?")
        local_port=$(get_server_cfg  "$server" "local_port"  "?")

        printf "  ${BOLD}%-20s${NC} " "$server"

        if [[ -f "$pid_file" ]]; then
            local pid
            pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                echo -e "${GREEN}● UP${NC}   (SSH PID: $pid)  ${ssh_alias}:${remote_port} ← localhost:${local_port}"
            else
                echo -e "${RED}✗ DOWN${NC} (stale PID: $pid)"
            fi
        else
            echo -e "${YELLOW}? UNKNOWN${NC} (no PID file)"
        fi
    done

    echo -e "${BOLD}${BLUE}══════════════════════════════════════════════════${NC}\n"
}

# =============================================================================
# ENTRY POINT
# =============================================================================

usage() {
    cat <<EOF
${BOLD}Usage:${NC}
  $0 [OPTIONS] <config_file>
  $0 status <config_file>

${BOLD}Options:${NC}
  -h, --help        Show this help
  status            Show tunnel status and exit

${BOLD}Environment overrides:${NC}
  LOG_LEVEL=DEBUG   $0 tunnels.conf
EOF
}

main() {
    local config_file=""
    local cmd="start"

    for arg in "$@"; do
        case "$arg" in
            -h|--help) usage; exit 0 ;;
            status)    cmd="status" ;;
            *)         config_file="$arg" ;;
        esac
    done

    config_file="${config_file:-$(dirname "$0")/tunnels.conf}"

    if [[ "$cmd" == "status" ]]; then
        parse_config "$config_file"
        show_status
        exit 0
    fi

    check_deps
    parse_config "$config_file"
    mkdir -p "$LOG_DIR" "$PID_DIR"

    if (( ${#SERVERS[@]} == 0 )); then
        echo -e "${RED}No server sections found in config: $config_file${NC}"
        exit 1
    fi

    # Main process PID
    echo $$ > "${PID_DIR}/manager.pid"
    trap 'stop_all_workers; rm -f "${PID_DIR}/manager.pid"; exit 0' SIGINT SIGTERM

    echo -e "${BOLD}${GREEN}"
    echo "  ╔══════════════════════════════════════════════╗"
    echo "  ║     SSH Reverse Tunnel Manager               ║"
    echo "  ║     Servers: ${#SERVERS[@]}  |  Log: $LOG_DIR"
    echo "  ╚══════════════════════════════════════════════╝"
    echo -e "${NC}"

    # Spawn all workers in parallel (async)
    for server in "${!SERVERS[@]}"; do
        spawn_worker "$server"
    done

    log_info "manager" "All ${#SERVERS[@]} worker(s) spawned. Monitoring..."
    monitor_workers
}

main "$@"
