#!/usr/bin/env python3
"""hypebot — Telegram-fronted daily hype-edit batch automation.

Long-polls a dedicated Telegram bot. Every morning it asks what today's batch
should be about; the reply (or /batch <brief> anytime) launches a headless
Claude Code run that executes the hype-edit skill in batch mode. On success the
finished edits land as ONE Telegram album (per-video TikTok captions), the
captions repeat as copy-friendly single messages, and full-res files are copied
to ~/Videos/hype/<date>/ for desktop posting. /start-posting then paces the
3-hour posting cadence with reminder pings.

Config via environment (see ~/.config/hypebot/secrets.env):
  HYPEBOT_TOKEN, HYPEBOT_CHAT_ID          required
  HYPEBOT_API_BASE                        default https://api.telegram.org
  HYPEBOT_CLAUDE                          default "claude"
  HYPEBOT_WORK_ROOT                       default /mnt/games-nvme-gen4/hypebot
  HYPEBOT_VIDEOS_DIR                      default ~/Videos/hype
  HYPEBOT_PROMPT_HOUR                     default 9
  HYPEBOT_BATCH_SIZE                      default 5
  HYPEBOT_EDIT_SECONDS                    default 30
  HYPEBOT_RUN_TIMEOUT_HOURS               default 6
  HYPEBOT_CADENCE_HOURS                   default 3
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

TOKEN = os.environ.get("HYPEBOT_TOKEN", "")
CHAT_ID = os.environ.get("HYPEBOT_CHAT_ID", "")
API_BASE = os.environ.get("HYPEBOT_API_BASE", "https://api.telegram.org")
CLAUDE_BIN = os.environ.get("HYPEBOT_CLAUDE", "claude")
CLAUDE_MODEL = os.environ.get("HYPEBOT_CLAUDE_MODEL", "claude-fable-5")
OPENCODE_BIN = os.environ.get("HYPEBOT_OPENCODE", "opencode")
OPENCODE_MODEL = os.environ.get("HYPEBOT_OPENCODE_MODEL", "xai/grok-4.5")
SKILL_MD = os.environ.get(
    "HYPEBOT_SKILL_MD", str(Path.home() / ".claude/skills/hype-edit/SKILL.md"))
WORK_ROOT = Path(os.environ.get("HYPEBOT_WORK_ROOT", "/mnt/games-nvme-gen4/hypebot"))
VIDEOS_DIR = Path(os.environ.get("HYPEBOT_VIDEOS_DIR", str(Path.home() / "Videos/hype")))
PROMPT_HOUR = int(os.environ.get("HYPEBOT_PROMPT_HOUR", "9"))
BATCH_SIZE = int(os.environ.get("HYPEBOT_BATCH_SIZE", "5"))
EDIT_SECONDS = int(os.environ.get("HYPEBOT_EDIT_SECONDS", "30"))
RUN_TIMEOUT_S = float(os.environ.get("HYPEBOT_RUN_TIMEOUT_HOURS", "6")) * 3600
CADENCE_S = float(os.environ.get("HYPEBOT_CADENCE_HOURS", "3")) * 3600
TG_SIZE_CAP = 49 * 1024 * 1024
PREVIEW_TARGET = 46 * 1024 * 1024
STATE_FILE = Path.home() / ".local/state/hypebot/state.json"
LOG_DIR = Path.home() / ".local/state/hypebot"

COMMANDS = [
    ("batch", "start a batch: /batch <brief>"),
    ("skip", "skip today's batch"),
    ("status", "show run phase + elapsed"),
    ("cancel", "kill the active run"),
    ("redo", "re-run one edit: /redo <n> <feedback>"),
    ("last", "re-send the last batch album"),
    ("start_posting", "begin 3h posting reminders"),
    ("stop_posting", "cancel posting reminders"),
    ("help", "list commands"),
]


def _stamp(level, msg):
    return f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"


def log(msg, level="INFO"):
    line = _stamp(level, msg)
    print(line, file=sys.stderr if level in ("ERROR", "WARN") else sys.stdout, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / f"run-{datetime.now().strftime('%Y-%m-%d')}.log").open("a") as f:
        f.write(line + "\n")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log(f"state.json unreadable, starting fresh: {e}", "WARN")
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def _multipart(fields, files):
    boundary = f"----hypebot{uuid.uuid4().hex}"
    parts = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        )
    for name, (filename, data) in files.items():
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
             f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
            + data + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def api(method, payload=None, files=None, timeout=90):
    """POST to the Bot API with 429/network retries; returns the `result` object.

    Raises RuntimeError with Telegram's description on a non-ok response so
    callers can decide whether to fall back (e.g. album → individual sends).
    """
    url = f"{API_BASE}/bot{TOKEN}/{method}"
    last_err = None
    for attempt in range(4):
        try:
            if files:
                body, ctype = _multipart(payload or {}, files)
                req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
            else:
                req = urllib.request.Request(
                    url, data=json.dumps(payload or {}).encode(),
                    headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
            except Exception:
                data = {"ok": False, "description": str(e)}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            log(f"telegram {method} network error (attempt {attempt + 1}): {e}", "WARN")
            time.sleep(2 ** attempt)
            continue
        if data.get("ok"):
            return data.get("result")
        retry_after = (data.get("parameters") or {}).get("retry_after")
        if retry_after:
            log(f"telegram 429 on {method}, waiting {retry_after}s", "WARN")
            time.sleep(retry_after + 1)
            continue
        raise RuntimeError(f"telegram {method} failed: {data.get('description', 'unknown')}")
    raise RuntimeError(f"telegram {method} failed after retries: {last_err}")


def send(text, silent=False):
    return api("sendMessage", {"chat_id": CHAT_ID, "text": text,
                               "disable_notification": silent})


def edit_message(message_id, text):
    try:
        api("editMessageText", {"chat_id": CHAT_ID, "message_id": message_id, "text": text})
    except RuntimeError as e:
        if "message is not modified" not in str(e):
            log(f"editMessageText: {e}", "WARN")


def ffprobe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height",
         "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    info = json.loads(r.stdout)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"dur": float(info.get("format", {}).get("duration", 0)),
            "w": v.get("width", 0), "h": v.get("height", 0),
            "audio": any(s.get("codec_type") == "audio" for s in streams)}


def make_preview(src, dst):
    """Re-encode a >cap video to fit Telegram's 50MB bot upload limit.

    Targets PREVIEW_TARGET bytes via NVENC VBR (libx264 fallback), stepping the
    bitrate down 15% per attempt until the output fits.
    """
    meta = ffprobe(src)
    if not meta:
        raise RuntimeError(f"ffprobe failed for {src.name}")
    dur = max(meta["dur"], 1.0)
    kbps = int((PREVIEW_TARGET * 8 / dur - 192_000) / 1000)
    for _ in range(4):
        for codec in (["h264_nvenc", "-rc", "vbr", "-preset", "p5"], ["libx264", "-preset", "medium"]):
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                 "-c:v", codec[0], *codec[1:], "-b:v", f"{kbps}k",
                 "-maxrate", f"{int(kbps * 1.1)}k", "-bufsize", f"{kbps * 2}k",
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)],
                capture_output=True, text=True)
            if r.returncode == 0:
                break
        else:
            raise RuntimeError(f"preview encode failed for {src.name}")
        if dst.stat().st_size < TG_SIZE_CAP:
            return
        kbps = int(kbps * 0.85)
    raise RuntimeError(f"preview for {src.name} won't fit under 50MB")


def engines(prompt):
    return [
        (f"{CLAUDE_MODEL} (claude code, max effort)",
         [CLAUDE_BIN, "-p", prompt, "--model", CLAUDE_MODEL, "--effort", "max",
          "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]),
        (f"{OPENCODE_MODEL} (opencode, max variant)",
         [OPENCODE_BIN, "run", prompt, "-m", OPENCODE_MODEL, "--variant", "max",
          "--format", "json", "--auto", "--print-logs"]),
    ]


QUOTA_RE = re.compile(
    r"usage limit|session limit|rate.?limit|out of (usage|quota)|quota exceeded"
    r"|limit reached|limit will reset|resets \d|overloaded", re.I)


def batch_prompt(brief, date_dir, n, seconds):
    return f"""You are hypebot's unattended worker. Produce a TikTok hype-edit batch end to end by reading {SKILL_MD} fully (plus its reference/ docs) and following it exactly, including BATCH MODE. No user is available — make every decision yourself and see it through to completion.

