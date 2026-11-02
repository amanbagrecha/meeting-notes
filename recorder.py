"""System-audio + microphone recorder: PipeWire + ffmpeg on Linux, the page's
`fused.capture` on macOS.

Every call is a fresh process (fused-render runs `main` in a subprocess and
kills it at 60 s), so the capture itself is a *detached* ffmpeg that outlives
the call. State lives in `.fused/data/meetings/<id>/rec.json`; the raw audio
is appended to `audio.pcm` (16 kHz, s16le, interleaved) so a growing file can
be sliced at any time without caring about a WAV header. A meeting is stereo
(L = microphone, R = system audio) or, when recorded without a mic or before
stereo existed, mono; `rec.json` keeps that as `channels`, fixed by the first
take. `stop` wraps the audio into `audio.wav` (mono mix for playback and
transcription) plus, for stereo, `audio.mic.wav` and `audio.system.wav`.

macOS ("capture" backend): there is no PipeWire monitor, so the PAGE records
each take with `fused.capture.audio` -- one recording of the system output
(`take.system.m4a`) and one of the default microphone (`take.mic.m4a`) -- and
`stop` decodes both with the built-in `afconvert` into the same `audio.pcm`
layout. An .m4a cannot be read until it is finalised, so there is no live
slice on macOS: the transcript is written at stop.
"""

import array
import json
import sys
import warnings
import math
import operator
import os
import shutil
import signal
import subprocess
import re
import time
import wave

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        import audioop  # stdlib through 3.12; C-speed mixing of PCM
    except ImportError:  # 3.13+
        audioop = None

MAC = sys.platform == "darwin"
APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(APP, ".fused", "data")
CACHE = os.path.join(APP, ".fused", "cache")
MEETINGS = os.path.join(DATA, "meetings")
CURRENT = os.path.join(DATA, "recording.json")
SETTINGS = os.path.join(DATA, "settings.json")

RATE = 16000
WIN = RATE // 20  # frames in 50 ms: one level-meter bar
FLOOR_DB = -90.0
CHANNELS = ("mix", "mic", "system")
# Mono downmix of an interleaved L = mic, R = system stream.
FILTERS = {"mix": "pan=mono|c0=c0+c1,alimiter=limit=0.95:level=0",
           "mic": "pan=mono|c0=c0", "system": "pan=mono|c0=c1"}

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
    # A zombie still answers kill(0); check /proc state (Linux only).
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return not os.path.isdir("/proc")
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
    if MAC:
        # The page records through fused.capture and refines this with
        # fused.capture.sources() (permissions, the default mic's name).
        if not shutil.which("afconvert"):
            return {"available": False, "reason": "needs afconvert (ships with macOS) on PATH"}
        st = _settings()
        return {"available": True, "reason": None, "backend": "capture",
                "monitor_label": "Mac audio", "mic": None if st["mic"] == "off" else "default",
                "mic_label": "default input", "settings": st, "devices": []}
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


