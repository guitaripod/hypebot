#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

mkdir -p ~/.config/hypebot ~/.local/state/hypebot ~/Videos/hype
if [ ! -f ~/.config/hypebot/secrets.env ]; then
  cat > ~/.config/hypebot/secrets.env <<'EOF'
HYPEBOT_TOKEN=
HYPEBOT_CHAT_ID=264546885
EOF
  chmod 600 ~/.config/hypebot/secrets.env
  echo "created ~/.config/hypebot/secrets.env — paste the @BotFather token into HYPEBOT_TOKEN"
fi

mkdir -p ~/.config/systemd/user
ln -sf "$here/hypebot.service" ~/.config/systemd/user/hypebot.service
systemctl --user daemon-reload
echo "installed. next: fill HYPEBOT_TOKEN, then: systemctl --user enable --now hypebot"
