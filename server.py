#!/usr/bin/env python3
"""
AudioStream - stream your music and audiobooks to any browser.

Pure Python standard library, no dependencies.

Usage:
    python3 server.py --root ~/Music --root ~/Audiobooks
    python3 server.py --root / --port 8080 --password secret

Then open http://<this-machine>:8000 in a browser.
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

AUDIO_EXTS = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
CHUNK = 256 * 1024


def natural_key(s):
    """Sort 'Chapter 2' before 'Chapter 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


class ProgressStore:
    """Remembers playback position per file in a JSON file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"files": {}, "last": None}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                self.data.setdefault("files", {})
                self.data.setdefault("last", None)
            except Exception as e:
                print(f"Warning: could not read {path}: {e}", file=sys.stderr)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def get(self, fpath):
        with self.lock:
            return self.data["files"].get(fpath)

    def set(self, fpath, position, duration):
        with self.lock:
            finished = bool(duration) and position >= duration - 5
            self.data["files"][fpath] = {
                "position": round(float(position), 1),
                "duration": round(float(duration or 0), 1),
                "finished": finished,
                "updated": time.time(),
            }
            self.data["last"] = fpath
            self._save()
            return self.data["files"][fpath]

    def clear(self, fpath):
        with self.lock:
            self.data["files"].pop(fpath, None)
            if self.data.get("last") == fpath:
                self.data["last"] = None
            self._save()

    def snapshot(self):
        with self.lock:
            return dict(self.data["files"]), self.data.get("last")


class Handler(BaseHTTPRequestHandler):
    server_version = "AudioStream/1.0"
    roots = []
    store = None
    password = None
    show_hidden = False

    # ---------- helpers ----------
    def log_message(self, fmt, *args):
        if "/api/progress" in (self.path or ""):
            return  # too chatty
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, msg):
        self.send_json({"error": msg}, status)

    def check_auth(self):
        if not self.password:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user_pass = base64.b64decode(hdr[6:]).decode("utf-8")
                if user_pass.split(":", 1)[-1] == self.password:
                    return True
            except Exception:
                pass
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="AudioStream"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def safe_path(self, p):
        """Resolve p and make sure it's inside one of the allowed roots."""
        if not p:
            return None
        real = os.path.realpath(os.path.expanduser(p))
        for r in self.roots:
            try:
                if os.path.commonpath([real, r]) == r:
                    return real
            except ValueError:
                continue
        return None

    def qs(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    # ---------- routing ----------
    def do_GET(self):
        if not self.check_auth():
            return
        route = urlparse(self.path).path
        try:
            if route in ("/", "/index.html"):
                return self.serve_static("index.html")
            if route.startswith("/static/"):
                return self.serve_static(route[len("/static/"):])
            if route == "/api/roots":
                return self.api_roots()
            if route == "/api/list":
                return self.api_list()
            if route == "/api/progress":
                return self.api_get_progress()
            if route == "/api/recent":
                return self.api_recent()
            if route == "/stream":
                return self.stream()
            self.send_error_json(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        if not self.check_auth():
            return
        if urlparse(self.path).path == "/stream":
            return self.stream(head_only=True)
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        if not self.check_auth():
            return
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_error_json(400, "bad json")
        if route == "/api/progress":
            return self.api_set_progress(payload)
        if route == "/api/progress/clear":
            fp = self.safe_path(payload.get("path"))
            if not fp:
                return self.send_error_json(403, "path not allowed")
            self.store.clear(fp)
            return self.send_json({"ok": True})
        self.send_error_json(404, "not found")

    # ---------- static ----------
    def serve_static(self, name):
        fp = os.path.realpath(os.path.join(STATIC_DIR, name))
        if os.path.commonpath([fp, STATIC_DIR]) != STATIC_DIR or not os.path.isfile(fp):
            return self.send_error_json(404, "not found")
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---------- API ----------
    def api_roots(self):
        self.send_json({"roots": self.roots})

    def api_list(self):
        q = self.qs()
        path = self.safe_path(q.get("path") or self.roots[0])
        if not path or not os.path.isdir(path):
            return self.send_error_json(403, "path not allowed or not a directory")

        files_progress, last = self.store.snapshot()
        dirs, files = [], []
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            return self.send_error_json(403, "permission denied")

        for e in entries:
            if not self.show_hidden and e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    full = os.path.join(path, e.name)
                    prefix = full + os.sep
                    has_prog = any(k.startswith(prefix) for k in files_progress)
                    dirs.append({"name": e.name, "path": full, "has_progress": has_prog})
                elif e.is_file():
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext in AUDIO_EXTS:
                        full = os.path.join(path, e.name)
                        files.append({
                            "name": e.name,
                            "path": full,
                            "size": e.stat().st_size,
                            "progress": files_progress.get(full),
                        })
            except OSError:
                continue

        dirs.sort(key=lambda d: natural_key(d["name"]))
        files.sort(key=lambda f: natural_key(f["name"]))

        # Most recently played file in this folder -> "resume" target
        resume = None
        best = 0
        for f in files:
            p = f["progress"]
            if p and p["updated"] > best:
                best, resume = p["updated"], f["path"]

        parent = os.path.dirname(path)
        if path in self.roots or not self.safe_path(parent):
            parent = None

        self.send_json({
            "path": path,
            "parent": parent,
            "root": next((r for r in self.roots if os.path.commonpath([path, r]) == r), None),
            "dirs": dirs,
            "files": files,
            "resume": resume,
        })

    def api_get_progress(self):
        fp = self.safe_path(self.qs().get("path"))
        if not fp:
            return self.send_error_json(403, "path not allowed")
        self.send_json({"path": fp, "progress": self.store.get(fp)})

    def api_set_progress(self, payload):
        fp = self.safe_path(payload.get("path"))
        if not fp or not os.path.isfile(fp):
            return self.send_error_json(403, "path not allowed")
        try:
            pos = float(payload.get("position", 0))
            dur = float(payload.get("duration", 0) or 0)
        except (TypeError, ValueError):
            return self.send_error_json(400, "bad numbers")
        self.send_json({"ok": True, "progress": self.store.set(fp, pos, dur)})

    def api_recent(self):
        files_progress, last = self.store.snapshot()
        items = []
        for fp, p in files_progress.items():
            if os.path.isfile(fp) and self.safe_path(fp):
                items.append({
                    "path": fp,
                    "name": os.path.basename(fp),
                    "folder": os.path.dirname(fp),
                    "progress": p,
                })
        items.sort(key=lambda i: i["progress"]["updated"], reverse=True)
        self.send_json({"last": last, "items": items[:20]})

    # ---------- streaming with HTTP Range ----------
    def stream(self, head_only=False):
        fp = self.safe_path(self.qs().get("path"))
        if not fp or not os.path.isfile(fp):
            return self.send_error_json(404, "file not found")
        ext = os.path.splitext(fp)[1].lower()
        ctype = AUDIO_EXTS.get(ext) or mimetypes.guess_type(fp)[0] or "application/octet-stream"
        size = os.path.getsize(fp)

        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                s, e = m.groups()
                if s == "" and e:          # suffix range: last N bytes
                    start = max(0, size - int(e))
                else:
                    start = int(s or 0)
                    if e:
                        end = min(int(e), size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return

        with open(fp, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # browser seeked / closed - normal
                remaining -= len(chunk)


def main():
    ap = argparse.ArgumentParser(description="Stream music & audiobooks to your browser.")
    ap.add_argument("--root", action="append",
                    help="Folder you're allowed to browse (repeatable). Default: your home folder.")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0 = all interfaces)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--password", default=os.environ.get("AUDIOSTREAM_PASSWORD"),
                    help="Require this password (any username). Or set AUDIOSTREAM_PASSWORD.")
    ap.add_argument("--data", default=os.path.join(HERE, "progress.json"),
                    help="Where to store playback positions (default: progress.json next to server.py)")
    ap.add_argument("--show-hidden", action="store_true", help="Show dot-files/folders")
    args = ap.parse_args()

    roots = args.root or [os.path.expanduser("~")]
    roots = [os.path.realpath(os.path.expanduser(r)) for r in roots]
    for r in roots:
        if not os.path.isdir(r):
            sys.exit(f"Root is not a directory: {r}")

    Handler.roots = roots
    Handler.store = ProgressStore(args.data)
    Handler.password = args.password
    Handler.show_hidden = args.show_hidden

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"AudioStream running on http://{args.host}:{args.port}")
    print("Browsing roots:", *roots, sep="\n  ")
    print("Password protection:", "ON" if args.password else "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
