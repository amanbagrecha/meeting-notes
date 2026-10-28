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
- Args: `action` (str, default "list": one of list | create | get | update | delete), `meeting_id` (str, required for get/update/delete), `title` (str, default "", used by create), `patch` (str, JSON object, used by update; keys: title, notes, transcript, summary, speaker_names, duration_s, status, format)
- Returns: list → `[{id, title, created_at, duration_s, status, has_summary, segments}]`; create/get/update → the full meeting object; delete → `{"ok": true, "id": str}`
- Example: `{"action":"py","app":"meeting-notes","file":"meetings.py","args":{"action":"get","meeting_id":"20261028-171500-ab12cd"}}`

## recorder.py
Starts or stops a detached ffmpeg capture of the default output's monitor (+ default mic) into `.fused/data/meetings/<id>/audio.pcm`; `stop` also writes `audio.wav`. Starting a recording captures everything the machine plays.
- Args: `action` (str, default "status": one of capabilities | status | start | stop | slice), `meeting_id` (str, required except for capabilities), `mic` (bool, default true, start only), `frm` / `to` (float seconds, slice only)
- Returns: status/start/stop → `{meeting_id, running, seconds, bytes, mic, started_at, wav, error}`; capabilities → `{available, reason, sink, monitor, mic, devices, current}`; slice → `{path, from, to, seconds}` (a WAV in `.fused/cache/slices/`)
- Example: `{"action":"py","app":"meeting-notes","file":"recorder.py","args":{"action":"status","meeting_id":"20261028-171500-ab12cd"}}`
