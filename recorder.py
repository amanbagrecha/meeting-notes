"""System-audio + microphone recorder driven by ffmpeg: PipeWire on Linux,
fused.capture files on macOS.

Every call is a fresh process (fused-render runs `main` in a subprocess and
kills it at 60 s), so the capture itself is a *detached* ffmpeg that outlives
the call. State lives in `.fused/data/meetings/<id>/rec.json`; the raw audio
is appended to `audio.pcm` (16 kHz, s16le, interleaved) so a growing file can
be sliced at any time without caring about a WAV header. A meeting is stereo
(L = microphone, R = system audio) or, when recorded without a mic or before
stereo existed, mono; `rec.json` keeps that as `channels`, fixed by the first
take. `stop` wraps the audio into `audio.wav` (mono mix for playback and
transcription) plus, for stereo, `audio.mic.wav` and `audio.system.wav`.

On macOS the page records the system audio with fused.capture into an ADTS
`.aac` take file (readable while it grows); the detached ffmpeg follows that
file and records the mic itself over AVFoundation, padding the mic's start so
both channels begin at the moment the system capture started.
"""

import array
import json
import math
import operator
import os
import shutil
import signal
import subprocess
import re
import sys
import time
import wave

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
DARWIN = sys.platform == "darwin"


def _find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.access(path, os.X_OK):
            return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _find_ffmpeg()


