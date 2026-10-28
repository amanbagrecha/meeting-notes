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
PATCHABLE = {"title", "notes", "transcript", "summary", "speaker_names", "duration_s", "status", "format"}


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
    }


def _row(m):
    seg = m.get("transcript", {}).get("segments", [])
    return {
        "id": m["id"], "title": m["title"], "created_at": m["created_at"],
        "duration_s": m.get("duration_s", 0), "status": m.get("status"),
        "has_summary": bool(m.get("summary")), "segments": len(seg),
    }


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