def _samples(data, channels=1):
    """Whole frames of interleaved s16le `data` as an array of samples."""
    a = array.array("h")
    frame = 2 * channels
    a.frombytes(data[:len(data) // frame * frame])
    return a


def _db(a):
    rms = math.sqrt(sum(map(operator.mul, a, a)) / len(a)) if len(a) else 0
    return round(max(FLOOR_DB, 20 * math.log10(rms / 32768)) if rms > 0 else FLOOR_DB, 1)


def _window_levels(a):
    """dBFS of each 50 ms window of mono samples `a`."""
    return [_db(a[i:i + WIN]) for i in range(0, len(a) - WIN + 1, WIN)]


def _channel_levels(data, channels=1):
    """Per-channel dBFS of each 50 ms window of interleaved s16le `data`."""
    a = _samples(data, channels)
    return [_window_levels(a[c::channels]) for c in range(channels)]


def _levels(data, channels=1):
    """dBFS of each 50 ms window of s16le `data`: the loudest channel."""
    return [max(w) for w in zip(*_channel_levels(data, channels))]


def _mono(a, channels, channel):
    """One channel of interleaved samples `a`; "mix" is the clipped sum."""
    if channels == 1:
        return a
    if channel == "mic":
        return a[0::2]
    if channel == "system":
        return a[1::2]
    return array.array("h", (max(-32768, min(32767, l + r)) for l, r in zip(a[0::2], a[1::2])))


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
        out[key] = {"device": devices[key], "levels": _levels(data[-int(secs * RATE * 2):] if data else b""),
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
    channels = rec.get("channels", 1)
    frame = 2 * channels
    bps = RATE * frame
    capture = rec.get("backend") == "capture"
    if capture:
        # The page owns the fused.capture recordings; it decides liveness.
        running = rec.get("state") == "recording"
        if running:
            size += int(max(0.0, time.time() - rec.get("started_at", time.time())) * RATE) * frame
    else:
        running = bool(rec) and rec.get("state") == "recording" and _alive(rec.get("pid"))
    err = None
    if rec and rec.get("state") == "recording" and not running and not capture:
        # ffmpeg died on its own: surface its last stderr lines.
        try:
            with open(os.path.join(folder, "ffmpeg.log")) as f:
                tail = f.read().strip().splitlines()[-3:]
            err = " ".join(tail) or "recorder exited unexpectedly"
        except OSError:
            err = "recorder exited unexpectedly"
    levels = []
    per = []
    if running and not capture:
        # The last second of the take in progress, for the page's level meter.
        part = os.path.join(folder, "audio.part.pcm")
        n = _size(part) // frame * frame
        span = min(n, bps) // (WIN * frame) * (WIN * frame)
        if span:
            with open(part, "rb") as f:
                f.seek(n - span)
                data = f.read(span)
            per = _channel_levels(data, channels)
            levels = [max(w) for w in zip(*per)]
    out = {
        "meeting_id": meeting_id,
        "running": running,
        "channels": channels,
        "levels": levels,
        "seconds": size / bps,
        "bytes": size,
        "mic": rec.get("mic", False),
        "monitor": rec.get("monitor"),
        "mic_device": rec.get("mic_device"),
        "started_at": rec.get("started_at"),
        "backend": rec.get("backend", "ffmpeg"),
        "captures": rec.get("captures") or {},
        "wav": wav if os.path.exists(wav) else None,
        "wav_mic": None,
        "wav_system": None,
        "error": err,
    }
    if channels == 2:
        out["levels_mic"], out["levels_system"] = per if per else ([], [])
        for key, name in (("wav_mic", "audio.mic.wav"), ("wav_system", "audio.system.wav")):
            p = os.path.join(folder, name)
            out[key] = p if os.path.exists(p) else None
    return out


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
    # The first take fixes the layout: stereo (L mic, R system) if it has a mic.
    if _size(os.path.join(folder, "audio.pcm")):
        channels = _read_json(os.path.join(folder, "rec.json"), {}).get("channels", 1)
    else:
        channels = 2 if use_mic else 1
    mono = f"aresample={RATE},aformat=channel_layouts=mono"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
           "-f", "pulse", "-i", caps["monitor"]]
    if use_mic and channels == 2:
        cmd += ["-f", "pulse", "-i", caps["mic"], "-filter_complex",
                f"[0:a]{mono}[sys];[1:a]{mono}[mic];"
                "[mic][sys]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[a]",
                "-map", "[a]"]
    elif use_mic:
        cmd += ["-f", "pulse", "-i", caps["mic"],
                "-filter_complex",
                f"[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0,aresample={RATE}[a]",
                "-map", "[a]"]
    elif channels == 2:  # no mic this take: keep the left channel silent so takes line up
        cmd += ["-filter_complex", f"[0:a]{mono},pan=stereo|c0=0*c0|c1=c0[a]", "-map", "[a]"]
    # -flush_packets 1: write every packet straight to disk so status/slice see
    # audio within ~100 ms instead of in 256 KiB (8 s) bursts.
    cmd += ["-ac", str(channels), "-ar", str(RATE), "-f", "s16le", "-c:a", "pcm_s16le",
            "-flush_packets", "1", "-y", part]
    log = open(os.path.join(folder, "ffmpeg.log"), "ab")
    proc = subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=log, close_fds=True)
    rec = {"pid": proc.pid, "state": "recording", "started_at": time.time(),
           "channels": channels, "mic": use_mic, "monitor": caps["monitor"], "mic_device": caps["mic"] if use_mic else None}
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


TAKES = {"system": "take.system.m4a", "mic": "take.mic.m4a"}


def _begin(meeting_id, mic):
    """macOS: open a take the page records with fused.capture; returns its paths."""
    caps = _capabilities()
    if not caps["available"]:
        raise RuntimeError(caps["reason"])
    current = _read_json(CURRENT, {})
    if current.get("meeting_id") and current["meeting_id"] != meeting_id:
        other = _read_json(os.path.join(_folder(current["meeting_id"]), "rec.json"), {})
        if other.get("state") == "recording":
            raise RuntimeError(f"already recording meeting {current['meeting_id']}; stop it first")
    folder = _folder(meeting_id)
    os.makedirs(folder, exist_ok=True)
    rec_path = os.path.join(folder, "rec.json")
    prev = _read_json(rec_path, {})
    _absorb_takes(folder, prev.get("channels", 1))  # a take left by a crash
    use_mic = bool(mic and caps.get("mic"))
    if _size(os.path.join(folder, "audio.pcm")):
        channels = prev.get("channels", 1)
    else:
        channels = 2 if use_mic else 1
    if channels == 1:
        use_mic = False  # a mono meeting stays system-only, as on Linux
    paths = {k: os.path.join(folder, v) for k, v in TAKES.items()}
    rec = {"backend": "capture", "state": "recording", "started_at": time.time(),
           "channels": channels, "mic": use_mic, "monitor": "Mac audio",
           "mic_device": "default input" if use_mic else None, "captures": {}}
    _write_json(rec_path, rec)
    _write_json(CURRENT, {"meeting_id": meeting_id, "backend": "capture"})
    return {"channels": channels, "system": paths["system"], "mic": paths["mic"] if use_mic else None}


def _set_captures(meeting_id, ids):
    """Remember the fused.capture ids of the take so a reloaded page re-attaches."""
    folder = _folder(meeting_id)
    rec_path = os.path.join(folder, "rec.json")
    rec = _read_json(rec_path, {})
    rec["captures"] = {k: v for k, v in json.loads(ids or "{}").items() if isinstance(v, str)}
    if not rec["captures"].get("mic"):
        rec["mic"] = False
        rec["mic_device"] = None
    _write_json(rec_path, rec)
    return _status(meeting_id)


def _decode(path):
    """An .m4a take as 16 kHz mono s16le bytes, via macOS's afconvert."""
    tmp = os.path.join(CACHE, "decode-" + os.path.basename(os.path.dirname(path)) + "-" + os.path.basename(path) + ".wav")
    os.makedirs(CACHE, exist_ok=True)
    try:
        r = subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", path, tmp],
                           capture_output=True, text=True, timeout=50)
        if r.returncode != 0:
            raise RuntimeError(f"could not read {os.path.basename(path)}: {(r.stderr or r.stdout).strip()[-200:]}")
        with wave.open(tmp, "rb") as w:
            return w.readframes(w.getnframes())
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _interleave(left, right):
    """Two mono s16le streams -> one stereo stream, the shorter padded with silence."""
    n = max(len(left), len(right))
    left, right = left.ljust(n, b"\0"), right.ljust(n, b"\0")
    if audioop:
        return audioop.add(audioop.tostereo(left, 2, 1, 0), audioop.tostereo(right, 2, 0, 1), 2)
    out = array.array("h", bytes(2 * n))
    out[0::2], out[1::2] = _samples(left), _samples(right)
    return out.tobytes()


