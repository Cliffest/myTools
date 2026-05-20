```bash
# ── Install sshpass ───────────────────────────────────────────────────────────
sudo apt install sshpass            # Debian/Ubuntu
brew install hudochenkov/sshpass/sshpass  # macOS

# ── Run ──────────────────────────────────────────────────────────────────────
chmod +x ssh-R.sh
./ssh-R.sh tunnels.conf

# Run in background
nohup ./ssh-R.sh tunnels.conf &

# ── Status ───────────────────────────────────────────────────────────────────
./ssh-R.sh status tunnels.conf

# ── Logs ─────────────────────────────────────────────────────────────────────
tail -f /tmp/ssh_tunnels/combined.log       # all servers
tail -f /tmp/ssh_tunnels/work_server.log    # one server
tail -f /tmp/ssh_tunnels/home_nas.log

# ── Stop ─────────────────────────────────────────────────────────────────────
kill $(cat /tmp/ssh_tunnels/pids/manager.pid)
```


Config file: tunnels.conf
```Ini
# ── Global defaults (applied to all servers unless overridden) ────────────────
[global]
retry_interval    = 10
server_alive      = 30
server_alive_max  = 3
max_retries       = 0
log_dir           = /tmp/ssh_tunnels
log_level         = INFO


# ── Server definitions ────────────────────────────────────────────────────────
# Each section name is an arbitrary label for this tunnel.
# ssh_alias must match a Host entry in ~/.ssh/config

[work_server]
ssh_alias         = work                   # matches Host work in ~/.ssh/config
remote_port       = 2222
local_port        = 22
local_host        = localhost
password          = mypassword123          # plaintext (use password_cmd instead!)

[home_nas]
ssh_alias         = nas
remote_port       = 2223
local_port        = 22
local_host        = localhost
password_cmd      = cat /run/secrets/nas_password   # read from file
retry_interval    = 5                      # override global for this server

[lab_box]
ssh_alias         = lab
remote_port       = 8080
local_port        = 8080
local_host        = 127.0.0.1
password_cmd      = pass show lab/tunnel   # use 'pass' password manager
server_alive      = 20
max_retries       = 5                      # give up after 5 attempts
```