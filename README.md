# hypebot

Telegram bot that runs TikTok hype-edit batches hands-free. On batch days
(Mon + Thu, 09:00) it asks what the batch should be about; the reply — or
`/batch <brief>` anytime — launches a headless agent that executes the
[hype-edit skill](https://github.com/guitaripod/claudeconfig) end to end.
Back come two album messages (5 portrait TikTok videos + their 5 landscape
companions), copy-paste caption messages, and full-res files in
`~/Videos/hype/<date>/`.

Deployed as the `hypebot` systemd user service, talking to **@hype_edit_bot**;
only the configured chat id is honored.

## Flow

0. Preflight: prune workdirs beyond the newest 4 (the latest batch is always
   kept for `/redo`/`/last`), refuse if < 60 GB free, warn if a yt-dlp probe
   fails (stale cookies → sourcing will 429).
1. Brief in → `claude -p` (Fable 5, `--effort high` — the knee of the
   quality/quota curve; `HYPEBOT_EFFORT` to change) works under
   `/mnt/games-nvme-gen4/hypebot/<date>/`, one workdir per edit, full SKILL.md
   quality loop, zero clip overlap. If the run dies on a genuine quota error
   (never on a pipeline failure or `/cancel`), it reruns on grok-4.5 via
   `opencode --variant high`.
2. Agent writes `deliver/manifest.json` last — that's the success signal. The
   bot validates every entry (portrait 1080×1920 + landscape 1920×1080
   companion, ~15 s, audio, caption).
   Edits use the skill's **remaster** style: full-bleed 90°-rotated landscape
   at 60 fps, 4K-remaster grade, motion-interpolated slow-mo.
3. Full-res → `~/Videos/hype/<date>/`. Files ≥ 49 MB get a ~46 MB preview
   encode for Telegram (bot upload cap is 50 MB); disk copies stay untouched.
4. Progress streams from the agent's `progress.log` into a single edited
   Telegram message.

## Commands

- `/batch <brief>` — start a run (one at a time)
- `/status` · `/cancel`
- `/redo <n> <feedback>` — surgical re-run of one edit on its checkpointed workdir
- `/last` — re-send the album
- `/start_posting` / `/stop_posting` — "post #N now" pings every 3 h, caption included
- `/skip` — dismiss a batch-day prompt

## Failure behavior

Service restarts kill the engine (shared cgroup): the bot records the active
run, announces the interruption on boot, and — if rendering had finished —
delivers the batch anyway. Telegram 5xx/truncated responses retry with
backoff; `last_batch` is saved before any send, so `/last` always works;
failed reminders re-queue (+2 min). Failed runs leave a checkpointed workdir
you can resume in a live session.

## Config

`~/.config/hypebot/secrets.env` → `HYPEBOT_TOKEN`, `HYPEBOT_CHAT_ID`. Every
knob (prompt days/hour, batch size, edit length, timeout, cadence, engines,
paths) is an env var — see the docstring in `hypebot.py`.

## Selftest

`selftest/run.sh` — mock Bot API + fake engine (real tiny mp4s), asserts the
whole loop with no token, network, or model. Run it after any change.
