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
from http.cookies import SimpleCookie
from ads import AdLibrary
from urllib.parse import parse_qs, quote, unquote, urlparse

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

IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".gif": "image/gif"}
# Preferred cover file names, best first (matched case-insensitively, any image extension)
COVER_NAMES = ["cover", "folder", "front", "album", "albumart", "albumartlarge", "poster", "art"]

HERE = os.path.dirname(os.path.abspath(__file__))
_cover_cache = {}   # dir -> (mtime, image path or None)


def find_cover(dirpath):
    """Pick the cover image in a folder: a well-known name first, else the largest image."""
    try:
        mtime = os.stat(dirpath).st_mtime
    except OSError:
        return None
    hit = _cover_cache.get(dirpath)
    if hit and hit[0] == mtime:
        return hit[1]
    images = []
    try:
        for e in os.scandir(dirpath):
            if e.name.startswith(".") or not e.is_file():
                continue
            stem, ext = os.path.splitext(e.name)
            if ext.lower() in IMAGE_EXTS:
                images.append((stem.lower(), e.path, e.stat().st_size))
    except OSError:
        images = []
    best = None
    for name in COVER_NAMES:
        best = next((p for stem, p, _ in images if stem == name), None)
        if best:
            break
    if not best and images:
        best = max(images, key=lambda i: i[2])[1]   # largest picture is usually the cover
    _cover_cache[dirpath] = (mtime, best)
    return best
STATIC_DIR = os.path.join(HERE, "static")
CHUNK = 256 * 1024


def natural_key(s):
    """Sort 'Chapter 2' before 'Chapter 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


USER_RE = re.compile(r"^[\w .\-']{1,32}$")
COOKIE_NAME = "audiostream_user"


class ProgressStore:
    """Remembers playback position per user, per file, in a JSON file.

    Layout: {"users": {"Alice": {"files": {path: {...}}, "last": path}, ...}}
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"users": {}}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if "users" in raw:
                    self.data = raw
                elif raw.get("files"):
                    # Migrate the old single-user format into a "Default" profile
                    self.data["users"]["Default"] = {"files": raw["files"], "last": raw.get("last")}
            except Exception as e:
                print(f"Warning: could not read {path}: {e}", file=sys.stderr)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def _u(self, user):
        return self.data["users"].setdefault(user, {"files": {}, "last": None})

    def users(self):
        with self.lock:
            return sorted(self.data["users"], key=str.lower)

    def add_user(self, user):
        with self.lock:
            if user not in self.data["users"]:
                self._u(user)
                self._save()

    def remove_user(self, user):
        with self.lock:
            if self.data["users"].pop(user, None) is not None:
                self._save()

    def get(self, user, fpath):
        with self.lock:
            return self._u(user)["files"].get(fpath)

    def set(self, user, fpath, position, duration):
        with self.lock:
            u = self._u(user)
            finished = bool(duration) and position >= duration - 5
            u["files"][fpath] = {
                "position": round(float(position), 1),
                "duration": round(float(duration or 0), 1),
                "finished": finished,
                "updated": time.time(),
            }
            u["last"] = fpath
            self._save()
            return u["files"][fpath]

    def clear(self, user, fpath):
        with self.lock:
            u = self._u(user)
            u["files"].pop(fpath, None)
            if u.get("last") == fpath:
                u["last"] = None
            self._save()

    DEFAULT_SETTINGS = {"ads_enabled": False, "ads_every_min": 30, "ads_per_break": 2}

    def get_settings(self, user):
        with self.lock:
            return {**self.DEFAULT_SETTINGS, **self._u(user).get("settings", {})}

    def set_settings(self, user, new):
        with self.lock:
            cur = {**self.DEFAULT_SETTINGS, **self._u(user).get("settings", {})}
            if "ads_enabled" in new:
                cur["ads_enabled"] = bool(new["ads_enabled"])
            if "ads_every_min" in new:
                cur["ads_every_min"] = max(1, min(600, int(new["ads_every_min"])))
            if "ads_per_break" in new:
                cur["ads_per_break"] = max(1, min(10, int(new["ads_per_break"])))
            self._u(user)["settings"] = cur
            self._save()
            return cur

    def snapshot(self, user):
        with self.lock:
            u = self.data["users"].get(user) or {"files": {}, "last": None}
            return dict(u["files"]), u.get("last")


