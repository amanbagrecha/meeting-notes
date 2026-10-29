---
name: meeting-notes
description: Local meeting recorder. Records system audio + mic over PipeWire, keeps meetings with transcripts and AI summaries in .fused/data/meetings.
approve: [recorder.py]
---
# Meeting notes

Meetings live in `.fused/data/meetings/<id>/` as `meeting.json` beside the
recorded `audio.wav`. Transcription and summarisation run from the page
through `fused.ai` (local Whisper + local llama.cpp), not from these files.

## meetings.py
Lists, creates, reads, patches or deletes meeting records. Writes `.fused/data/meetings/<id>/meeting.json`.
- Args: `action` (str, default "list": one of list | create | get | update | delete), `meeting_id` (str, required for get/update/delete), `title` (str, default "", used by create), `patch` (str, JSON object, used by update; keys: title, notes, transcript, summary, speaker_names, duration_s, status, format, expected_speakers, calendar)
- Returns: list → `[{id, title, created_at, duration_s, status, has_summary, segments, speakers, excerpt, tasks, open_tasks, calendar_key}]` (`calendar_key` = `uid@start` of the linked calendar event, or null) (`excerpt` = first prose of the summary, ≤ 220 chars; `tasks`/`open_tasks` count the summary's checkboxes); create/get/update → the full meeting object; delete → `{"ok": true, "id": str}`
- Example: `{"action":"py","app":"meeting-notes","file":"meetings.py","args":{"action":"get","meeting_id":"20261028-171500-ab12cd"}}`

## recorder.py
Starts or stops a detached ffmpeg capture of the chosen output's monitor (+ chosen mic) into `.fused/data/meetings/<id>/audio.pcm`; `stop` also writes `audio.wav`. Devices come from `.fused/data/settings.json` (default output monitor and default input unless set). Starting a recording captures everything the machine plays.
- Args: `action` (str, default "status": one of capabilities | settings | meter | status | start | stop | slice), `meeting_id` (str, required for status/start/stop/slice), `mic` (bool, default true, start only; false records without the mic), `frm` / `to` (float seconds, slice only), `system` (str, settings only: source name of the output monitor, "" = default), `mic_device` (str, settings only: source name, "" = default, "off" = no mic), `secs` (float, default 0.5, meter only)
- Returns: status/start/stop → `{meeting_id, running, levels, seconds, bytes, mic, monitor, mic_device, started_at, wav, error}` (`levels` = dBFS per 50 ms over the last second while running); capabilities/settings → `{available, reason, sink, monitor, monitor_label, mic, mic_label, default_monitor, default_mic, settings, devices:[{name,label,monitor}], current}`; meter → `{system:{device, levels, error}, mic:{device, levels, error}}`; slice → `{path, from, to, seconds}` (a WAV in `.fused/cache/slices/`)
- Example: `{"action":"py","app":"meeting-notes","file":"recorder.py","args":{"action":"meter","secs":"0.5"}}`

## gcal.py
Lists calendar events around now from the Google Calendar secret iCal address saved in `.fused/data/calendar.json`. Fetches the feed over the network at most every 5 min; writes only its cache in `.fused/cache/calendar/`.
- Args: `action` (str, default "upcoming": one of upcoming | status), `hours_back` (float, default 3), `hours_ahead` (float, default 24), `refresh` (bool, default false: fetch now instead of using a cache under 5 min old)
- Returns: upcoming → `{configured, events: [{uid, start, end, title, location, link, attendees: [{name, email, status}], people, invited}], fetched_at, error, now}` (`people` = accepted attendees, or everyone not declined when fewer than two accepted; `invited` = everyone not declined; times are epoch seconds); status → `{configured}`
- Example: `{"action":"py","app":"meeting-notes","file":"gcal.py","args":{"action":"upcoming","hours_ahead":"8"}}`
