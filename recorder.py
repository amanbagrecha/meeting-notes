"""System-audio + microphone recorder on PipeWire, driven by ffmpeg.

Every call is a fresh process (fused-render runs `main` in a subprocess and
kills it at 60 s), so the capture itself is a *detached* ffmpeg that outlives
the call. State lives in `.fused/data/meetings/<id>/rec.json`; the raw audio
is appended to `audio.pcm` (16 kHz, mono, s16le) so a growing file can be
sliced at any time without caring about a WAV header, and `stop` wraps it
into `audio.wav` for the final transcription.
"""

import json
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

RATE = 16000
BYTES_PER_SEC = RATE * 2  # mono s16le

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


def _capabilities():
    missing = [t for t in ("ffmpeg", "pactl") if not shutil.which(t)]
    if missing:
        return {"available": False, "reason": f"needs {', '.join(missing)} on PATH (PipeWire/PulseAudio + ffmpeg)"}
    sink = _pactl("get-default-sink")
    if not sink:
        return {"available": False, "reason": "no default audio output found (is PipeWire running?)"}
    return {"available": True, "reason": None, "sink": sink, "monitor": sink + ".monitor",
            "mic": _pactl("get-default-source") or None}


def _devices():
    rows = []
    for line in _pactl("list", "short", "sources").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append({"name": parts[1], "monitor": parts[1].endswith(".monitor")})
    return rows


def _status(meeting_id):
    folder = _folder(meeting_id)
    rec = _read_json(os.path.join(folder, "rec.json"), {})
    pcm = os.path.join(folder, "audio.pcm")
    wav = os.path.join(folder, "audio.wav")
    size = os.path.getsize(pcm) if os.path.exists(pcm) else 0
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
    return {
        "meeting_id": meeting_id,
        "running": running,
        "seconds": size / BYTES_PER_SEC,
        "bytes": size,
        "mic": rec.get("mic", False),
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
    pcm = os.path.join(folder, "audio.pcm")
    use_mic = bool(mic and caps.get("mic"))
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
           "-f", "pulse", "-i", caps["monitor"]]
    if use_mic:
        cmd += ["-f", "pulse", "-i", caps["mic"],
                "-filter_complex",
                f"[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0,aresample={RATE}[a]",
                "-map", "[a]"]
    cmd += ["-ac", "1", "-ar", str(RATE), "-f", "s16le", "-c:a", "pcm_s16le", "-y", pcm]
    log = open(os.path.join(folder, "ffmpeg.log"), "ab")
    proc = subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=log, close_fds=True)
    rec = {"pid": proc.pid, "state": "recording", "started_at": time.time(),
           "mic": use_mic, "monitor": caps["monitor"], "mic_device": caps.get("mic") if use_mic else None}
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
    pcm = os.path.join(folder, "audio.pcm")
    wav = os.path.join(folder, "audio.wav")
    if os.path.exists(pcm) and os.path.getsize(pcm) > 0:
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", pcm,
                        "-c:a", "pcm_s16le", "-y", wav], check=True, timeout=50)
    return _status(meeting_id)


def _slice(meeting_id, frm, to):
    folder = _folder(meeting_id)
    pcm = os.path.join(folder, "audio.pcm")
    size = os.path.getsize(pcm) if os.path.exists(pcm) else 0
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
        with open(pcm, "rb") as f:
            f.seek(start_b)
            data = f.read(n)
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
         frm: float = 0.0, to: float = 0.0):
    if action == "capabilities":
        caps = _capabilities()
        caps["devices"] = _devices() if caps["available"] else []
        cur = _read_json(CURRENT, {})
        caps["current"] = cur.get("meeting_id") if cur.get("meeting_id") and _alive(cur.get("pid")) else None
        return caps
    if action == "status":
        return _status(meeting_id)
    if action == "start":
        return _start(meeting_id, mic)
    if action == "stop":
        return _stop(meeting_id)
    if action == "slice":
        return _slice(meeting_id, float(frm), float(to))
    raise ValueError(f"unknown action {action!r}")
