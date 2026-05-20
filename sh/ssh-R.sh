#!/usr/bin/env bash
# =============================================================================
# SSH Reverse Tunnel Manager — Multi-Server, Bash 3.x Compatible
# No associative arrays; uses flat prefixed variables + name list
# Usage: ./ssh-R.sh [config_file]
# =============================================================================

set -uo pipefail

# ─── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m';  GREEN='\033[0;32m';  YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m';  BOLD='\033[1m'; NC='\033[0m'

# ─── Global Defaults ─────────────────────────────────────────────────────────
RETRY_INTERVAL="10"
SERVER_ALIVE_INTERVAL="30"
SERVER_ALIVE_COUNT_MAX="3"
MAX_RETRIES="0"
LOG_DIR="/tmp/ssh_tunnels"
PID_DIR="/tmp/ssh_tunnels/pids"
LOG_LEVEL="INFO"

# ─── Server name list (space-separated) ──────────────────────────────────────
SERVER_NAMES=""

# =============================================================================
# COMPAT: Simulate associative array via prefixed flat variables
#   set_cfg  section key value   →  _CFG__section__key=value
#   get_cfg  section key default →  echo $value
# =============================================================================

set_cfg() {
    local section="$1" key="$2" value="$3"
    # sanitize: replace - with _ so variable names stay valid
    section="${section//-/_}"
    key="${key//-/_}"
    eval "_CFG__${section}__${key}=\"\$value\""
}

get_cfg() {
    local section="$1" key="$2" default="${3:-}"
    section="${section//-/_}"
    key="${key//-/_}"
    local varname="_CFG__${section}__${key}"
    # indirect expansion compatible with bash 3
    eval "echo \"\${${varname}:-\$default}\""
}

# =============================================================================
# LOGGING
# =============================================================================

_log() {
    local level="$1" tag="$2" color="$3"
    shift 3
    local msg="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local entry="[$timestamp][$level][$tag] $msg"

    mkdir -p "$LOG_DIR"
    echo "$entry" >> "${LOG_DIR}/${tag}.log"
    echo "$entry" >> "${LOG_DIR}/combined.log"

    case "$level" in
        DEBUG)
            [[ "$LOG_LEVEL" == "DEBUG" ]] || return 0
            echo -e "${color}${entry}${NC}"
            ;;
        *)
            echo -e "${color}${entry}${NC}"
            ;;
    esac
}

log_info()  { _log "INFO " "$1" "${GREEN}"  "${@:2}"; }
log_warn()  { _log "WARN " "$1" "${YELLOW}" "${@:2}"; }
log_error() { _log "ERROR" "$1" "${RED}"    "${@:2}" >&2; }
log_debug() { _log "DEBUG" "$1" "${CYAN}"   "${@:2}"; }

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
        # Strip inline comments and leading/trailing whitespace
        local line="${raw_line%%#*}"
        # trim leading spaces/tabs
        while [[ "$line" == [[:space:]]* ]]; do line="${line#?}"; done
        # trim trailing spaces/tabs
        while [[ "$line" == *[[:space:]] ]]; do line="${line%?}"; done
        [[ -z "$line" ]] && continue

        # ── Section header ────────────────────────────────────────────────
        if [[ "$line" == \[*\] ]]; then
            current_section="${line#[}"
            current_section="${current_section%]}"
            # trim spaces inside brackets
            while [[ "$current_section" == [[:space:]]* ]]; do
                current_section="${current_section#?}"
            done
            while [[ "$current_section" == *[[:space:]] ]]; do
                current_section="${current_section%?}"
            done

            if [[ "$current_section" != "global" ]]; then
                # Append to server list if not already present
                local already=0
                for s in $SERVER_NAMES; do
                    [[ "$s" == "$current_section" ]] && already=1 && break
                done
                if [[ $already -eq 0 ]]; then
                    SERVER_NAMES="${SERVER_NAMES}${SERVER_NAMES:+ }${current_section}"
                fi
            fi
            continue
        fi

        # ── Key = Value ───────────────────────────────────────────────────
        if [[ "$line" == *=* ]]; then
            local key="${line%%=*}"
            local value="${line#*=}"

            # trim key
            while [[ "$key" == [[:space:]]* ]];   do key="${key#?}";     done
            while [[ "$key" == *[[:space:]] ]];   do key="${key%?}";     done
            # trim value
            while [[ "$value" == [[:space:]]* ]]; do value="${value#?}"; done
            while [[ "$value" == *[[:space:]] ]]; do value="${value%?}"; done

            [[ -z "$current_section" ]] && continue

            if [[ "$current_section" == "global" ]]; then
                case "$key" in
                    retry_interval)   RETRY_INTERVAL="$value"         ;;
                    server_alive)     SERVER_ALIVE_INTERVAL="$value"  ;;
                    server_alive_max) SERVER_ALIVE_COUNT_MAX="$value" ;;
                    max_retries)      MAX_RETRIES="$value"            ;;
                    log_dir)          LOG_DIR="$value"
                                      PID_DIR="${value}/pids"         ;;
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
    for dep in ssh sshpass; do
        command -v "$dep" &>/dev/null || missing="$missing $dep"
    done

    if [[ -n "$missing" ]]; then
        echo -e "${RED}Missing dependencies:${missing}${NC}"
        echo -e "${YELLOW}  Debian/Ubuntu : sudo apt install sshpass"
        echo -e "  macOS         : brew install hudochenkov/sshpass/sshpass${NC}"
        exit 1
    fi
}