def _absorb_takes(folder, channels):
    """Decode a finished macOS take (take.*.m4a) and append it to audio.pcm."""
    paths = {k: os.path.join(folder, v) for k, v in TAKES.items()}
    if not any(_size(p) for p in paths.values()):
        for p in paths.values():
            if os.path.exists(p):
                os.remove(p)
        return
    data = {k: _decode(p) if _size(p) else b"" for k, p in paths.items()}
    if channels == 2:
        pcm = _interleave(data["mic"], data["system"])
    else:
        pcm = data["system"]
    with open(os.path.join(folder, "audio.pcm"), "ab") as f:
        f.write(pcm)
    for p in paths.values():
        if os.path.exists(p):
            os.remove(p)


def _write_wavs(folder, channels):
    """audio.wav (+ mic/system for stereo) from audio.pcm without ffmpeg."""
    pcm = os.path.join(folder, "audio.pcm")
    names = {"mix": "audio.wav"}
    if channels == 2:
        names.update(mic="audio.mic.wav", system="audio.system.wav")
    tmp = {k: os.path.join(folder, v + ".tmp") for k, v in names.items()}
    writers = {}
    for k, p in tmp.items():
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        writers[k] = w
    try:
        with open(pcm, "rb") as f:
            while True:
                chunk = f.read(RATE * 2 * channels * 30)
                chunk = chunk[:len(chunk) // (2 * channels) * (2 * channels)]
                if not chunk:
                    break
                for k, w in writers.items():
                    w.writeframes(_downmix(chunk, channels, k))
    finally:
        for w in writers.values():
            w.close()
    for k, p in tmp.items():
        os.replace(p, os.path.join(folder, names[k]))


def _downmix(data, channels, channel):
    """One channel of interleaved s16le `data` as mono bytes ("mix" = clipped sum)."""
    if channels == 1:
        return data
    if audioop:
        fl, fr = {"mic": (1, 0), "system": (0, 1), "mix": (1, 1)}[channel]
        return audioop.tomono(data, 2, fl, fr)
    return _mono(_samples(data, 2), 2, channel).tobytes()


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
    if rec.get("backend") == "capture":
        pid = None  # the page has already stopped its fused.capture recordings
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
    _absorb_takes(folder, rec.get("channels", 1))
    pcm = os.path.join(folder, "audio.pcm")
    wav = os.path.join(folder, "audio.wav")
    if _size(pcm) > 0 and not shutil.which("ffmpeg"):
        _write_wavs(folder, rec.get("channels", 1))
    elif _size(pcm) > 0:
        channels = rec.get("channels", 1)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-f", "s16le", "-ar", str(RATE), "-ac", str(channels), "-i", pcm]
        if channels == 2:
            cmd += ["-filter_complex",
                    f"[0:a]asplit=3[a][b][c];[a]{FILTERS['mix']}[mix];"
                    f"[b]{FILTERS['mic']}[mic];[c]{FILTERS['system']}[sys]",
                    "-map", "[mix]", "-c:a", "pcm_s16le", "-y", wav,
                    "-map", "[mic]", "-c:a", "pcm_s16le", "-y", os.path.join(folder, "audio.mic.wav"),
                    "-map", "[sys]", "-c:a", "pcm_s16le", "-y", os.path.join(folder, "audio.system.wav")]
        else:
            cmd += ["-c:a", "pcm_s16le", "-y", wav]
        subprocess.run(cmd, check=True, timeout=50)
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


def _write_wav(path, data):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data)


