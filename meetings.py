"""Meeting records: one folder per meeting under .fused/data/meetings/<id>/
holding meeting.json (title, notes, transcript, summary, speaker names) beside
the audio the recorder writes there."""

import json
import os
import re
import shutil
import time
import uuid

APP = os.path.dirname(os.path.abspath(__file__))
MEETINGS = os.path.join(APP, ".fused", "data", "meetings")

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Keys the page may patch. Anything else in `patch` is ignored.
PATCHABLE = {"title", "notes", "transcript", "summary", "speaker_names", "duration_s", "status", "format",
             "expected_speakers", "calendar"}


def _path(meeting_id):
    if not _ID.match(meeting_id or ""):
        raise ValueError("meeting_id must be 1-64 chars of [A-Za-z0-9_-]")
    return os.path.join(MEETINGS, meeting_id, "meeting.json")


def _read(meeting_id):
    p = _path(meeting_id)
    if not os.path.exists(p):
        raise FileNotFoundError(f"no meeting {meeting_id}")
    with open(p) as f:
        return json.load(f)


def _write(meeting):
    p = _path(meeting["id"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meeting, f)
    os.replace(tmp, p)
    return meeting


def _new(title):
    now = time.time()
    return {
        "id": time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + uuid.uuid4().hex[:6],
        "title": title or time.strftime("Meeting %b %d, %Y %H:%M", time.localtime(now)),
        "created_at": now,
        "duration_s": 0,
        "status": "new",            # new | recording | transcribing | done
        "notes": "",
        "format": "auto",
        "transcript": {"segments": [], "speakers": [], "final": False, "model": None},
        "speaker_names": {},
        "summary": None,            # {markdown, model, format, created_at}
        "expected_speakers": None,  # people in the call; passed to diarization as the exact count
        "calendar": None,           # {uid, start, end, title, attendees: [name]} when started from an event
    }


_HEADING = re.compile(r"^#{1,6}\s+(.*)")
_TASK = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+")


def _excerpt(markdown, limit=220):
    """First prose under the '## Summary' heading (else the first non-heading
    text), bullets flattened, cut to `limit` chars for the home-page card."""
    lines = (markdown or "").split("\n")
    picked, in_summary, found = [], False, False
    for raw in lines:
        l = raw.strip()
        h = _HEADING.match(l)
        if h:
            if found:
                break
            in_summary = "summary" in h.group(1).lower()
            continue
        if not l:
            continue
        if in_summary or not found:
            picked.append(re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(?:\[[ xX]\]\s+)?", "", l))
            found = True
            if not in_summary:
                break
    text = " ".join(picked).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "\u2026"


def _row(m):
    tr = m.get("transcript", {})
    seg = tr.get("segments", [])
    md = (m.get("summary") or {}).get("markdown", "")
    tasks = [t.group(1) for t in (_TASK.match(l) for l in md.split("\n")) if t]
    return {
        "id": m["id"], "title": m["title"], "created_at": m["created_at"],
        "duration_s": m.get("duration_s", 0), "status": m.get("status"),
        "has_summary": bool(m.get("summary")), "segments": len(seg),
        "speakers": len(tr.get("speakers", [])),
        "excerpt": _excerpt(md),
        "tasks": len(tasks), "open_tasks": sum(1 for t in tasks if t == " "),
        "calendar_key": _cal_key(m.get("calendar")),
    }


def _cal_key(c):
    """uid@start: one occurrence of a (possibly recurring) calendar event."""
    if not isinstance(c, dict) or not c.get("uid"):
        return None
    return f"{c['uid']}@{int(c.get('start') or 0)}"


def main(action: str = "list", meeting_id: str = "", title: str = "", patch: str = ""):
    os.makedirs(MEETINGS, exist_ok=True)
    if action == "list":
        rows = []
        for name in os.listdir(MEETINGS):
            p = os.path.join(MEETINGS, name, "meeting.json")
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        rows.append(_row(json.load(f)))
                except (OSError, ValueError):
                    continue
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows
    if action == "create":
        return _write(_new(title.strip()))
    if action == "get":
        return _read(meeting_id)
    if action == "update":
        m = _read(meeting_id)
        data = json.loads(patch) if patch else {}
        if not isinstance(data, dict):
            raise ValueError("patch must be a JSON object")
        for k, v in data.items():
            if k in PATCHABLE:
                m[k] = v
        m["updated_at"] = time.time()
        return _write(m)
    if action == "delete":
        p = _path(meeting_id)
        shutil.rmtree(os.path.dirname(p), ignore_errors=True)
        return {"ok": True, "id": meeting_id}
    raise ValueError(f"unknown action {action!r}")
