"""
Commercial breaks for AudioStream.

The ads folder can hold:
  * single commercials (one ad per file), and/or
  * long compilation files with many ads in a row.

Compilations are split at their silent gaps with ffmpeg's silencedetect.
The results are cached in ad_cues.json (next to progress.json), so each file
is only scanned once. You can hand-edit ad_cues.json to fix a bad split.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading

# Files shorter than this are treated as a single ad and not scanned
SINGLE_AD_MAX_SECONDS = 120
# Silence needed to count as a gap between ads
SILENCE_DB = "-35dB"
SILENCE_MIN_SECONDS = 0.6
# Ignore "ads" shorter than this (e.g. blips between two gaps)
MIN_AD_SECONDS = 4


def _probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:
        return None


def detect_segments(path, duration=None):
    """Return [[start, end], ...] for each ad in a compilation file."""
    if duration is None:
        duration = _probe_duration(path)
    if not duration:
        return None
    if duration <= SINGLE_AD_MAX_SECONDS:
        return [[0.0, round(duration, 2)]]

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-af", f"silencedetect=noise={SILENCE_DB}:d={SILENCE_MIN_SECONDS}",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800)
    log = proc.stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]

    segments, cursor = [], 0.0
    for s, e in zip(starts, ends):
        s = max(0.0, s)
        if s - cursor >= MIN_AD_SECONDS:
            segments.append([round(cursor, 2), round(s, 2)])
        cursor = e
    if duration - cursor >= MIN_AD_SECONDS:
        segments.append([round(cursor, 2), round(duration, 2)])
    return segments or [[0.0, round(duration, 2)]]


class AdLibrary:
    def __init__(self, ads_dir, cues_path, audio_exts):
        self.dir = os.path.realpath(ads_dir) if ads_dir else None
        self.cues_path = cues_path
        self.exts = audio_exts
        self.lock = threading.Lock()
        self.cues = {}
        self.scanning = False
        if cues_path and os.path.exists(cues_path):
            try:
                with open(cues_path, "r", encoding="utf-8") as f:
                    self.cues = json.load(f)
            except Exception as e:
                print(f"Warning: could not read {cues_path}: {e}", file=sys.stderr)

    @property
    def enabled(self):
        return bool(self.dir and os.path.isdir(self.dir))

    def _files(self):
        out = []
        for dirpath, dirnames, filenames in os.walk(self.dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for n in filenames:
                if not n.startswith(".") and os.path.splitext(n)[1].lower() in self.exts:
                    full = os.path.join(dirpath, n)
                    out.append(os.path.relpath(full, self.dir))
        return sorted(out)

    def _key(self, rel):
        st = os.stat(os.path.join(self.dir, rel))
        return f"{st.st_size}:{int(st.st_mtime)}"

    def _save(self):
        tmp = self.cues_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cues, f, indent=1)
        os.replace(tmp, self.cues_path)

    def resolve(self, rel):
        """Absolute path of an ad file, or None if it's outside the ads folder."""
        if not self.enabled or not rel:
            return None
        full = os.path.realpath(os.path.join(self.dir, rel))
        if os.path.commonpath([full, self.dir]) != self.dir or not os.path.isfile(full):
            return None
        return full

    def segments(self):
        """Every playable ad: [{"file": rel, "start": s, "end": e}, ...]"""
        if not self.enabled:
            return []
        out = []
        with self.lock:
            for rel in self._files():
                c = self.cues.get(rel)
                try:
                    current = c and c.get("key") == self._key(rel)
                except OSError:
                    continue
                if current and c.get("segments"):
                    for s, e in c["segments"]:
                        out.append({"file": rel, "start": s, "end": e})
                else:
                    # Not scanned yet: play the whole file as one ad
                    out.append({"file": rel, "start": 0, "end": None})
        return out

    def scan(self, force=False, verbose=False):
        """Split any new or changed files. Needs ffmpeg + ffprobe."""
        if not self.enabled:
            return
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            print("Ads: ffmpeg/ffprobe not found - each ad file plays whole. "
                  "Install with: sudo apt install ffmpeg", file=sys.stderr)
            return
        self.scanning = True
        try:
            for rel in self._files():
                try:
                    key = self._key(rel)
                except OSError:
                    continue
                with self.lock:
                    c = self.cues.get(rel)
                    if not force and c and c.get("key") == key:
                        continue
                if verbose:
                    print(f"Ads: scanning {rel} ...", file=sys.stderr)
                segs = detect_segments(os.path.join(self.dir, rel))
                if segs is None:
                    continue
                with self.lock:
                    self.cues[rel] = {"key": key, "segments": segs}
                    self._save()
                if verbose:
                    print(f"Ads:   {len(segs)} ad(s) found", file=sys.stderr)
            # forget files that were removed
            with self.lock:
                present = set(self._files())
                gone = [k for k in self.cues if k not in present]
                for k in gone:
                    del self.cues[k]
                if gone:
                    self._save()
        finally:
            self.scanning = False

    def scan_in_background(self):
        threading.Thread(target=self.scan, kwargs={"verbose": True}, daemon=True).start()
