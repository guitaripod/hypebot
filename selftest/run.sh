#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
tmp="$(mktemp -d /tmp/hypebot-selftest.XXXXXX)"
trap 'kill $(jobs -p) 2>/dev/null || true; rm -rf "$tmp"' EXIT

port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')

export MOCK_LOG="$tmp/mock.jsonl"
export MOCK_BRIEF="selftest brief"
export HOME="$tmp/home"
export HYPEBOT_TOKEN="TESTTOKEN"
export HYPEBOT_CHAT_ID="111111111"
export HYPEBOT_API_BASE="http://127.0.0.1:$port"
export HYPEBOT_CLAUDE="$here/fake-engine"
export HYPEBOT_WORK_ROOT="$tmp/work"
export HYPEBOT_VIDEOS_DIR="$tmp/videos"
export HYPEBOT_BATCH_SIZE="2"
export HYPEBOT_EDIT_SECONDS="2"
mkdir -p "$HOME"

python3 "$here/mock_api.py" "$port" &
sleep 0.5
python3 "$repo/hypebot.py" > "$tmp/hypebot.out" 2>&1 &
bot_pid=$!

deadline=$((SECONDS + 120))
until grep -q sendMediaGroup "$MOCK_LOG" 2>/dev/null; do
  if ! kill -0 "$bot_pid" 2>/dev/null; then
    echo "FAIL: hypebot exited early"; cat "$tmp/hypebot.out"; exit 1
  fi
  if (( SECONDS > deadline )); then
    echo "FAIL: no sendMediaGroup within 120s"; cat "$tmp/hypebot.out"; cat "$MOCK_LOG" 2>/dev/null; exit 1
  fi
  sleep 1
done
sleep 5

python3 - "$MOCK_LOG" "$HYPEBOT_VIDEOS_DIR" <<'EOF'
import json, sys, glob, os
calls = [json.loads(l) for l in open(sys.argv[1])]
methods = [c["method"] for c in calls]
assert "setMyCommands" in methods, methods
msgs = [c["payload"] for c in calls if c["method"] == "sendMessage"]
sends = [p.get("text", "") for p in msgs]
assert any("Batch started" in t for t in sends), sends
done = next(p for p in msgs if "Batch done" in p.get("text", ""))
keys = [b["callback_data"] for row in done["reply_markup"]["inline_keyboard"] for b in row]
assert keys == ["start_posting"], keys
assert any("caption 1" in t for t in sends), sends
assert any("caption 2" in t for t in sends), sends
mg = next(c for c in calls if c["method"] == "sendMediaGroup")
media = json.loads(mg["payload"]["media"])
assert len(media) == 2, media
assert all(m["type"] == "video" and m["caption"] for m in media), media
files = {f["field"] for f in mg["payload"].get("_files", [])}
assert files == {"v0", "v1"}, files
vids = glob.glob(os.path.join(sys.argv[2], "*", "*.mp4"))
assert len(vids) == 2, vids
manifests = glob.glob(os.path.join(sys.argv[2], "*", "manifest.json"))
assert len(manifests) == 1 and "selftest brief" in open(manifests[0]).read(), manifests
print("assertions ok")
EOF

echo "PASS"
