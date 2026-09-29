#!/bin/sh
#
# Install AVer as user-level systemd services (no root / no system-wide units).
#
# Generates ~/.config/systemd/user/aver-backend.service and aver-worker.service
# pointing at THIS WebAPI directory and its ./venv, then enables and starts them.
#
# Requirements on the target machine:
#   * a per-user systemd instance (the default on modern distros; check with
#     `systemctl --user is-system-running`),
#   * the venv already created here (run ./setup_api.sh first),
#   * for the worker to use the GPU, a CUDA-enabled torch in ./venv.
#
# To survive logout/reboot the services need "lingering". This script tries to
# enable it; if that needs an administrator, ask them to run once:
#   loginctl enable-linger <your-user>
#
# All application config (port, API key, DB path, GPU knobs, ...) lives in
# WebAPI/.env. Edit that file before or after running this script; the two
# wrapper scripts source it on every start, so a plain
#   systemctl --user restart aver-backend aver-worker
# picks up changes without re-running this installer.
#
set -e

# Absolute path to this WebAPI directory (where start_api.sh / start_worker.sh live).
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if ! systemctl --user is-system-running >/dev/null 2>&1; then
	echo "WARNING: 'systemctl --user' is not fully running; services may not start"
	echo "         until you have an active user systemd instance."
fi

mkdir -p "$UNIT_DIR"
echo "Installing units into $UNIT_DIR (WorkingDirectory=$WORKDIR)"

# Generate minimal unit files. No Environment= lines: the two wrappers source
# WebAPI/.env at every start, so all application config (port, API key,
# DB path, GPU knobs, language auto-correct, ...) lives there. Edit .env,
# then `systemctl --user restart aver-backend aver-worker`.

cat > "$UNIT_DIR/aver-backend.service" <<EOF
[Unit]
Description=AVer Backend API (web tier, user-level)
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORKDIR
ExecStart=$WORKDIR/start_api.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/aver-worker.service" <<EOF
[Unit]
Description=AVer Inference Worker (user-level)
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORKDIR
ExecStart=$WORKDIR/start_worker.sh
Restart=always
RestartSec=5
# Give the worker unlimited time to drain its current job on SIGTERM.
# See aver-worker.service (root unit) for the rationale.
TimeoutStopSec=infinity

[Install]
WantedBy=default.target
EOF

# Keep the services running after logout / across reboots.
if loginctl enable-linger "$USER" 2>/dev/null; then
	echo "Lingering enabled for $USER (services survive logout/reboot)."
else
	echo "NOTE: could not enable lingering automatically. Without it, the services"
	echo "      stop when you log out. Ask an admin to run: loginctl enable-linger $USER"
fi

systemctl --user daemon-reload
systemctl --user enable --now aver-backend.service aver-worker.service

echo
echo "Done. Manage with:"
echo "  systemctl --user status aver-backend aver-worker"
echo "  systemctl --user restart aver-worker"
echo "  journalctl --user -u aver-worker -f"
