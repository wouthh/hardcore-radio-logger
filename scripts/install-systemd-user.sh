#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${HCR_CONFIG_FILE:-%h/.config/hcr-sync/hcr-sync.env}"
python_bin="${PYTHON_BIN:-$(command -v python3)}"
systemd_user_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$systemd_user_dir"

sed \
  -e "s#{{PROJECT_DIR}}#$project_dir#g" \
  -e "s#{{ENV_FILE}}#$env_file#g" \
  -e "s#{{PYTHON_BIN}}#$python_bin#g" \
  "$project_dir/systemd/user/hcr-sync.service.in" > "$systemd_user_dir/hcr-sync.service"

cp "$project_dir/systemd/user/hcr-sync.timer" "$systemd_user_dir/hcr-sync.timer"

echo "Installed:"
echo "  $systemd_user_dir/hcr-sync.service"
echo "  $systemd_user_dir/hcr-sync.timer"
echo
echo "Next:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now hcr-sync.timer"
