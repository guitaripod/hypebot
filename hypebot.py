#!/usr/bin/env python3
"""hypebot — Telegram-fronted hype-edit batch automation.

Long-polls a dedicated Telegram bot. On prompt days (default Mon + Thu) it asks
what the batch should be about; the reply (or /batch <brief> anytime) launches a headless
Claude Code run that executes the hype-edit skill in batch mode. On success the
finished edits land as ONE Telegram album (per-video TikTok captions), the
captions repeat as copy-friendly single messages, and full-res files are copied
to ~/Videos/hype/<date>/ for desktop posting. /start_posting then paces the
3-hour posting cadence with reminder pings.

Config via environment (see ~/.config/hypebot/secrets.env):
  HYPEBOT_TOKEN, HYPEBOT_CHAT_ID          required
  HYPEBOT_API_BASE                        default https://api.telegram.org
  HYPEBOT_CLAUDE                          default "claude"
  HYPEBOT_WORK_ROOT                       default /mnt/games-nvme-gen4/hypebot
  HYPEBOT_VIDEOS_DIR                      default ~/Videos/hype
  HYPEBOT_PROMPT_HOUR                     default 9
  HYPEBOT_PROMPT_DAYS                     default mon,thu
  HYPEBOT_BATCH_SIZE                      default 5
  HYPEBOT_EDIT_SECONDS                    default 15
  HYPEBOT_RUN_TIMEOUT_HOURS               default 6
  HYPEBOT_CADENCE_HOURS                   default 3
  HYPEBOT_MIN_FREE_GB                     default 60 (refuse batch below this)
  HYPEBOT_KEEP_WORKDIRS                   default 4 (older ones pruned pre-batch)
  HYPEBOT_PREFLIGHT                       default 1 (0 disables prune/disk/yt-dlp checks)
  HYPEBOT_EFFORT                          default high (claude reasoning effort)
  HYPEBOT_OPENCODE_VARIANT                default high (grok reasoning variant)
"""

import http.client
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
def _resolve_bin(name):
    """Resolve a launcher to an absolute path so we don't depend on the
    service's PATH (systemd --user omits ~/.local/bin, where claude lives)."""
    if os.path.sep in name:
        return name
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".local/bin" / name
    return str(fallback) if fallback.exists() else name


CLAUDE_BIN = _resolve_bin(os.environ.get("HYPEBOT_CLAUDE", "claude"))
CLAUDE_MODEL = os.environ.get("HYPEBOT_CLAUDE_MODEL", "claude-opus-4-8")
CLAUDE_EFFORT = os.environ.get("HYPEBOT_EFFORT", "high")
OPENCODE_BIN = _resolve_bin(os.environ.get("HYPEBOT_OPENCODE", "opencode"))
OPENCODE_MODEL = os.environ.get("HYPEBOT_OPENCODE_MODEL", "xai/grok-4.5")
OPENCODE_VARIANT = os.environ.get("HYPEBOT_OPENCODE_VARIANT", "high")
SKILL_MD = os.environ.get(
    "HYPEBOT_SKILL_MD", str(Path.home() / ".claude/skills/hype-edit/SKILL.md"))
WORK_ROOT = Path(os.environ.get("HYPEBOT_WORK_ROOT", "/mnt/games-nvme-gen4/hypebot"))
VIDEOS_DIR = Path(os.environ.get("HYPEBOT_VIDEOS_DIR", str(Path.home() / "Videos/hype")))
PROMPT_HOUR = int(os.environ.get("HYPEBOT_PROMPT_HOUR", "9"))
PROMPT_DAYS = {d.strip()[:3].lower() for d in
               os.environ.get("HYPEBOT_PROMPT_DAYS", "mon,thu").split(",") if d.strip()}
BATCH_SIZE = int(os.environ.get("HYPEBOT_BATCH_SIZE", "5"))
EDIT_SECONDS = int(os.environ.get("HYPEBOT_EDIT_SECONDS", "15"))
RUN_TIMEOUT_S = float(os.environ.get("HYPEBOT_RUN_TIMEOUT_HOURS", "10")) * 3600
CADENCE_S = float(os.environ.get("HYPEBOT_CADENCE_HOURS", "3")) * 3600
MIN_FREE_GB = float(os.environ.get("HYPEBOT_MIN_FREE_GB", "60"))
KEEP_WORKDIRS = int(os.environ.get("HYPEBOT_KEEP_WORKDIRS", "4"))
PREFLIGHT = os.environ.get("HYPEBOT_PREFLIGHT", "1") != "0"
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