# =============================================================================
# PASSWORD RESOLUTION
# =============================================================================

resolve_password() {
    local server="$1"
    local password password_cmd

    password=$(get_cfg "$server" "password" "")
    password_cmd=$(get_cfg "$server" "password_cmd" "")

    if [[ -n "$password_cmd" ]]; then
        password=$(eval "$password_cmd" 2>/dev/null) || {
            log_error "$server" "password_cmd failed: $password_cmd"
            return 1
        }
    fi

    if [[ -z "$password" ]]; then
        log_error "$server" "No 'password' or 'password_cmd' set in config"
        return 1
    fi

    printf '%s' "$password"
}

# =============================================================================
# TUNNEL WORKER  (one subprocess per server)
# =============================================================================

run_tunnel_worker() {
    local server="$1"

    local ssh_alias remote_port local_port local_host
    local retry_interval server_alive server_alive_max max_retries

    ssh_alias=$(get_cfg      "$server" "ssh_alias"       "$server")
    remote_port=$(get_cfg    "$server" "remote_port"     "2222")
    local_port=$(get_cfg     "$server" "local_port"      "22")
    local_host=$(get_cfg     "$server" "local_host"      "localhost")
    retry_interval=$(get_cfg "$server" "retry_interval"  "$RETRY_INTERVAL")
    server_alive=$(get_cfg   "$server" "server_alive"    "$SERVER_ALIVE_INTERVAL")
    server_alive_max=$(get_cfg "$server" "server_alive_max" "$SERVER_ALIVE_COUNT_MAX")
    max_retries=$(get_cfg    "$server" "max_retries"     "$MAX_RETRIES")

    local retry_count=0
    local tunnel_pid=""

    log_info  "$server" "Worker started → ${ssh_alias}:${remote_port} ← ${local_host}:${local_port}"

    # ── kill current SSH child ────────────────────────────────────────────────
    _kill_tunnel() {
        if [[ -n "$tunnel_pid" ]] && kill -0 "$tunnel_pid" 2>/dev/null; then
            kill "$tunnel_pid" 2>/dev/null || true
            wait "$tunnel_pid" 2>/dev/null || true
            log_debug "$server" "Killed SSH PID $tunnel_pid"
        fi
        tunnel_pid=""
        rm -f "${PID_DIR}/${server}.pid"
    }

    # ── start one SSH tunnel ──────────────────────────────────────────────────
    _start_tunnel() {
        local password
        password=$(resolve_password "$server") || return 1

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
            2>> "${LOG_DIR}/${server}.log" &

        tunnel_pid=$!
        mkdir -p "$PID_DIR"
        echo "$tunnel_pid" > "${PID_DIR}/${server}.pid"
        log_debug "$server" "SSH PID: $tunnel_pid"
    }

    # ── is tunnel process running? ────────────────────────────────────────────
    _is_alive() {
        [[ -n "$tunnel_pid" ]] && kill -0 "$tunnel_pid" 2>/dev/null
    }

    # ── cleanup on signal ─────────────────────────────────────────────────────
    trap '_kill_tunnel; log_info "$server" "Worker stopped."; exit 0' SIGTERM SIGINT SIGHUP

    # ── retry loop ────────────────────────────────────────────────────────────
    while true; do

        if ! _is_alive; then
            # Check max retries
            if [[ "$max_retries" -gt 0 ]] && [[ "$retry_count" -ge "$max_retries" ]]; then
                log_error "$server" "Reached max retries ($max_retries). Worker stopping."
                exit 1
            fi

            # Back-off wait (skip on first attempt)
            if [[ "$retry_count" -gt 0 ]]; then
                log_warn "$server" "Tunnel down. Retry #${retry_count} in ${retry_interval}s..."
                sleep "$retry_interval"
            fi

            _kill_tunnel

            if ! _start_tunnel; then
                retry_count=$(( retry_count + 1 ))
                log_warn "$server" "Failed to launch SSH (attempt $retry_count)"
                sleep "$retry_interval"
                continue
            fi

            # Short grace period to catch instant failures
            sleep 3

            if _is_alive; then
                log_info "$server" "✓ Tunnel UP  (PID: $tunnel_pid)"
                retry_count=0
            else
                retry_count=$(( retry_count + 1 ))
                log_warn "$server" "Tunnel exited immediately (attempt $retry_count)"
                continue
            fi
        fi

        log_debug "$server" "Heartbeat OK (PID: $tunnel_pid)"
        sleep "$retry_interval"
    done
}