class Handler(BaseHTTPRequestHandler):
    server_version = "AudioStream/1.0"
    # Give up on a connection that sends or accepts nothing for this long.
    # Without it, a phone that vanishes mid-stream can tie up a thread forever.
    timeout = 120
    roots = []
    store = None
    password = None
    show_hidden = False
    ads = None

    # ---------- helpers ----------
    def log_message(self, fmt, *args):
        if "/api/progress" in (getattr(self, "path", None) or ""):
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

    def current_user(self):
        """Profile name from the cookie, or None if not picked / unknown."""
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            if COOKIE_NAME in c:
                name = unquote(c[COOKIE_NAME].value)
                if name in self.store.users():
                    return name
        except Exception:
            pass
        return None

    def require_user(self):
        u = self.current_user()
        if not u:
            self.send_error_json(401, "no user selected")
        return u

    def send_json_cookie(self, obj, name):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if name is None:
            self.send_header("Set-Cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax")
        else:
            self.send_header("Set-Cookie",
                             f"{COOKIE_NAME}={quote(name)}; Path=/; Max-Age=315360000; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

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
            if route == "/api/users":
                return self.send_json({"users": self.store.users(), "current": self.current_user()})
            if route == "/api/roots":
                return self.api_roots()
            if route == "/api/list":
                return self.api_list()
            if route == "/api/progress":
                return self.api_get_progress()
            if route == "/api/recent":
                return self.api_recent()
            if route == "/cover":
                return self.serve_cover()
            if route == "/stream":
                return self.stream()
            if route == "/adstream":
                return self.stream(ad=True)
            if route == "/api/ads":
                return self.send_json({"enabled": self.ads.enabled, "scanning": self.ads.scanning,
                                       "segments": self.ads.segments()})
            if route == "/api/settings":
                user = self.require_user()
                if user:
                    self.send_json(self.store.get_settings(user))
                return
            self.send_error_json(404, "not found")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_HEAD(self):
        if not self.check_auth():
            return
        route = urlparse(self.path).path
        if route in ("/stream", "/adstream"):
            return self.stream(head_only=True, ad=(route == "/adstream"))
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
        if route == "/api/login":
            name = (payload.get("name") or "").strip()
            if not USER_RE.match(name):
                return self.send_error_json(400, "Name must be 1-32 letters, numbers, spaces, . - ' or _")
            self.store.add_user(name)
            return self.send_json_cookie({"ok": True, "current": name}, name)
        if route == "/api/logout":
            return self.send_json_cookie({"ok": True}, None)
        if route == "/api/users/delete":
            name = payload.get("name")
            self.store.remove_user(name)
            return self.send_json_cookie({"ok": True}, None) if name == self.current_user() \
                else self.send_json({"ok": True})
        user = self.require_user()
        if not user:
            return
        if route == "/api/progress":
            return self.api_set_progress(user, payload)
        if route == "/api/settings":
            try:
                return self.send_json(self.store.set_settings(user, payload))
            except (TypeError, ValueError):
                return self.send_error_json(400, "bad settings")
        if route == "/api/progress/clear":
            fp = self.safe_path(payload.get("path"))
            if not fp:
                return self.send_error_json(403, "path not allowed")
            self.store.clear(user, fp)
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

    def serve_cover(self):
        """GET /cover?path=<folder or audio file> -> that folder's cover image."""
        p = self.safe_path(self.qs().get("path"))
        if p and os.path.isfile(p):
            p = os.path.dirname(p)
        img = find_cover(p) if p and os.path.isdir(p) else None
        if not img:
            return self.send_error_json(404, "no cover")
        with open(img, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", IMAGE_EXTS[os.path.splitext(img)[1].lower()])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=600")
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

        files_progress, last = self.store.snapshot(self.current_user())
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
                    dirs.append({"name": e.name, "path": full, "has_progress": has_prog,
                                 "cover": bool(find_cover(full))})
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
            "cover": bool(find_cover(path)),
        })

    def api_get_progress(self):
        user = self.require_user()
        if not user:
            return
        fp = self.safe_path(self.qs().get("path"))
        if not fp:
            return self.send_error_json(403, "path not allowed")
        self.send_json({"path": fp, "progress": self.store.get(user, fp)})

    def api_set_progress(self, user, payload):
        fp = self.safe_path(payload.get("path"))
        if not fp or not os.path.isfile(fp):
            return self.send_error_json(403, "path not allowed")
        try:
            pos = float(payload.get("position", 0))
            dur = float(payload.get("duration", 0) or 0)
        except (TypeError, ValueError):
            return self.send_error_json(400, "bad numbers")
        self.send_json({"ok": True, "progress": self.store.set(user, fp, pos, dur)})

    def api_recent(self):
        user = self.require_user()
        if not user:
            return
        files_progress, last = self.store.snapshot(user)
        items = []
        for fp, p in files_progress.items():
            if os.path.isfile(fp) and self.safe_path(fp):
                items.append({
                    "path": fp,
                    "name": os.path.basename(fp),
                    "folder": os.path.dirname(fp),
                    "cover": bool(find_cover(os.path.dirname(fp))),
                    "progress": p,
                })
        items.sort(key=lambda i: i["progress"]["updated"], reverse=True)
        self.send_json({"last": last, "items": items[:20]})

    # ---------- streaming with HTTP Range ----------
    def stream(self, head_only=False, ad=False):
        if ad:
            fp = self.ads.resolve(self.qs().get("f"))
        else:
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
                except TimeoutError:
                    # Client stopped reading (asleep / gone). Drop it; the
                    # browser re-requests the rest with a Range header if needed.
                    self.close_connection = True
                    return
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
    ap.add_argument("--ads", default=os.environ.get("AUDIOSTREAM_ADS"),
                    help="Folder of commercials for commercial breaks. Or set AUDIOSTREAM_ADS.")
    ap.add_argument("--scan-ads", action="store_true",
                    help="Re-split every file in the ads folder now, print the results and exit")
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
    cues_path = os.path.join(os.path.dirname(os.path.abspath(args.data)), "ad_cues.json")
    Handler.ads = AdLibrary(args.ads, cues_path, AUDIO_EXTS)
    if args.ads and not Handler.ads.enabled:
        print(f"Warning: ads folder not found: {args.ads}", file=sys.stderr)
    if args.scan_ads:
        Handler.ads.scan(force=True, verbose=True)
        for seg in Handler.ads.segments():
            print(f"  {seg['file']}: {seg['start']:.1f}s - {seg['end']}s")
        return
    if Handler.ads.enabled:
        Handler.ads.scan_in_background()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"AudioStream running on http://{args.host}:{args.port}")
    print("Browsing roots:", *roots, sep="\n  ")
    print("Password protection:", "ON" if args.password else "off")
    print("Commercials folder:", Handler.ads.dir if Handler.ads.enabled else "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
