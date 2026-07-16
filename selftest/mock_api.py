#!/usr/bin/env python3
"""Mock Telegram Bot API for hypebot's selftest.

Serves /bot<token>/<method>, records every call as JSONL to $MOCK_LOG, and
plays one scenario: the first getUpdates after the startup drain delivers
"/batch <MOCK_BRIEF>", everything after that is empty. sendMediaGroup bodies
get a light multipart parse so the assertions can check item count + captions.
"""
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = os.environ["MOCK_LOG"]
BRIEF = os.environ.get("MOCK_BRIEF", "test brief")
LOCK = threading.Lock()
STATE = {"drained": False, "batch_sent": False, "message_id": 100}


def record(entry):
    with LOCK:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _reply(self, result):
        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        method = self.path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            payload = json.loads(raw or b"{}")
        else:
            payload = self._parse_multipart(raw)
        entry = {"method": method, "payload": payload}
        if method == "getUpdates":
            self._get_updates(entry)
            return
        record(entry)
        with LOCK:
            STATE["message_id"] += 1
            mid = STATE["message_id"]
        self._reply({"message_id": mid} if method != "setMyCommands" else True)

    do_GET = do_POST

    def _parse_multipart(self, raw):
        text = raw.decode("latin-1")
        fields = {}
        for m in re.finditer(
                r'name="([^"]+)"(?:; filename="([^"]*)")?\r\n(?:[^\r\n]+\r\n)*\r\n', text):
            name, filename = m.group(1), m.group(2)
            if filename is not None:
                fields.setdefault("_files", []).append(
                    {"field": name, "filename": filename})
            else:
                end = text.find("\r\n--", m.end())
                fields[name] = text[m.end():end]
        return fields

    def _get_updates(self, entry):
        with LOCK:
            if not STATE["drained"]:
                STATE["drained"] = True
                step = "drain"
            elif not STATE["batch_sent"]:
                STATE["batch_sent"] = True
                step = "batch"
            else:
                step = "empty"
        record(entry)
        if step == "batch":
            self._reply([{
                "update_id": 1,
                "message": {"message_id": 1,
                            "chat": {"id": int(os.environ["HYPEBOT_CHAT_ID"])},
                            "text": f"/batch {BRIEF}"}}])
            return
        if step == "empty":
            time.sleep(0.5)
        self._reply([])


def main():
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