_STATE_LOCK = threading.Lock()


def save_state(state):
    with _STATE_LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(f".tmp{threading.get_ident()}")
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
        safe = re.sub(r'[\\"\r\n]', "_", filename)
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
             f"filename=\"{safe}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
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
    for attempt in range(5):
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
            except (ValueError, OSError, http.client.HTTPException):
                data = None
            if data is None or e.code >= 500:
                last_err = e
                log(f"telegram {method} http {e.code} (attempt {attempt + 1})", "WARN")
                time.sleep(min(2 ** attempt, 30))
                continue
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException, ValueError) as e:
            last_err = e
            log(f"telegram {method} transport error (attempt {attempt + 1}): {e}", "WARN")
            time.sleep(min(2 ** attempt, 30))
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


def send(text, silent=False, buttons=None):
    """sendMessage; buttons = rows of (label, callback_data) inline keys."""
    payload = {"chat_id": CHAT_ID, "text": text, "disable_notification": silent}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [
            [{"text": lb, "callback_data": cd} for lb, cd in row] for row in buttons]}
    return api("sendMessage", payload)


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
    kbps = max(int((PREVIEW_TARGET * 8 / dur - 192_000) / 1000), 500)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part.mp4")
    try:
        for _ in range(4):
            for codec in (["h264_nvenc", "-rc", "vbr", "-preset", "p5"],
                          ["libx264", "-preset", "medium"]):
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                     "-c:v", codec[0], *codec[1:], "-b:v", f"{kbps}k",
                     "-maxrate", f"{int(kbps * 1.1)}k", "-bufsize", f"{kbps * 2}k",
                     "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                     "-f", "mp4", str(tmp)],
                    capture_output=True, text=True)
                if r.returncode == 0:
                    break
            else:
                raise RuntimeError(f"preview encode failed for {src.name}")
            if tmp.stat().st_size < TG_SIZE_CAP:
                tmp.replace(dst)
                return
            kbps = int(kbps * 0.85)
        raise RuntimeError(f"preview for {src.name} won't fit under 50MB")
    finally:
        tmp.unlink(missing_ok=True)


