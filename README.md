# hypebot

Telegram-fronted automation for TikTok hype-edit batches. A long-running
systemd user service long-polls a dedicated Telegram bot; twice a week (Mon +
Thu by default, `HYPEBOT_PROMPT_DAYS`) it asks
what the batch should be about, then a headless coding agent executes the
[hype-edit skill](https://github.com/guitaripod/claudeconfig) end to end and the
bot delivers the finished edits back — one album message with per-video TikTok
captions, copy-paste caption messages, full-res files in `~/Videos/hype/<date>/`.

## Engines

Runs are executed by the first available engine, in order:

1. **Fable 5** — `claude -p --model claude-fable-5 --effort max`
2. **Grok 4.5** — `opencode run -m xai/grok-4.5 --variant max --auto` (automatic
   fallback when the Claude run dies on a usage/quota limit)

Both receive the same prompt, which points at the skill's `SKILL.md` by absolute
path, so the pipeline is engine-agnostic. Fallback triggers only on genuine
quota wording in the engine log (never on a pipeline failure that wrote
`FAILED.md`), and a cancelled run never falls back.

## Resilience

- `active_run` is persisted in state; if the service restarts mid-run (systemd
  kills the whole cgroup, engine included) the bot says so on boot, points at
  the checkpointed workdir — and if rendering had already finished, delivers
  the batch instead.
- Transient Telegram 5xx/truncated responses are retried with backoff; `last_batch`
  is saved before any delivery send, so `/last` can always retry the album.
- Posting reminders that fail to send are re-queued (+2 min), not dropped.

## Telegram UX

- **09:00 on batch days (Mon/Thu)**: "What should this one be about?" — reply
  free text, or `/skip`.
- `/batch <brief>` — start a batch anytime.
- Progress pings during the run (single edited message, no spam), sourced from
  the agent's `progress.log`.
- On success: summary → **one album** (5 videos, captions attached) → 5
  copy-friendly caption messages in posting order.
- `/start_posting` — reminder ping every 3 h ("post #N now" + the caption);
  `/stop_posting` cancels.
- `/status`, `/cancel`, `/redo <n> <feedback>` (surgical re-run of one edit on
  its checkpointed workdir), `/last` (re-send album), `/help`.

Only the configured chat id is honored; other senders are ignored.

## Install

```sh
./install.sh                # secrets template + systemd unit
# create a bot with @BotFather, paste token into ~/.config/hypebot/secrets.env
systemctl --user enable --now hypebot
```

## Config

Environment (see `hypebot.py` docstring for the full list): `HYPEBOT_TOKEN`,
`HYPEBOT_CHAT_ID` required; work root defaults to `/mnt/games-nvme-gen4/hypebot`,
delivery to `~/Videos/hype`, 5×30 s edits, 6 h run timeout, 3 h cadence.

Videos over Telegram's 50 MB bot cap are re-encoded to a ~46 MB preview for the
album; the full-res file always lands on disk untouched.

## Selftest

```sh
selftest/run.sh
```

Spins a mock Bot API server and a fake engine (tiny real mp4s via ffmpeg), runs
the daemon against them, and asserts the whole loop: command registration →
`/batch` → progress → validation → album with captions → disk delivery. No
token, no network, no real model.
