"""Calendar feed: reads the user's Google Calendar through its private iCal
address (Settings → your calendar → Integrate calendar → "Secret address in
iCal format") and lists the events around now with their attendees.

The address is a secret: it lives in .fused/data/calendar.json (written by the
page) and never travels as a parameter. The feed is the only network access
in the app; it is fetched at most every FEED_TTL seconds and the expanded
window is cached in .fused/cache/calendar/, so polling this file is cheap."""

import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

APP = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(APP, ".fused", "data", "calendar.json")
CACHE = os.path.join(APP, ".fused", "cache", "calendar")
CACHE_VERSION = "v1"
FEED_TTL = 300            # seconds between fetches of the feed
WINDOW_BACK = 12 * 3600   # expanded window kept in cache, around fetch time
WINDOW_AHEAD = 36 * 3600


def _config():
    try:
        with open(CONFIG) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def _url(cfg):
    u = (cfg.get("ics_url") or "").strip()
    if u.startswith("webcal://"):
        u = "https://" + u[len("webcal://"):]
    return u


def _cache_path(url):
    key = hashlib.sha256((CACHE_VERSION + "|" + url).encode()).hexdigest()[:20]
    return os.path.join(CACHE, key + ".json")


def _write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _epoch(v):
    if isinstance(v, datetime):
        if v.tzinfo is None:  # floating time: the machine's local zone
            return v.timestamp()
        return v.astimezone(timezone.utc).timestamp()
    return None  # all-day (date) events are not meetings


def _attendees(ev):
    raw = ev.get("ATTENDEE")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    out = []
    for a in raw:
        p = getattr(a, "params", {}) or {}
        cutype = str(p.get("CUTYPE", "INDIVIDUAL")).upper()
        if cutype in ("RESOURCE", "ROOM"):
            continue
        email = str(a).split(":", 1)[-1] if ":" in str(a) else str(a)
        out.append({
            "name": str(p.get("CN", "")) or email,
            "email": email,
            "status": str(p.get("PARTSTAT", "NEEDS-ACTION")).lower(),
        })
    return out


def _conference(ev):
    for k in ("X-GOOGLE-CONFERENCE", "URL"):
        v = ev.get(k)
        if v:
            return str(v)
    loc = str(ev.get("LOCATION") or "")
    return loc if loc.startswith("http") else ""


def _expand(ics_bytes, now):
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics_bytes)
    a = datetime.fromtimestamp(now - WINDOW_BACK, timezone.utc)
    b = datetime.fromtimestamp(now + WINDOW_AHEAD, timezone.utc)
    events = []
    for ev in recurring_ical_events.of(cal).between(a, b):
        start, end = ev.get("DTSTART"), ev.get("DTEND")
        s = _epoch(start.dt) if start else None
        if s is None:
            continue
        e = _epoch(end.dt) if end else None
        if e is None:
            dur = ev.get("DURATION")
            e = s + (dur.dt.total_seconds() if dur else 3600)
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        people = _attendees(ev)
        going = [p for p in people if p["status"] != "declined"]
        # Diarization treats this as the EXACT voice count, so overcounting
        # splits real voices. Count only people who accepted; fall back to
        # everyone not declined when fewer than two have answered at all.
        accepted = [p for p in people if p["status"] == "accepted"]
        count = accepted if len(accepted) >= 2 else going
        events.append({
            "uid": str(ev.get("UID", "")),
            "start": s, "end": e,
            "title": str(ev.get("SUMMARY", "")).strip() or "Untitled event",
            "location": str(ev.get("LOCATION") or ""),
            "link": _conference(ev),
            "attendees": people,
            # Google lists the organiser among attendees, so this includes you.
            "people": len(count) if count else None,
            "invited": len(going),
        })
    events.sort(key=lambda x: x["start"])
    return events


def _load(url, refresh):
    """Expanded events for the cached window, refetching when stale.
    Returns (events, fetched_at, error)."""
    path = _cache_path(url)
    cached = None
    try:
        with open(path) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        cached = None
    now = time.time()
    fresh = cached and now - cached.get("fetched_at", 0) < FEED_TTL
    # Window drifts: refetch before the cached expansion stops covering now+12h.
    covered = cached and cached.get("fetched_at", 0) + WINDOW_AHEAD - now > 12 * 3600
    if cached and fresh and covered and not refresh:
        return cached["events"], cached["fetched_at"], None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fused-meeting-notes/1"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
        events = _expand(body, now)
        _write_atomic(path, {"fetched_at": now, "events": events})
        return events, now, None
    except Exception as e:  # network down or bad feed: serve the last good copy
        msg = f"{type(e).__name__}: {e}"
        if cached:
            return cached["events"], cached["fetched_at"], msg
        return [], None, msg


def main(action: str = "upcoming", hours_back: float = 3, hours_ahead: float = 24, refresh: bool = False):
    cfg = _config()
    url = _url(cfg)
    if action == "status":
        return {"configured": bool(url)}
    if action != "upcoming":
        raise ValueError(f"unknown action {action!r}")
    if not url:
        return {"configured": False, "events": [], "fetched_at": None, "error": None}
    if not url.startswith("https://"):
        return {"configured": True, "events": [], "fetched_at": None,
                "error": "The calendar address must start with https:// (Google's secret iCal address)."}
    events, fetched_at, err = _load(url, refresh)
    now = time.time()
    lo, hi = now - hours_back * 3600, now + hours_ahead * 3600
    shown = [e for e in events if e["end"] > lo and e["start"] < hi]
    return {"configured": True, "events": shown, "fetched_at": fetched_at, "error": err, "now": now}