def engines(prompt):
    return [
        (f"{CLAUDE_MODEL} (claude code, {CLAUDE_EFFORT} effort)",
         [CLAUDE_BIN, "-p", prompt, "--model", CLAUDE_MODEL, "--effort", CLAUDE_EFFORT,
          "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]),
        (f"{OPENCODE_MODEL} (opencode, {OPENCODE_VARIANT} variant)",
         [OPENCODE_BIN, "run", prompt, "-m", OPENCODE_MODEL, "--variant", OPENCODE_VARIANT,
          "--format", "json", "--auto", "--print-logs"]),
    ]


QUOTA_RE = re.compile(
    r"hit your (session|usage|weekly|5.hour)? ?limit|usage limit"
    r"|out of (usage|quota)|quota exceeded|credit balance is too low"
    r"|limit (reached|will reset) ?[·∙|]|resets \d{1,2}(:\d{2})? ?[ap]m", re.I)


def batch_prompt(brief, date_dir, n, seconds):
    return f"""You are hypebot's unattended worker. Produce a TikTok hype-edit batch end to end by reading {SKILL_MD} fully (plus its reference/ docs) and following it exactly, including BATCH MODE. No user is available — make every decision yourself and see it through to completion.

Marcus's brief for today: "{brief}"

Parameters:
- {n} edits, each {seconds}s, REMASTER style (extract_audio.py --style remaster --pitch 1.03): full-bleed 90deg-rotated landscape at 1080x1920@60, 4K-remaster grade, slow-mo motion-interpolated, iconic moments only — per SKILL.md "Styles". Every edit also gets its landscape companion (render.py --landscape, 1920x1080@60).
- Work under {date_dir}/ — one workdir per edit (edit1..edit{n}). Share the source pool per SKILL.md batch mode where subjects overlap; enforce ZERO clip overlap across edits by cascading exclude_clips.
- Song selection: current edit-culture/phonk-leaning picks appropriate to the brief; web-search to confirm relevance/trendiness today. Read {VIDEOS_DIR}/*/manifest.json (if any exist) and avoid repeating recent song or player+song combos.
- Quality bar is the full SKILL.md loop, no shortcuts: seg-grid review rounds until clean, opener retention gate (subject clearly in view from frame one), the song's main hook as the centerpiece (drop mid-edit with the most iconic moment detonating exactly on it), segment-level hero verification, render-exact probing, and qc.py printing "ALL GATES PASS" for every edit.
- Do NOT run AI restoration: SeedVR2 (restore.py) is OFF by default (its detail gain is imperceptible after TikTok re-encode + phone downscale and costs ~1hr GPU per edit). The remaster look comes from RIFE 60fps + the grade in render.py, which run automatically — restoration adds nothing worth the time here.
- Captions: engagement-optimized TikTok captions with hashtags per the established style.

Progress protocol: append one line to {date_dir}/progress.log at every milestone, format "phase | detail" (e.g. "sourcing | edit2: 14 sources fetched", "review | edit4: round 2 clean"). The Telegram bot relays these to Marcus.

Delivery contract (STRICT):
1. Only after every edit passes qc, create {date_dir}/deliver/ containing the final mp4s named 01_<player>_<song>.mp4 .. {n:02d}_<player>_<song>.mp4 in recommended posting order, PLUS each edit's landscape companion named 01_<player>_<song>_landscape.mp4 etc.
2. Write {date_dir}/deliver/manifest.json LAST — its existence signals success. JSON array, posting order, entries: {{"file": "<name in deliver/>", "landscape": "<landscape name in deliver/>", "player": "...", "song": "...", "caption": "..."}}.
3. If something fails irrecoverably, write {date_dir}/FAILED.md (what failed, where to resume) and exit nonzero. Never write a partial manifest."""


def redo_prompt(date_dir, entry, idx, feedback):
    return f"""You are hypebot's unattended worker. One edit from an existing hype-edit batch needs a redo based on Marcus's feedback. The batch lives in {date_dir}/ (checkpointed workdirs edit1..editN, deliverables in deliver/, manifest.json).

Edit #{idx + 1}: {entry['player']} × {entry['song']} → deliver/{entry['file']}

Marcus's feedback: "{feedback}"

Apply the feedback using the hype-edit skill's iteration loop (seg-grid review, exclude_clips, hero_overrides — read {SKILL_MD} fully). Re-render (portrait AND --landscape), re-run qc until "ALL GATES PASS", then overwrite deliver/{entry['file']} + deliver/{entry.get('landscape', entry['file'].replace('.mp4', '_landscape.mp4'))} and update this entry's caption in deliver/manifest.json if the feedback warrants it. Append progress lines to {date_dir}/progress.log. Touch nothing belonging to the other edits. On success write {date_dir}/REDO_OK, on irrecoverable failure write {date_dir}/FAILED.md and exit nonzero."""


class Runner:
    """Owns the single active engine subprocess (claude -p, opencode fallback)."""

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
        def done(ok, err):
            try:
                on_done(ok, err)
            except Exception as e:
                log(f"on_done crashed: {e}", "ERROR")

        date_dir.mkdir(parents=True, exist_ok=True)
        (date_dir / "FAILED.md").unlink(missing_ok=True)
        chain = engines(prompt)
        for i, (label, argv) in enumerate(chain):
            outcome, detail = self._run_engine(date_dir, label, argv)
            if outcome == "ok":
                done(True, "")
                return
            if outcome in ("cancelled", "timeout"):
                done(False, detail)
                return
            has_next = i + 1 < len(chain)
            quota_hit = (not (date_dir / "FAILED.md").exists()
                         and QUOTA_RE.search(_tail_text(date_dir / "engine-run.log", 800)))
            if has_next and quota_hit:
                log(f"{label} quota-limited, falling back", "WARN")
                try:
                    send(f"⏭ {label} hit its usage limit — switching to {chain[i + 1][0]}.")
                except RuntimeError:
                    pass
                continue
            done(False, f"{label}: {detail}")
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
        if self.cancelled:
            return "cancelled", "cancelled"
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
        self._prompt_retry_at = 0.0
        self._resending = threading.Event()

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
        self._recover_interrupted()
        log("hypebot online")
        while True:
            self._tick_schedule()
            try:
                updates = api("getUpdates", {
                    "offset": self.state.get("update_offset", 0),
                    "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                    timeout=40)
            except RuntimeError as e:
                log(f"getUpdates: {e}", "WARN")
                time.sleep(5)
                continue
            for u in updates or []:
                self.state["update_offset"] = u["update_id"] + 1
                save_state(self.state)
                text = self._update_text(u)
                if text:
                    try:
                        self.handle(text)
                    except Exception as e:
                        log(f"handler error for {text!r}: {e}", "ERROR")
                        try:
                            send(f"⚠️ error handling that: {e}")
                        except RuntimeError:
                            pass

    def _update_text(self, u):
        """Normalize an update to a command string, or "" to ignore.

        Inline-button taps arrive as callback_query with data like "skip";
        they map onto the same /command handlers as typed text.
        """
        cq = u.get("callback_query")
        if cq:
            try:
                api("answerCallbackQuery", {"callback_query_id": cq["id"]})
            except RuntimeError as e:
                log(f"answerCallbackQuery: {e}", "WARN")
            if str((cq.get("from") or {}).get("id")) != str(CHAT_ID):
                return ""
            data = (cq.get("data") or "").strip()
            return f"/{data}" if data else ""
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(CHAT_ID):
            return ""
        return (msg.get("text") or "").strip()

    def _drain_backlog(self):
        updates = api("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
        if updates:
            self.state["update_offset"] = updates[-1]["update_id"] + 1
        else:
            self.state["update_offset"] = 0
        save_state(self.state)

    def _recover_interrupted(self):
        """Handle a run that was live when the previous process died.

        The engine child shares the systemd cgroup, so any service stop/restart
        killed it. If the batch had already rendered (manifest present) deliver
        it now; otherwise point at the checkpointed workdir.
        """
        run = self.state.get("active_run")
        if not run:
            return
        self.state["active_run"] = None
        save_state(self.state)
        date_dir = Path(run["date_dir"])
        note = f"⚠️ hypebot restarted mid-{run['kind']} — the engine run was killed. "
        if run["kind"] == "batch" and (date_dir / "deliver/manifest.json").exists():
            try:
                send(note + "Rendering had finished; delivering the batch now.")
            except RuntimeError:
                pass
            self._batch_done(date_dir, run.get("brief", "(recovered)"), True, "")
            return
        try:
            send(note + f"Workdir is checkpointed at {date_dir} — resume it in a live "
                        f"session, or /batch to start fresh.")
        except RuntimeError as e:
            log(f"recovery notice failed: {e}", "WARN")

    def _tick_schedule(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if (now.strftime("%a").lower() in PROMPT_DAYS
                and now.hour >= PROMPT_HOUR and now.hour < 20
                and self.state.get("last_prompt_date") != today):
            if self.runner.active:
                self.state["last_prompt_date"] = today
                save_state(self.state)
            elif time.time() >= self._prompt_retry_at:
                try:
                    send("🎬 Batch day — what should this one be about? "
                         "Reply with a brief.",
                         buttons=[[("⏭ Skip today", "skip")]])
                    self.state["last_prompt_date"] = today
                    self.state["awaiting_brief"] = True
                    save_state(self.state)
                except RuntimeError as e:
                    log(f"morning prompt failed, retrying in 10min: {e}", "WARN")
                    self._prompt_retry_at = time.time() + 600
        queue = self.state.get("posting_queue") or []
        due = [q for q in queue if q["at"] <= time.time()]
        if due:
            keep = [q for q in queue if q["at"] > time.time()]
            for q in due:
                try:
                    send(f"📤 Post #{q['n']} now — {q['label']}\n\n{q['caption']}")
                except RuntimeError as e:
                    log(f"posting reminder failed, re-queued +2min: {e}", "WARN")
                    q["at"] = time.time() + 120
                    keep.append(q)
            self.state["posting_queue"] = sorted(keep, key=lambda q: q["at"])
            save_state(self.state)
            if not keep:
                try:
                    send("🎉 That was the last one. Batch fully posted.")
                except RuntimeError as e:
                    log(f"posting wrap-up failed: {e}", "WARN")

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
        while d.exists() or (VIDEOS_DIR / d.name).exists():
            d = Path(f"{base}-{i}")
            i += 1
        return d

    def _prune_workdirs(self):
        """Old workdirs are only needed for /redo and /last on the latest batch;
        full-res deliverables were archived to VIDEOS_DIR at delivery time."""
        protected = {Path(self.state.get("last_batch", {}).get("date_dir", "x")).name}
        dirs = sorted(d for d in WORK_ROOT.iterdir()
                      if d.is_dir() and d.name not in protected)
        for d in dirs[:-KEEP_WORKDIRS] if KEEP_WORKDIRS else dirs:
            try:
                shutil.rmtree(d)
                log(f"pruned old workdir {d}")
            except OSError as e:
                log(f"prune failed for {d}: {e}", "WARN")

    def _ytdlp_ok(self):
        try:
            r = subprocess.run(
                ["yt-dlp", "--simulate", "--quiet", "--no-warnings", "--no-playlist",
                 "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
                capture_output=True, text=True, timeout=45)
            return r.returncode == 0, r.stderr.strip()[-200:]
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)

    def start_batch(self, brief):
        if self.runner.active:
            send("⏳ A run is already active — /status or /cancel first.")
            return
        self.state["awaiting_brief"] = False
        if PREFLIGHT:
            self._prune_workdirs()
            free_gb = shutil.disk_usage(WORK_ROOT).free / 1e9
            if free_gb < MIN_FREE_GB:
                send(f"❌ Not starting: only {free_gb:.0f}GB free under {WORK_ROOT} "
                     f"(need {MIN_FREE_GB:.0f}GB even after pruning old workdirs). "
                     f"Free up space and retry.")
                save_state(self.state)
                return
            ok, why = self._ytdlp_ok()
            if not ok:
                self._safe_send(f"⚠️ yt-dlp preflight failed ({why or 'no detail'}) — "
                                f"sourcing may 429. Starting anyway; if the run fails, "
                                f"refresh the Vivaldi cookies.")
        date_dir = self._fresh_date_dir()
        self.state["active_run"] = {"kind": "batch", "date_dir": str(date_dir),
                                    "brief": brief, "started": time.time()}
        save_state(self.state)
        prompt = batch_prompt(brief, date_dir, BATCH_SIZE, EDIT_SECONDS)
        self.runner.start("batch", date_dir, prompt,
                          lambda ok, err: self._batch_done(date_dir, brief, ok, err))
        send(f"🚀 Batch started: “{brief}”\n{BATCH_SIZE} edits · {date_dir}\n"
             f"I'll ping progress here.",
             buttons=[[("ℹ️ Status", "status"), ("🛑 Cancel", "cancel")]])
        log(f"batch started: {brief!r} → {date_dir}")

    def _batch_done(self, date_dir, brief, ok, err):
        self.state["active_run"] = None
        save_state(self.state)
        if not ok:
            if err == "cancelled":
                self._safe_send(f"🛑 Cancelled. Workdir checkpointed at {date_dir}.")
                log("batch cancelled")
            else:
                self._safe_send(f"❌ Batch failed: {err}\n\n"
                                f"Workdir (checkpointed, resumable): {date_dir}")
                log(f"batch failed: {err}", "ERROR")
            return
        try:
            manifest = self._validate(date_dir)
            out_dir = self._archive(date_dir, manifest, brief)
            dropped = len(self.state.get("posting_queue") or [])
            self.state["last_batch"] = {
                "date_dir": str(date_dir), "out_dir": str(out_dir), "manifest": manifest}
            self.state["posting_queue"] = []
            save_state(self.state)
            if dropped:
                self._safe_send(f"ℹ️ Cleared {dropped} pending posting reminder(s) "
                                f"from the previous batch.")
            self._deliver(manifest, date_dir, out_dir, brief)
            log(f"batch delivered → {out_dir}")
        except Exception as e:
            self._safe_send(f"❌ Batch finished but delivery hit an error: {e}\n\n"
                            f"Full-res files: {date_dir}/deliver/ — /last retries the album.")
            log(f"delivery failed: {e}", "ERROR")

    def _safe_send(self, text):
        try:
            send(text)
        except RuntimeError as e:
            log(f"send failed: {e}", "ERROR")

    def _validate(self, date_dir):
        mpath = date_dir / "deliver/manifest.json"
        if not mpath.exists():
            raise RuntimeError("claude exited 0 but wrote no manifest.json")
        manifest = json.loads(mpath.read_text())
        if len(manifest) != BATCH_SIZE:
            raise RuntimeError(f"manifest has {len(manifest)} entries, expected {BATCH_SIZE}")
        for e in manifest:
            for k in ("file", "landscape", "player", "song", "caption"):
                if not isinstance(e.get(k), str) or not e[k].strip():
                    raise RuntimeError(f"manifest entry missing/empty '{k}': {e}")
            for k, dims in (("file", (1080, 1920)), ("landscape", (1920, 1080))):
                if "/" in e[k] or '"' in e[k]:
                    raise RuntimeError(f"unsafe file name in manifest: {e[k]!r}")
                f = date_dir / "deliver" / e[k]
                if not f.exists():
                    raise RuntimeError(f"missing file {e[k]}")
                meta = ffprobe(f)
                if not meta or not meta["audio"]:
                    raise RuntimeError(f"{e[k]}: unreadable or no audio")
                if abs(meta["dur"] - EDIT_SECONDS) > 3:
                    raise RuntimeError(f"{e[k]}: duration {meta['dur']:.1f}s, expected ~{EDIT_SECONDS}s")
                if (meta["w"], meta["h"]) != dims:
                    raise RuntimeError(f"{e[k]}: {meta['w']}x{meta['h']}, expected {dims[0]}x{dims[1]}")
        return manifest

    def _archive(self, date_dir, manifest, brief):
        out_dir = VIDEOS_DIR / date_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)
        for e in manifest:
            shutil.copy2(date_dir / "deliver" / e["file"], out_dir / e["file"])
            shutil.copy2(date_dir / "deliver" / e["landscape"], out_dir / e["landscape"])
        (out_dir / "manifest.json").write_text(json.dumps(
            {"brief": brief, "date": date_dir.name, "edits": manifest}, indent=1))
        return out_dir

    def _previews(self, date_dir, manifest, key="file"):
        pdir = date_dir / "preview"
        paths = []
        for e in manifest:
            src = date_dir / "deliver" / e[key]
            if src.stat().st_size < TG_SIZE_CAP:
                paths.append(src)
            else:
                dst = pdir / e[key]
                if (not dst.exists() or dst.stat().st_size >= TG_SIZE_CAP
                        or dst.stat().st_mtime < src.stat().st_mtime):
                    make_preview(src, dst)
                paths.append(dst)
        return paths

    def _deliver(self, manifest, date_dir, out_dir, brief):
        ls_paths = self._previews(date_dir, manifest, key="landscape")
        songs = "\n".join(f"{i + 1}. {e['player']} × {e['song']}" for i, e in enumerate(manifest))
        send(f"✅ Batch done: “{brief}”\n\n{songs}\n\n"
             f"Full-res (portrait + landscape) on disk: {out_dir}\n"
             f"Landscape album + copy-paste captions incoming. "
             f"Tap ▶️ when you start posting "
             f"(pings every {CADENCE_S / 3600:g}h, posting order below).",
             buttons=[[("▶️ Start posting", "start_posting")]])
        self._send_album(manifest, ls_paths, key="landscape",
                         captions=[f"#{i + 1} {e['player']} × {e['song']}"
                                   for i, e in enumerate(manifest)])
        for i, e in enumerate(manifest):
            send(f"#{i + 1} {e['player']} caption:")
            send(e["caption"], silent=True)

    def _send_album(self, manifest, paths, key="file", captions=None):
        media, files = [], {}
        for i, (e, p) in enumerate(zip(manifest, paths)):
            k = f"v{i}"
            meta = ffprobe(p) or {"w": 1080, "h": 1920, "dur": EDIT_SECONDS}
            cap = captions[i] if captions else e["caption"]
            media.append({"type": "video", "media": f"attach://{k}",
                          "caption": cap[:1024],
                          "width": meta["w"], "height": meta["h"],
                          "duration": int(meta["dur"]), "supports_streaming": True})
            files[k] = (e[key], p.read_bytes())
        try:
            api("sendMediaGroup", {"chat_id": CHAT_ID, "media": json.dumps(media)},
                files=files, timeout=900)
        except RuntimeError as err:
            log(f"album failed ({err}), falling back to individual sends", "WARN")
            self._safe_send(f"Album send failed ({err}) — sending individually.")
            failures = []
            for i, (entry, p) in enumerate(zip(manifest, paths)):
                cap = captions[i] if captions else entry["caption"]
                try:
                    api("sendVideo",
                        {"chat_id": CHAT_ID, "caption": cap[:1024],
                         "supports_streaming": "true"},
                        files={"video": (entry[key], p.read_bytes())}, timeout=900)
                except RuntimeError as ve:
                    failures.append(f"{entry[key]}: {ve}")
            if failures:
                raise RuntimeError("individual sends failed — " + "; ".join(failures))

    def resend_last(self):
        last = self.state.get("last_batch")
        if not last:
            send("No batch on record yet.")
            return
        date_dir = Path(last["date_dir"])
        if not (date_dir / "deliver").exists():
            send(f"Workdir gone; full-res still at {last['out_dir']}")
            return
        if self._resending.is_set():
            send("⏳ Already re-sending.")
            return
        self._resending.set()
        threading.Thread(target=self._resend_worker,
                         args=(last["manifest"], date_dir), daemon=True).start()
        send("📦 Re-sending the last album…", silent=True)

    def _resend_worker(self, manifest, date_dir):
        try:
            self._send_album(manifest, self._previews(date_dir, manifest, key="landscape"),
                             key="landscape")
        except Exception as e:
            self._safe_send(f"❌ Re-send failed: {e}")
            log(f"resend failed: {e}", "ERROR")
        finally:
            self._resending.clear()

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
             f"First reminder lands now.",
             buttons=[[("🛑 Stop reminders", "stop_posting")]])

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
        self.state["active_run"] = {"kind": "redo", "date_dir": str(date_dir),
                                    "started": time.time()}
        save_state(self.state)
        prompt = redo_prompt(date_dir, manifest[idx], idx, parts[1])
        self.runner.start("redo", date_dir, prompt,
                          lambda ok, err: self._redo_done(date_dir, idx, ok, err))
        send(f"🔁 Redoing #{idx + 1} {manifest[idx]['player']}: “{parts[1]}”",
             buttons=[[("ℹ️ Status", "status"), ("🛑 Cancel", "cancel")]])

    def _redo_done(self, date_dir, idx, ok, err):
        self.state["active_run"] = None
        save_state(self.state)
        if not ok:
            self._safe_send("🛑 Redo cancelled." if err == "cancelled"
                            else f"❌ Redo failed: {err}")
            return
        if not (date_dir / "REDO_OK").exists():
            self._safe_send("❌ Redo run ended without REDO_OK marker — inspect " + str(date_dir))
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
            for q in self.state.get("posting_queue") or []:
                if q["n"] == idx + 1:
                    q["caption"] = entry["caption"]
                    q["label"] = f"{entry['player']} × {entry['song']} ({entry['file']})"
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
            self._safe_send(f"❌ Redo delivery failed: {e}")
            log(f"redo delivery failed: {e}", "ERROR")


def main():
    if not TOKEN or not CHAT_ID:
        print("error: HYPEBOT_TOKEN and HYPEBOT_CHAT_ID must be set "
              "(see ~/.config/hypebot/secrets.env)", file=sys.stderr)
        return 2
    bot = Bot()

    def on_sigterm(signum, frame):
        run = bot.state.get("active_run")
        if run and bot.runner.active:
            try:
                send(f"🛑 hypebot stopping — {run['kind']} interrupted; workdir "
                     f"checkpointed at {run['date_dir']}.")
            except Exception:
                pass
        save_state(bot.state)
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_sigterm)
    if "--prompt-now" in sys.argv:
        bot.state["awaiting_brief"] = True
        bot.state["last_prompt_date"] = datetime.now().strftime("%Y-%m-%d")
        save_state(bot.state)
        send("🎬 What should the batch be about? Reply with a brief.",
             buttons=[[("⏭ Skip", "skip")]])
    bot.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