Marcus's brief for today: "{brief}"

Parameters:
- {n} edits, each {seconds}s, vertical 1080x1920@30, song pitch-shifted +3% (extract_audio.py --pitch 1.03).
- Work under {date_dir}/ — one workdir per edit (edit1..edit{n}). Share the source pool per SKILL.md batch mode where subjects overlap; enforce ZERO clip overlap across edits by cascading exclude_clips.
- Song selection: current edit-culture/phonk-leaning picks appropriate to the brief; web-search to confirm relevance/trendiness today. Read {VIDEOS_DIR}/*/manifest.json (if any exist) and avoid repeating recent song or player+song combos.
- Quality bar is the full SKILL.md loop, no shortcuts: seg-grid review rounds until clean, opener retention gate, segment-level hero verification, render-exact probing, and qc.py printing "ALL GATES PASS" for every edit.
- Captions: engagement-optimized TikTok captions with hashtags per the established style.

Progress protocol: append one line to {date_dir}/progress.log at every milestone, format "phase | detail" (e.g. "sourcing | edit2: 14 sources fetched", "review | edit4: round 2 clean"). The Telegram bot relays these to Marcus.

Delivery contract (STRICT):
1. Only after every edit passes qc, create {date_dir}/deliver/ containing the final mp4s named 01_<player>_<song>.mp4 .. {n:02d}_<player>_<song>.mp4 in recommended posting order.
2. Write {date_dir}/deliver/manifest.json LAST — its existence signals success. JSON array, posting order, entries: {{"file": "<name in deliver/>", "player": "...", "song": "...", "caption": "..."}}.
3. If something fails irrecoverably, write {date_dir}/FAILED.md (what failed, where to resume) and exit nonzero. Never write a partial manifest."""


def redo_prompt(date_dir, entry, idx, feedback):
    return f"""You are hypebot's unattended worker. One edit from an existing hype-edit batch needs a redo based on Marcus's feedback. The batch lives in {date_dir}/ (checkpointed workdirs edit1..editN, deliverables in deliver/, manifest.json).

Edit #{idx + 1}: {entry['player']} × {entry['song']} → deliver/{entry['file']}

Marcus's feedback: "{feedback}"

Apply the feedback using the hype-edit skill's iteration loop (seg-grid review, exclude_clips, hero_overrides — read {SKILL_MD} fully). Re-render, re-run qc until "ALL GATES PASS", then overwrite deliver/{entry['file']} and update this entry's caption in deliver/manifest.json if the feedback warrants it. Append progress lines to {date_dir}/progress.log. Touch nothing belonging to the other edits. On success write {date_dir}/REDO_OK, on irrecoverable failure write {date_dir}/FAILED.md and exit nonzero."""


class Runner:
    """Owns the single active claude -p subprocess and its delivery."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.thread = None
        self.kind = ""
        self.date_dir = Path(".")
        self.started = 0.0
        self.cancelled = False

    @property
    def active(self):
        with self.lock:
            return self.thread is not None and self.thread.is_alive()

    def status_line(self):
        with self.lock:
            if not (self.thread and self.thread.is_alive()):
                return "idle"
            mins = int((time.time() - self.started) / 60)
            last = _tail(self.date_dir / "progress.log", 1) or "starting"
            return f"{self.kind} running {mins // 60}h{mins % 60:02d}m — {last}"

    def start(self, kind, date_dir, prompt, on_done):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.kind, self.date_dir, self.started, self.cancelled = kind, date_dir, time.time(), False
            self.thread = threading.Thread(
                target=self._run, args=(date_dir, prompt, on_done), daemon=True)
            self.thread.start()
            return True

    def cancel(self):
        with self.lock:
            self.cancelled = True
            if self.proc and self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGTERM)

    def _run(self, date_dir, prompt, on_done):
        date_dir.mkdir(parents=True, exist_ok=True)
        (date_dir / "FAILED.md").unlink(missing_ok=True)
        chain = engines(prompt)
        for i, (label, argv) in enumerate(chain):
            outcome, detail = self._run_engine(date_dir, label, argv)
            if outcome == "ok":
                on_done(True, "")
                return
            if outcome in ("cancelled", "timeout"):
                on_done(False, detail)
                return
            has_next = i + 1 < len(chain)
            if has_next and QUOTA_RE.search(detail):
                log(f"{label} quota-limited, falling back", "WARN")
                try:
                    send(f"⏭ {label} hit its usage limit — switching to {chain[i + 1][0]}.")
                except RuntimeError:
                    pass
                continue
            on_done(False, f"{label}: {detail}")
            return

    def _run_engine(self, date_dir, label, argv):
        """Launch one engine and babysit it; returns (outcome, detail).

        Outcomes: ok | fail (detail = failure text, quota-matched for fallback) |
        cancelled | timeout.
        """
        run_log_path = date_dir / "engine-run.log"
        with run_log_path.open("ab") as run_log:
            run_log.write(f"\n===== {datetime.now().isoformat()} {label} =====\n".encode())
            run_log.flush()
            try:
                proc = subprocess.Popen(
                    argv, cwd=date_dir, stdout=run_log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                return "fail", f"could not launch: {e}"
            with self.lock:
                self.proc = proc
            log(f"engine started: {label} (pid {proc.pid})")
            progress_mid = None
            last_progress = ""
            last_edit = 0.0
            deadline = time.time() + RUN_TIMEOUT_S
            while proc.poll() is None:
                if self.cancelled:
                    _kill_tree(proc)
                    return "cancelled", "cancelled"
                if time.time() > deadline:
                    _kill_tree(proc)
                    return "timeout", f"timed out after {RUN_TIMEOUT_S / 3600:.0f}h"
                line = _tail(date_dir / "progress.log", 1)
                if line and line != last_progress and time.time() - last_edit > 20:
                    last_progress, last_edit = line, time.time()
                    text = f"⚙️ {self.kind} [{label}]: {line}"
                    try:
                        if progress_mid is None:
                            progress_mid = send(text, silent=True)["message_id"]
                        else:
                            edit_message(progress_mid, text)
                    except RuntimeError as e:
                        log(f"progress ping failed: {e}", "WARN")
                time.sleep(5)
        if proc.returncode != 0:
            failed = date_dir / "FAILED.md"
            detail = failed.read_text()[:1500] if failed.exists() else _tail_text(
                run_log_path, 1500)
            return "fail", f"exited {proc.returncode}\n\n{detail}"
        return "ok", ""


def _kill_tree(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(20):
            if proc.poll() is not None:
                return
            time.sleep(0.5)
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _tail(path, n):
    if not path.exists():
        return ""
    lines = [l for l in path.read_text(errors="replace").splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def _tail_text(path, chars):
    if not path.exists():
        return "(no log)"
    return path.read_text(errors="replace")[-chars:]


class Bot:
    def __init__(self):
        self.state = load_state()
        self.runner = Runner()

    def run_forever(self):
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            api("setMyCommands", {"commands": [
                {"command": c, "description": d} for c, d in COMMANDS]})
        except RuntimeError as e:
            log(f"setMyCommands: {e}", "WARN")
        if "update_offset" not in self.state:
            self._drain_backlog()
        log("hypebot online")
        while True:
            self._tick_schedule()
            try:
                updates = api("getUpdates", {
                    "offset": self.state.get("update_offset", 0),
                    "timeout": 25, "allowed_updates": ["message"]}, timeout=40)
            except RuntimeError as e:
                log(f"getUpdates: {e}", "WARN")
                time.sleep(5)
                continue
            for u in updates or []:
                self.state["update_offset"] = u["update_id"] + 1
                save_state(self.state)
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != str(CHAT_ID):
                    continue
                text = (msg.get("text") or "").strip()
                if text:
                    try:
                        self.handle(text)
                    except Exception as e:
                        log(f"handler error for {text!r}: {e}", "ERROR")
                        try:
                            send(f"⚠️ error handling that: {e}")
                        except RuntimeError:
                            pass

    def _drain_backlog(self):
        updates = api("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
        if updates:
            self.state["update_offset"] = updates[-1]["update_id"] + 1
        else:
            self.state["update_offset"] = 0
        save_state(self.state)

    def _tick_schedule(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if (now.hour >= PROMPT_HOUR and now.hour < 20
                and self.state.get("last_prompt_date") != today
                and not self.runner.active):
            self.state["last_prompt_date"] = today
            self.state["awaiting_brief"] = True
            save_state(self.state)
            try:
                send("🎬 What's today's batch about? Reply with a brief, or /skip.")
            except RuntimeError as e:
                log(f"morning prompt failed: {e}", "WARN")
        queue = self.state.get("posting_queue") or []
        due = [q for q in queue if q["at"] <= time.time()]
        if due:
            remaining = [q for q in queue if q["at"] > time.time()]
            self.state["posting_queue"] = remaining
            save_state(self.state)
            for q in due:
                try:
                    send(f"📤 Post #{q['n']} now — {q['label']}\n\n{q['caption']}")
                except RuntimeError as e:
                    log(f"posting reminder failed: {e}", "WARN")
            if not remaining:
                try:
                    send("🎉 That was the last one. Batch fully posted.")
                except RuntimeError as e:
                    log(f"posting reminder failed: {e}", "WARN")

    def handle(self, text):
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        rest = rest.strip()
        if cmd == "/batch":
            if rest:
                self.start_batch(rest)
            else:
                self.state["awaiting_brief"] = True
                save_state(self.state)
                send("What should the batch be about?")
        elif cmd == "/skip":
            self.state["awaiting_brief"] = False
            save_state(self.state)
            send("Skipped. /batch <brief> anytime.")
        elif cmd == "/status":
            send(f"ℹ️ {self.runner.status_line()}")
        elif cmd == "/cancel":
            if self.runner.active:
                self.runner.cancel()
                send("🛑 Cancelling…")
            else:
                send("Nothing running.")
        elif cmd == "/redo":
            self.start_redo(rest)
        elif cmd == "/last":
            self.resend_last()
        elif cmd in ("/start_posting", "/startposting"):
            self.start_posting()
        elif cmd in ("/stop_posting", "/stopposting"):
            self.state["posting_queue"] = []
            save_state(self.state)
            send("Posting reminders cancelled.")
        elif cmd in ("/help", "/start"):
            send("Commands:\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS))
        elif cmd.startswith("/"):
            send("Unknown command. /help")
        elif self.state.get("awaiting_brief"):
            self.state["awaiting_brief"] = False
            save_state(self.state)
            self.start_batch(text)
        else:
            send("No batch pending — use /batch <brief> to start one.")

    def _fresh_date_dir(self):
        base = WORK_ROOT / datetime.now().strftime("%Y-%m-%d")
        d, i = base, 2
        while d.exists():
            d = Path(f"{base}-{i}")
            i += 1
        return d

    def start_batch(self, brief):
        if self.runner.active:
            send("⏳ A run is already active — /status or /cancel first.")
            return
        date_dir = self._fresh_date_dir()
        prompt = batch_prompt(brief, date_dir, BATCH_SIZE, EDIT_SECONDS)
        self.runner.start("batch", date_dir, prompt,
                          lambda ok, err: self._batch_done(date_dir, brief, ok, err))
        send(f"🚀 Batch started: “{brief}”\n{BATCH_SIZE} edits · {date_dir}\n"
             f"I'll ping progress here. /status anytime.")
        log(f"batch started: {brief!r} → {date_dir}")

    def _batch_done(self, date_dir, brief, ok, err):
        if not ok:
            send(f"❌ Batch failed: {err}\n\nWorkdir (checkpointed, resumable): {date_dir}")
            log(f"batch failed: {err}", "ERROR")
            return
        try:
            manifest = self._validate(date_dir)
            out_dir = self._archive(date_dir, manifest, brief)
            self._deliver(date_dir, manifest, out_dir, brief)
        except Exception as e:
            send(f"❌ Batch finished but delivery failed: {e}\n\nFiles: {date_dir}/deliver/")
            log(f"delivery failed: {e}", "ERROR")

    def _validate(self, date_dir):
        mpath = date_dir / "deliver/manifest.json"
        if not mpath.exists():
            raise RuntimeError("claude exited 0 but wrote no manifest.json")
        manifest = json.loads(mpath.read_text())
        if len(manifest) != BATCH_SIZE:
            raise RuntimeError(f"manifest has {len(manifest)} entries, expected {BATCH_SIZE}")
        for e in manifest:
            f = date_dir / "deliver" / e["file"]
            if not f.exists():
                raise RuntimeError(f"missing file {e['file']}")
            meta = ffprobe(f)
            if not meta or not meta["audio"]:
                raise RuntimeError(f"{e['file']}: unreadable or no audio")
            if abs(meta["dur"] - EDIT_SECONDS) > 3:
                raise RuntimeError(f"{e['file']}: duration {meta['dur']:.1f}s, expected ~{EDIT_SECONDS}s")
            if (meta["w"], meta["h"]) != (1080, 1920):
                raise RuntimeError(f"{e['file']}: {meta['w']}x{meta['h']}, expected 1080x1920")
            if not e.get("caption"):
                raise RuntimeError(f"{e['file']}: empty caption")
        return manifest

    def _archive(self, date_dir, manifest, brief):
        out_dir = VIDEOS_DIR / date_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)
        for e in manifest:
            shutil.copy2(date_dir / "deliver" / e["file"], out_dir / e["file"])
        (out_dir / "manifest.json").write_text(json.dumps(
            {"brief": brief, "date": date_dir.name, "edits": manifest}, indent=1))
        return out_dir

    def _previews(self, date_dir, manifest):
        pdir = date_dir / "preview"
        pdir.mkdir(exist_ok=True)
        paths = []
        for e in manifest:
            src = date_dir / "deliver" / e["file"]
            if src.stat().st_size < TG_SIZE_CAP:
                paths.append(src)
            else:
                dst = pdir / e["file"]
                if not dst.exists() or dst.stat().st_size >= TG_SIZE_CAP:
                    make_preview(src, dst)
                paths.append(dst)
        return paths

    def _deliver(self, date_dir, manifest, out_dir, brief):
        paths = self._previews(date_dir, manifest)
        songs = "\n".join(f"{i + 1}. {e['player']} × {e['song']}" for i, e in enumerate(manifest))
        send(f"✅ Batch done: “{brief}”\n\n{songs}\n\n"
             f"Full-res on disk: {out_dir}\n"
             f"Album + copy-paste captions incoming. /start_posting when you begin "
             f"(pings every {CADENCE_S / 3600:g}h, posting order below).")
        self._send_album(manifest, paths)
        for i, e in enumerate(manifest):
            send(f"#{i + 1} {e['player']} caption:")
            send(e["caption"], silent=True)
        self.state["last_batch"] = {
            "date_dir": str(date_dir), "out_dir": str(out_dir), "manifest": manifest}
        self.state["posting_queue"] = []
        save_state(self.state)
        log(f"batch delivered → {out_dir}")

    def _send_album(self, manifest, paths):
        media, files = [], {}
        for i, (e, p) in enumerate(zip(manifest, paths)):
            key = f"v{i}"
            meta = ffprobe(p) or {"w": 1080, "h": 1920, "dur": EDIT_SECONDS}
            media.append({"type": "video", "media": f"attach://{key}",
                          "caption": e["caption"][:1024],
                          "width": meta["w"], "height": meta["h"],
                          "duration": int(meta["dur"]), "supports_streaming": True})
            files[key] = (e["file"], p.read_bytes())
        try:
            api("sendMediaGroup", {"chat_id": CHAT_ID, "media": json.dumps(media)},
                files=files, timeout=900)
        except RuntimeError as e:
            log(f"album failed ({e}), falling back to individual sends", "WARN")
            send(f"Album send failed ({e}) — sending individually.")
            for i, (entry, p) in enumerate(zip(manifest, paths)):
                api("sendVideo",
                    {"chat_id": CHAT_ID, "caption": entry["caption"][:1024],
                     "supports_streaming": "true"},
                    files={"video": (entry["file"], p.read_bytes())}, timeout=900)

    def resend_last(self):
        last = self.state.get("last_batch")
        if not last:
            send("No batch on record yet.")
            return
        date_dir = Path(last["date_dir"])
        if not (date_dir / "deliver").exists():
            send(f"Workdir gone; full-res still at {last['out_dir']}")
            return
        self._send_album(last["manifest"], self._previews(date_dir, last["manifest"]))

    def start_posting(self):
        last = self.state.get("last_batch")
        if not last:
            send("No batch to post yet.")
            return
        manifest = last["manifest"]
        now = time.time()
        queue = [{"at": now + i * CADENCE_S, "n": i + 1,
                  "label": f"{e['player']} × {e['song']} ({e['file']})",
                  "caption": e["caption"]}
                 for i, e in enumerate(manifest)]
        self.state["posting_queue"] = queue
        save_state(self.state)
        times = "\n".join(
            f"#{q['n']} {datetime.fromtimestamp(q['at']).strftime('%H:%M')} — {q['label']}"
            for q in queue)
        send(f"🗓 Posting schedule ({CADENCE_S / 3600:g}h cadence):\n{times}\n\n"
             f"First reminder lands now; /stop_posting to cancel.")

    def start_redo(self, rest):
        if self.runner.active:
            send("⏳ A run is already active — wait or /cancel.")
            return
        last = self.state.get("last_batch")
        if not last:
            send("No batch to redo from.")
            return
        parts = rest.split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            send("Usage: /redo <n> <feedback>, e.g. /redo 3 opener is a wide shot, want a close-up")
            return
        idx = int(parts[0]) - 1
        manifest = last["manifest"]
        if not 0 <= idx < len(manifest):
            send(f"n must be 1..{len(manifest)}")
            return
        date_dir = Path(last["date_dir"])
        if not date_dir.exists():
            send(f"Workdir {date_dir} is gone — can't redo, run a fresh /batch.")
            return
        (date_dir / "REDO_OK").unlink(missing_ok=True)
        prompt = redo_prompt(date_dir, manifest[idx], idx, parts[1])
        self.runner.start("redo", date_dir, prompt,
                          lambda ok, err: self._redo_done(date_dir, idx, ok, err))
        send(f"🔁 Redoing #{idx + 1} {manifest[idx]['player']}: “{parts[1]}”")

    def _redo_done(self, date_dir, idx, ok, err):
        if not ok:
            send(f"❌ Redo failed: {err}")
            return
        if not (date_dir / "REDO_OK").exists():
            send("❌ Redo run ended without REDO_OK marker — inspect " + str(date_dir))
            return
        try:
            manifest = json.loads((date_dir / "deliver/manifest.json").read_text())
            entry = manifest[idx]
            last = self.state["last_batch"]
            last["manifest"] = manifest
            src = date_dir / "deliver" / entry["file"]
            out_dir = Path(last["out_dir"])
            shutil.copy2(src, out_dir / entry["file"])
            (out_dir / "manifest.json").write_text(json.dumps(
                {"date": date_dir.name, "edits": manifest}, indent=1))
            save_state(self.state)
            p = src
            if src.stat().st_size >= TG_SIZE_CAP:
                p = date_dir / "preview" / entry["file"]
                make_preview(src, p)
            api("sendVideo",
                {"chat_id": CHAT_ID, "caption": entry["caption"][:1024],
                 "supports_streaming": "true"},
                files={"video": (entry["file"], p.read_bytes())}, timeout=900)
            send(f"✅ Redo #{idx + 1} done — full-res updated at {out_dir}")
        except Exception as e:
            send(f"❌ Redo delivery failed: {e}")
            log(f"redo delivery failed: {e}", "ERROR")


def main():
    if not TOKEN or not CHAT_ID:
        print("error: HYPEBOT_TOKEN and HYPEBOT_CHAT_ID must be set "
              "(see ~/.config/hypebot/secrets.env)", file=sys.stderr)
        return 2
    bot = Bot()
    if "--prompt-now" in sys.argv:
        bot.state.pop("last_prompt_date", None)
        save_state(bot.state)
    bot.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