def _slice(meeting_id, frm, to, channel="mix", snap=0.0):
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
    folder = _folder(meeting_id)
    pcm = os.path.join(folder, "audio.pcm")
    part = os.path.join(folder, "audio.part.pcm")
    channels = _read_json(os.path.join(folder, "rec.json"), {}).get("channels", 1)
    if channels == 1:
        channel = "mix"
    frame = 2 * channels
    avail = (_size(pcm) + _size(part)) // frame / RATE
    to = min(to, avail) if to > 0 else avail
    frm = max(0.0, min(frm, to))
    if to - frm < 0.5:
        return {"path": None, "from": frm, "to": to, "seconds": 0, "channel": channel,
                "peak_db": None, "quiet_db": None}
    quiet = None
    lo = max(to - snap, frm + 0.5) if snap > 0 else to
    if lo < to:
        # Cut at the quietest 50 ms of the mix just before `to`: a pause, not a word.
        first = int(lo * RATE)
        raw = _read_span([pcm, part], first * frame, (int(to * RATE) - first) * frame)
        lv = _window_levels(_mono(_samples(raw, channels), channels, "mix"))
        if lv:
            i = min(reversed(range(len(lv))), key=lv.__getitem__)
            quiet = lv[i]
            to = lo + (i + 0.5) * WIN / RATE
    out_dir = os.path.join(CACHE, "slices")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{meeting_id}-{channel}-{int(frm * 1000)}-{int(to * 1000)}.wav")
    first = int(frm * RATE)
    if os.path.exists(out):
        with wave.open(out, "rb") as w:
            mono = _samples(w.readframes(w.getnframes()))
    else:
        data = _read_span([pcm, part], first * frame, (int(to * RATE) - first) * frame)
        if channels == 2 and shutil.which("ffmpeg"):
            data = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
                 "-af", FILTERS[channel], "-f", "s16le", "-ac", "1", "pipe:1"],
                input=data, check=True, timeout=30, stdout=subprocess.PIPE).stdout
        elif channels == 2:
            data = _downmix(data[:len(data) // 4 * 4], 2, channel)
        mono = _samples(data)
        tmp = out + ".tmp.wav"
        _write_wav(tmp, mono.tobytes())
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
    return {"path": out, "from": frm, "to": to, "seconds": to - frm, "channel": channel,
            "peak_db": max(_window_levels(mono), default=FLOOR_DB), "quiet_db": quiet}


def main(action: str = "status", meeting_id: str = "", mic: bool = True,
         frm: float = 0.0, to: float = 0.0, system: str = "", mic_device: str = "",
         secs: float = 0.5, channel: str = "mix", snap: float = 0.0, ids: str = ""):
    if action == "capabilities":
        caps = _capabilities()
        cur = _read_json(CURRENT, {})
        live = cur.get("backend") == "capture" or _alive(cur.get("pid"))
        caps["current"] = cur.get("meeting_id") if cur.get("meeting_id") and live else None
        return caps
    if action == "settings":
        # system / mic_device: "" = follow the system default; mic_device "off" = no mic.
        _write_json(SETTINGS, {"system": system.strip(), "mic": mic_device.strip()})
        return main("capabilities")
    if action == "begin":
        return _begin(meeting_id, mic)
    if action == "captures":
        return _set_captures(meeting_id, ids)
    if action == "meter":
        caps = _capabilities()
        if caps.get("backend") == "capture":
            return {}
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
        return _slice(meeting_id, float(frm), float(to), channel, float(snap))
    raise ValueError(f"unknown action {action!r}")