def _install_command(missing):
    if DARWIN:
        return "brew install ffmpeg" if shutil.which("brew") or os.access("/opt/homebrew/bin/brew", os.X_OK) else None
    pulse = {"apt-get": "pulseaudio-utils", "dnf": "pulseaudio-utils", "zypper": "pulseaudio-utils", "pacman": "libpulse"}
    tools = {"apt-get": "sudo apt-get install -y", "dnf": "sudo dnf install -y",
             "zypper": "sudo zypper install -y", "pacman": "sudo pacman -S --needed"}
    for manager, cmd in tools.items():
        if shutil.which(manager):
            pkgs = [pulse[manager] if t == "pactl" else t for t in missing]
            return f"{cmd} {' '.join(pkgs)}"
    return None


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
    if DARWIN:
        try:
            out = subprocess.run(["ps", "-o", "stat=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return True
        return bool(out) and not out.startswith("Z") and "ffmpeg" in out
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
    if DARWIN:
        return _mac_capabilities()
    missing = [t for t in ("ffmpeg", "pactl") if not shutil.which(t)]
    if missing:
        return {"available": False, "reason": f"needs {', '.join(missing)} on PATH (PipeWire/PulseAudio + ffmpeg)",
                "install": _install_command(missing)}
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


def _mac_mics():
    """AVFoundation audio input names, as ffmpeg lists them."""
    try:
        err = subprocess.run([FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                             capture_output=True, text=True, timeout=10).stderr
    except (OSError, subprocess.TimeoutExpired):
        return []
    names, audio = [], False
    for line in err.splitlines():
        if "AVFoundation audio devices" in line:
            audio = True
        elif "AVFoundation video devices" in line:
            audio = False
        else:
            m = re.search(r"\] \[\d+\] (.+)$", line)
            if audio and m:
                names.append(m.group(1).strip())
    return names


def _mac_capabilities():
    """macOS: the page records system audio with fused.capture; ffmpeg records the mic."""
    if not FFMPEG:
        return {"available": False, "install": _install_command(["ffmpeg"]),
                "reason": "needs ffmpeg (the app's Python environment did not install imageio-ffmpeg)"}
    st = _settings()
    mics = _mac_mics()
    devices = ([{"name": "system", "label": "Mac system audio", "monitor": True},
                {"name": "default", "label": "default input", "monitor": False}]
               + [{"name": n, "label": n, "monitor": False} for n in mics])
    if st["mic"] == "off" or not mics:
        mic = None
    else:
        mic = st["mic"] if st["mic"] in mics else "default"
    return {"available": True, "reason": None, "native": True, "sink": None,
            "monitor": "system", "monitor_label": "Mac system audio",
            "mic": mic, "mic_label": ("default input" if mic == "default" else mic) if mic else None,
            "default_monitor": "system", "default_mic": "default",
            "settings": {"system": "", "mic": st["mic"]}, "devices": devices}


def _mic_input(dev):
    if DARWIN:
        return ["-thread_queue_size", "4096", "-use_wallclock_as_timestamps", "1",
                "-f", "avfoundation", "-i", "none:" + dev]
    return ["-f", "pulse", "-i", dev]


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
        src = _mic_input(dev) if key == "mic" else ["-f", "pulse", "-i", dev]
        procs[key] = subprocess.Popen(
            [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"] + src + [
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
    per = []
    if running:
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
        "wav": wav if os.path.exists(wav) else None,
        "wav_mic": None,
        "wav_system": None,
        "error": err,
        "native": bool(rec.get("native")),
        "system_capture": rec.get("system_capture"),
    }
    if channels == 2:
        out["levels_mic"], out["levels_system"] = per if per else ([], [])
        for key, name in (("wav_mic", "audio.mic.wav"), ("wav_system", "audio.system.wav")):
            p = os.path.join(folder, name)
            out[key] = p if os.path.exists(p) else None
    return out


_TAKE = re.compile(r"^take-\d{8}-\d{6}\.system\.aac$")


def _is_take(folder, path):
    return (os.path.dirname(os.path.abspath(path)) == os.path.abspath(folder)
            and bool(_TAKE.match(os.path.basename(path))))


def _check_busy(meeting_id):
    current = _read_json(CURRENT, {})
    if current.get("meeting_id") and current["meeting_id"] != meeting_id:
        other = _status(current["meeting_id"])
        if other["running"]:
            raise RuntimeError(f"already recording meeting {current['meeting_id']}; stop it first")


def _prepare(meeting_id):
    """macOS: where the page's fused.capture recording writes this take."""
    if not DARWIN:
        raise RuntimeError("prepare is only used for macOS captures")
    _check_busy(meeting_id)
    folder = _folder(meeting_id)
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return {"system_file": os.path.join(folder, f"take-{stamp}.system.aac")}


def _follow(path, live=True):
    return (["-follow", "1"] if live else []) + ["-f", "aac", "-i", "file:" + path]


def _capture_cmd(sys_in, mic_in, channels, out, mic_t0=None):
    """`mic_t0`: epoch seconds the system audio started; the mic is padded to it."""
    mono = f"aresample={RATE},aformat=channel_layouts=mono"
    head = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "warning"]
    mic_chain = mono
    if mic_in and mic_t0 is not None:
        head.append("-copyts")
        mic_chain = (f"asetpts=N/SR/TB+STARTPTS-{mic_t0:.6f}/TB,aresample={RATE}:async=1:first_pts=0,"
                     "aformat=channel_layouts=mono,apad")
    cmd = head + sys_in
    if mic_in and channels == 2:
        cmd += mic_in + ["-filter_complex",
                         f"[0:a]{mono}[sys];[1:a]{mic_chain}[mic];"
                         "[mic][sys]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[a]",
                         "-map", "[a]"]
    elif mic_in:
        cmd += mic_in + ["-filter_complex",
                         f"[1:a]{mic_chain}[mic];"
                         f"[0:a][mic]amix=inputs=2:duration=first:dropout_transition=0,aresample={RATE}[a]",
                         "-map", "[a]"]
    elif channels == 2:  # no mic this take: keep the left channel silent so takes line up
        cmd += ["-filter_complex", f"[0:a]{mono},pan=stereo|c0=0*c0|c1=c0[a]", "-map", "[a]"]
    # -flush_packets 1: write every packet straight to disk so status/slice see
    # audio within ~100 ms instead of in 256 KiB (8 s) bursts.
    cmd += ["-ac", str(channels), "-ar", str(RATE), "-f", "s16le", "-c:a", "pcm_s16le",
            "-flush_packets", "1", "-y", out]
    return cmd


def _start(meeting_id, mic, system_file="", system_capture="", started_at=0.0):
    caps = _capabilities()
    if not caps["available"]:
        raise RuntimeError(caps["reason"])
    _check_busy(meeting_id)
    folder = _folder(meeting_id)
    os.makedirs(folder, exist_ok=True)
    st = _status(meeting_id)
    if st["running"]:
        return st
    # Each take records into its own part file and is appended to audio.pcm at
    # stop, so "Record more" on a finished meeting extends it instead of
    # overwriting it (ffmpeg cannot append).
    part = os.path.join(folder, "audio.part.pcm")
    _close_take(folder, _read_json(os.path.join(folder, "rec.json"), {}), False)
    use_mic = bool(mic and caps.get("mic"))
    mic_in = _mic_input(caps["mic"]) if use_mic else []
    if DARWIN:
        if not (system_file and _is_take(folder, system_file) and os.path.exists(system_file)):
            raise RuntimeError("the system-audio capture file is missing; start it with fused.capture first")
        sys_in = _follow(system_file)
    else:
        sys_in = ["-f", "pulse", "-i", caps["monitor"]]
    # The first take fixes the layout: stereo (L mic, R system) if it has a mic.
    if _size(os.path.join(folder, "audio.pcm")):
        channels = _read_json(os.path.join(folder, "rec.json"), {}).get("channels", 1)
    else:
        channels = 2 if use_mic else 1
    t0 = (float(started_at) or time.time()) if DARWIN else None
    st = _launch(meeting_id, folder, _capture_cmd(sys_in, mic_in, channels, part, t0), caps, channels, use_mic,
                 system_file, system_capture)
    if not st["running"] and DARWIN and use_mic:
        mic_error = st["error"]
        st = _launch(meeting_id, folder, _capture_cmd(sys_in, [], channels, part), caps, channels, False,
                     system_file, system_capture)
        st["mic_error"] = mic_error or "the microphone could not be opened"
    if not st["running"]:
        rec_path = os.path.join(folder, "rec.json")
        rec = _read_json(rec_path, {})
        rec["state"] = "failed"
        _write_json(rec_path, rec)
        raise RuntimeError(st["error"] or "ffmpeg failed to start")
    return st


def _launch(meeting_id, folder, cmd, caps, channels, use_mic, system_file, system_capture):
    log = open(os.path.join(folder, "ffmpeg.log"), "ab")
    proc = subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=log, close_fds=True)
    rec = {"pid": proc.pid, "state": "recording", "started_at": time.time(),
           "channels": channels, "mic": use_mic, "monitor": caps["monitor"], "mic_device": caps["mic"] if use_mic else None}
    if DARWIN:
        rec.update(native=True, system_file=system_file, system_capture=system_capture or None)
    _write_json(os.path.join(folder, "rec.json"), rec)
    _write_json(CURRENT, {"meeting_id": meeting_id, "pid": proc.pid})
    # Give ffmpeg a moment to fail fast on a bad device.
    time.sleep(0.6)
    return _status(meeting_id)


def _absorb_part(folder):
    """Append a finished take (audio.part.pcm) onto audio.pcm."""
    part = os.path.join(folder, "audio.part.pcm")
    if not os.path.exists(part):
        return
    pcm = os.path.join(folder, "audio.pcm")
    with open(part, "rb") as src, open(pcm, "ab") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(part)


def _settle(path, quiet=1.0, limit=15.0):
    """Wait until `path` stops growing: the follower has read the whole take."""
    last, since, start = _size(path), time.time(), time.time()
    while time.time() - since < quiet and time.time() - start < limit:
        time.sleep(0.2)
        size = _size(path)
        if size != last:
            last, since = size, time.time()


def _close_take(folder, rec, followed):
    """Fold the take in progress into audio.pcm.

    A macOS take file is removed once absorbed, unless its follower had died
    (the file may hold call audio the follower never read).
    """
    _absorb_part(folder)
    path = rec.get("system_file")
    if rec.get("native") and path and _is_take(folder, path) and (followed or rec.get("state") != "recording"):
        try:
            os.remove(path)
        except OSError:
            pass


def _signal(pid, sig):
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _pause(meeting_id, paused):
    """macOS: freeze the follower while the system capture is paused, so the
    mic is not recorded either and both channels stay in step."""
    rec = _read_json(os.path.join(_folder(meeting_id), "rec.json"), {})
    pid = rec.get("pid")
    if rec.get("native") and rec.get("state") == "recording" and _alive(pid):
        _signal(pid, signal.SIGSTOP if paused else signal.SIGCONT)
    return _status(meeting_id)


def _stop(meeting_id):
    folder = _folder(meeting_id)
    rec_path = os.path.join(folder, "rec.json")
    rec = _read_json(rec_path, {})
    pid = rec.get("pid")
    followed = bool(pid and _alive(pid))
    if followed:
        # A macOS follower waits at the end of its file for more audio and
        # ignores SIGINT there, so let it drain, then kill it.
        if rec.get("native"):
            _signal(pid, signal.SIGCONT)
            _settle(os.path.join(folder, "audio.part.pcm"))
        try:
            os.kill(pid, signal.SIGKILL if rec.get("native") else signal.SIGINT)
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
    cur = _read_json(CURRENT, {})
    if cur.get("meeting_id") == meeting_id:
        _write_json(CURRENT, {})
    _close_take(folder, rec, followed)
    rec["state"] = "stopped"
    rec["stopped_at"] = time.time()
    _write_json(rec_path, rec)
    pcm = os.path.join(folder, "audio.pcm")
    wav = os.path.join(folder, "audio.wav")
    if _size(pcm) > 0:
        channels = rec.get("channels", 1)
        cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
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
        if channels == 2:
            data = subprocess.run(
                [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
                 "-af", FILTERS[channel], "-f", "s16le", "-ac", "1", "pipe:1"],
                input=data, check=True, timeout=30, stdout=subprocess.PIPE).stdout
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
         secs: float = 0.5, channel: str = "mix", snap: float = 0.0,
         system_file: str = "", system_capture: str = "", started_at: float = 0.0):
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
        if DARWIN:
            return _meter({"mic": caps["mic"]}, secs)
        return _meter({"system": caps["monitor"], "mic": caps["mic"]}, secs)
    if action == "status":
        return _status(meeting_id)
    if action == "prepare":
        return _prepare(meeting_id)
    if action == "start":
        return _start(meeting_id, mic, system_file, system_capture, started_at)
    if action in ("pause", "resume"):
        return _pause(meeting_id, action == "pause")
    if action == "stop":
        return _stop(meeting_id)
    if action == "slice":
        return _slice(meeting_id, float(frm), float(to), channel, float(snap))
    raise ValueError(f"unknown action {action!r}")
