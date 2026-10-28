"""System-audio + microphone recorder on PipeWire, driven by ffmpeg.

Every call is a fresh process (fused-render runs `main` in a subprocess and
kills it at 60 s), so the capture itself is a *detached* ffmpeg that outlives
the call. State lives in `.fused/data/meetings/<id>/rec.json`; the raw audio
is appended to `audio.pcm` (16 kHz, mono, s16le) so a growing file can be
sliced at any time without caring about a WAV header, and `stop` wraps it
into `audio.wav` for the final transcription.
"""

import array
import json
import math
import os
import shutil
import signal
import subprocess
import time
import re

APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(APP, ".fused", "data")
CACHE = os.path.join(APP, ".fused", "cache")
MEETINGS = os.path.join(DATA, "meetings")
CURRENT = os.path.join(DATA, "recording.json")
SETTINGS = os.path.join(DATA, "settings.json")

RATE = 16000
BYTES_PER_SEC = RATE * 2  # mono s16le
WINDOW = RATE // 20 * 2   # 50 ms of s16le: one level-meter bar
FLOOR_DB = -90.0

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _folder(meeting_id: str) -> str:
    if not _ID.match(meeting_id or ""):
        raise ValueError("meeting_id must be 1-64 chars of [A-Za-z0-9_-]")
    return os.path.join(MEETINGS, meeting_id)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); check /proc state.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def _pactl(*args):
    try:
        out = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _sources():
    """All PulseAudio/PipeWire sources with their human-readable names."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=5)
        rows = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        rows = None
    if rows is None:  # older pactl without -f json
        rows = [{"name": l.split("\t")[1]} for l in _pactl("list", "short", "sources").splitlines() if "\t" in l]
    devices = []
    for r in rows:
        name = r.get("name") or ""
        monitor = name.endswith(".monitor")
        label = r.get("description") or name
        if monitor and label.startswith("Monitor of "):
            label = label[len("Monitor of "):]
        devices.append({"name": name, "label": label, "monitor": monitor})
    return devices


def _settings():
    s = _read_json(SETTINGS, {})
    return {"system": s.get("system") or "", "mic": s.get("mic") or ""}


def _capabilities():
    """Resolve the devices a recording would use.

    settings.system: "" = monitor of the default output, else a source name.
    settings.mic: "" = default input, "off" = no mic, else a source name.
    A saved device that has gone away (headset unplugged) falls back to the
    default rather than failing the recording.
    """
    missing = [t for t in ("ffmpeg", "pactl") if not shutil.which(t)]
    if missing:
        return {"available": False, "reason": f"needs {', '.join(missing)} on PATH (PipeWire/PulseAudio + ffmpeg)"}
    sink = _pactl("get-default-sink")
    if not sink:
        return {"available": False, "reason": "no default audio output found (is PipeWire running?)"}
    devices = _sources()
    names = {d["name"] for d in devices}
    labels = {d["name"]: d["label"] for d in devices}
    st = _settings()
    monitor = st["system"] if st["system"] in names else sink + ".monitor"
    default_mic = _pactl("get-default-source") or None
    if default_mic and default_mic.endswith(".monitor"):
        default_mic = None  # default input is itself a monitor: no real mic
    if st["mic"] == "off":
        mic = None
    else:
        mic = st["mic"] if st["mic"] in names else default_mic
    return {"available": True, "reason": None, "sink": sink, "monitor": monitor,
            "monitor_label": labels.get(monitor, monitor), "mic": mic,
            "mic_label": labels.get(mic, mic) if mic else None,
            "default_monitor": sink + ".monitor", "default_mic": default_mic,
            "settings": st, "devices": devices}


def _levels(data):
    """dBFS of each 50 ms window of s16le `data`."""
    out = []
    for i in range(0, len(data) - WINDOW + 1, WINDOW):
        a = array.array("h", data[i:i + WINDOW])
        rms = math.sqrt(sum(x * x for x in a) / len(a)) if len(a) else 0
        out.append(round(max(FLOOR_DB, 20 * math.log10(rms / 32768)) if rms > 0 else FLOOR_DB, 1))
    return out


def _meter(devices, secs):
    """Capture `secs` from each device at once and return its levels."""
    secs = max(0.2, min(float(secs), 2.0))
    procs = {}
    for key, dev in devices.items():
        if not dev:
            continue
        procs[key] = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", dev,
             "-t", str(secs), "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = {}
    for key, p in procs.items():
        try:
            data, err = p.communicate(timeout=secs + 8)
        except subprocess.TimeoutExpired:
            p.kill()
            data, err = p.communicate()
        out[key] = {"device": devices[key], "levels": _levels(data[-int(secs * BYTES_PER_SEC):] if data else b""),
                    "error": (err or b"").decode(errors="replace").strip()[-200:] or None if p.returncode else None}
    return out


def _size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def _status(meeting_id):
    folder = _folder(meeting_id)
    rec = _read_json(os.path.join(folder, "rec.json"), {})
    pcm = os.path.join(folder, "audio.pcm")
    wav = os.path.join(folder, "audio.wav")
    # Earlier takes live in audio.pcm; the take in progress in audio.part.pcm.
    size = _size(pcm) + _size(os.path.join(folder, "audio.part.pcm"))
    running = bool(rec) and rec.get("state") == "recording" and _alive(rec.get("pid"))
    err = None
    if rec and rec.get("state") == "recording" and not running:
        # ffmpeg died on its own: surface its last stderr lines.
        try:
            with open(os.path.join(folder, "ffmpeg.log")) as f:
                tail = f.read().strip().splitlines()[-3:]
            err = " ".join(tail) or "recorder exited unexpectedly"
        except OSError:
            err = "recorder exited unexpectedly"
    levels = []
    if running:
        # The last second of the take in progress, for the page's level meter.
        part = os.path.join(folder, "audio.part.pcm")
        n = _size(part)
        span = min(n, BYTES_PER_SEC) // WINDOW * WINDOW
        if span:
            with open(part, "rb") as f:
                f.seek((n - span) & ~1)
                levels = _levels(f.read(span))
    return {
        "meeting_id": meeting_id,
        "running": running,
        "levels": levels,
        "seconds": size / BYTES_PER_SEC,
        "bytes": size,
        "mic": rec.get("mic", False),
        "monitor": rec.get("monitor"),
        "mic_device": rec.get("mic_device"),
        "started_at": rec.get("started_at"),
        "wav": wav if os.path.exists(wav) else None,
        "error": err,
    }


def _start(meeting_id, mic):
    caps = _capabilities()
    if not caps["available"]:
        raise RuntimeError(caps["reason"])
    current = _read_json(CURRENT, {})
    if current.get("meeting_id") and current["meeting_id"] != meeting_id:
        other = _status(current["meeting_id"])
        if other["running"]:
            raise RuntimeError(f"already recording meeting {current['meeting_id']}; stop it first")
    folder = _folder(meeting_id)
    os.makedirs(folder, exist_ok=True)
    st = _status(meeting_id)
    if st["running"]:
        return st
    # Each take records into its own part file and is appended to audio.pcm at
    # stop, so "Record more" on a finished meeting extends it instead of
    # overwriting it (ffmpeg cannot append).
    part = os.path.join(folder, "audio.part.pcm")
    _absorb_part(folder)
    use_mic = bool(mic and caps.get("mic"))
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
           "-f", "pulse", "-i", caps["monitor"]]
    if use_mic:
        cmd += ["-f", "pulse", "-i", caps["mic"],
                "-filter_complex",
                f"[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0,aresample={RATE}[a]",
                "-map", "[a]"]
    # -flush_packets 1: write every packet straight to disk so status/slice see
    # audio within ~100 ms instead of in 256 KiB (8 s) bursts.
    cmd += ["-ac", "1", "-ar", str(RATE), "-f", "s16le", "-c:a", "pcm_s16le",
            "-flush_packets", "1", "-y", part]
    log = open(os.path.join(folder, "ffmpeg.log"), "ab")
    proc = subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=log, close_fds=True)
    rec = {"pid": proc.pid, "state": "recording", "started_at": time.time(),
           "mic": use_mic, "monitor": caps["monitor"], "mic_device": caps["mic"] if use_mic else None}
    _write_json(os.path.join(folder, "rec.json"), rec)
    _write_json(CURRENT, {"meeting_id": meeting_id, "pid": proc.pid})
    # Give ffmpeg a moment to fail fast on a bad device.
    time.sleep(0.6)
    st = _status(meeting_id)
    if not st["running"]:
        rec["state"] = "failed"
        _write_json(os.path.join(folder, "rec.json"), rec)
        raise RuntimeError(st["error"] or "ffmpeg failed to start")
    return st


def _absorb_part(folder):
    """Append a finished take (audio.part.pcm) onto audio.pcm."""
    part = os.path.join(folder, "audio.part.pcm")
    if not os.path.exists(part):
        return
    pcm = os.path.join(folder, "audio.pcm")
    with open(part, "rb") as src, open(pcm, "ab") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(part)


def _stop(meeting_id):
    folder = _folder(meeting_id)
    rec_path = os.path.join(folder, "rec.json")
    rec = _read_json(rec_path, {})
    pid = rec.get("pid")
    if pid and _alive(pid):
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    rec["state"] = "stopped"
    rec["stopped_at"] = time.time()
    _write_json(rec_path, rec)
    cur = _read_json(CURRENT, {})
    if cur.get("meeting_id") == meeting_id:
        _write_json(CURRENT, {})
    _absorb_part(folder)
    pcm = os.path.join(folder, "audio.pcm")
    wav = os.path.join(folder, "audio.wav")
    if os.path.exists(pcm) and os.path.getsize(pcm) > 0:
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", pcm,
                        "-c:a", "pcm_s16le", "-y", wav], check=True, timeout=50)
    return _status(meeting_id)


def _read_span(paths, start, n):
    """Read `n` bytes at `start` from the virtual concatenation of `paths`."""
    out = bytearray()
    pos = 0
    for p in paths:
        size = _size(p)
        if size == 0:
            continue
        lo, hi = max(start, pos), min(start + n, pos + size)
        if hi > lo:
            with open(p, "rb") as f:
                f.seek(lo - pos)
                out += f.read(hi - lo)
        pos += size
        if pos >= start + n:
            break
    return bytes(out)


def _slice(meeting_id, frm, to):
    folder = _folder(meeting_id)
    pcm = os.path.join(folder, "audio.pcm")
    part = os.path.join(folder, "audio.part.pcm")
    size = _size(pcm) + _size(part)
    avail = size / BYTES_PER_SEC
    to = min(to, avail) if to > 0 else avail
    frm = max(0.0, min(frm, to))
    if to - frm < 0.5:
        return {"path": None, "from": frm, "to": to, "seconds": 0}
    out_dir = os.path.join(CACHE, "slices")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{meeting_id}-{int(frm * 1000)}-{int(to * 1000)}.wav")
    if not os.path.exists(out):
        tmp = out + ".tmp.wav"
        start_b = int(frm * BYTES_PER_SEC) & ~1
        n = int((to - frm) * BYTES_PER_SEC) & ~1
        data = _read_span([pcm, part], start_b, n)
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
                        "-c:a", "pcm_s16le", "-y", tmp], input=data, check=True, timeout=30)
        os.replace(tmp, out)
    # Keep the slice cache small: drop slices older than an hour.
    now = time.time()
    for name in os.listdir(out_dir):
        p = os.path.join(out_dir, name)
        try:
            if now - os.path.getmtime(p) > 3600:
                os.remove(p)
        except OSError:
            pass
    return {"path": out, "from": frm, "to": to, "seconds": to - frm}


def main(action: str = "status", meeting_id: str = "", mic: bool = True,
         frm: float = 0.0, to: float = 0.0, system: str = "", mic_device: str = "",
         secs: float = 0.5):
    if action == "capabilities":
        caps = _capabilities()
        cur = _read_json(CURRENT, {})
        caps["current"] = cur.get("meeting_id") if cur.get("meeting_id") and _alive(cur.get("pid")) else None
        return caps
    if action == "settings":
        # system / mic_device: "" = follow the system default; mic_device "off" = no mic.
        _write_json(SETTINGS, {"system": system.strip(), "mic": mic_device.strip()})
        return main("capabilities")
    if action == "meter":
        caps = _capabilities()
        if not caps["available"]:
            raise RuntimeError(caps["reason"])
        return _meter({"system": caps["monitor"], "mic": caps["mic"]}, secs)
    if action == "status":
        return _status(meeting_id)
    if action == "start":
        return _start(meeting_id, mic)
    if action == "stop":
        return _stop(meeting_id)
    if action == "slice":
        return _slice(meeting_id, float(frm), float(to))
    raise ValueError(f"unknown action {action!r}")