# =============================================================================
# MANAGER
# =============================================================================

# Worker PIDs stored as flat vars: _WPID__<server>=<pid>
set_wpid()  { eval "_WPID__${1//-/_}=\"$2\""; }
get_wpid()  { eval "echo \"\${_WPID__${1//-/_}:-}\"";}

spawn_worker() {
    local server="$1"
    run_tunnel_worker "$server" &
    local wpid=$!
    set_wpid "$server" "$wpid"
    log_info "manager" "Spawned worker '$server' (PID: $wpid)"
}

stop_all_workers() {
    log_info "manager" "Stopping all workers..."
    for server in $SERVER_NAMES; do
        local pid
        pid=$(get_wpid "$server")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill -SIGTERM "$pid" 2>/dev/null || true
            log_info "manager" "SIGTERM → worker '$server' (PID: $pid)"
        fi
    done
    for server in $SERVER_NAMES; do
        local pid
        pid=$(get_wpid "$server")
        [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
    done
    log_info "manager" "All workers stopped."
}

monitor_workers() {
    log_info "manager" "Monitoring ${SERVER_NAMES} ..."
    while true; do
        for server in $SERVER_NAMES; do
            local pid
            pid=$(get_wpid "$server")
            if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
                log_warn "manager" "Worker '$server' (PID: $pid) gone — restarting..."
                spawn_worker "$server"
            fi
        done
        sleep 5
    done
}

# =============================================================================
# STATUS
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

# =============================================================================
# MAIN
# =============================================================================

usage() {
    cat <<EOF
${BOLD}Usage:${NC}
  $0 <config_file>           Start tunnel manager
  $0 status <config_file>    Show tunnel status

${BOLD}Example:${NC}
  $0 tunnels.conf
  LOG_LEVEL=DEBUG $0 tunnels.conf
EOF
}

main() {
    local config_file=""
    local cmd="start"

    for arg in "$@"; do
        case "$arg" in
            -h|--help) usage; exit 0 ;;
            status)    cmd="status"  ;;
            *)         config_file="$arg" ;;
        esac
    done

    config_file="${config_file:-$(dirname "$0")/tunnels.conf}"

    parse_config "$config_file"

    if [[ "$cmd" == "status" ]]; then
        show_status
        exit 0
    fi

    check_deps
    mkdir -p "$LOG_DIR" "$PID_DIR"

    if [[ -z "$SERVER_NAMES" ]]; then
        echo -e "${RED}No server sections found in: $config_file${NC}"
        exit 1
    fi

    mkdir -p "$PID_DIR"
    echo $$ > "${PID_DIR}/manager.pid"
    trap 'stop_all_workers; rm -f "${PID_DIR}/manager.pid"; exit 0' SIGINT SIGTERM SIGHUP

    echo -e "${BOLD}${GREEN}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║    SSH Reverse Tunnel Manager            ║"
    echo "  ╠══════════════════════════════════════════╣"
    printf  "  ║  Servers : %-30s║\n" "$SERVER_NAMES"
    printf  "  ║  Log dir : %-30s║\n" "$LOG_DIR"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"

    # Spawn all workers in parallel
    for server in $SERVER_NAMES; do
        spawn_worker "$server"
    done

    monitor_workers
}

main "$@"