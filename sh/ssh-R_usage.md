```bash
# ── Install sshpass ───────────────────────────────────────────────────────────
sudo apt install sshpass            # Debian/Ubuntu
brew install hudochenkov/sshpass/sshpass  # macOS

# ── Run ──────────────────────────────────────────────────────────────────────
chmod +x ssh_tunnel.sh
./ssh_tunnel.sh tunnels.conf

# Run in background
nohup ./ssh_tunnel.sh tunnels.conf &

# ── Status ───────────────────────────────────────────────────────────────────
./ssh_tunnel.sh status tunnels.conf

# ── Logs ─────────────────────────────────────────────────────────────────────
tail -f /tmp/ssh_tunnels/combined.log       # all servers
tail -f /tmp/ssh_tunnels/work_server.log    # one server
tail -f /tmp/ssh_tunnels/home_nas.log

# ── Stop ─────────────────────────────────────────────────────────────────────
kill $(cat /tmp/ssh_tunnels/pids/manager.pid)
```