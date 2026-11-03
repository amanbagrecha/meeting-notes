# Meeting notes

A local-only meeting recorder for fused-render, modelled on Notion's AI Meeting Notes. One click records system audio plus your microphone, a transcript fills in while the call runs, and when you stop an AI-written summary with key points, decisions, action items and open questions appears beside your own notes. Every model runs on this machine; nothing leaves it.

## What it does

- **Record / Stop** from the meeting page, with elapsed time, live level meters and a no-sound warning. The recording also shows up as a download-manager job row so it survives navigation.
- **Live transcript** appended every ~20 s with a small Whisper model, then replaced at stop by a full pass with a larger model and speaker diarization (skippable).
- **Summary** in a chosen format (Auto, Standup, 1:1, Sales call, Interview, Brainstorm), produced by the local text model from the catalog.
- **Your notes**: free text saved with the meeting and fed to the summariser as context.
- **Transcript tab**: speaker-coloured, timestamped, searchable, click to rename speakers.
- **Meetings sidebar**: list, create, rename and delete meetings. Collapses into a drawer on narrow widths.

## Files

- `index.html` — the whole UI; talks to Python through `fused.runPython` and to local models through `fused.ai`.
- `recorder.py` — spawns a detached `ffmpeg` that writes the meeting's audio as 16 kHz stereo (left = your mic, right = system audio): on Linux it reads PipeWire (default sink monitor + mic); on macOS it follows the two `.aac` files `fused.capture` is recording. Also slices the growing audio for live transcription.
- `meetings.py` — stores each meeting under `.fused/data/meetings/<id>/` (`audio.wav`, `meeting.json`).
- `SCOPE.md` — design notes and the decisions behind the above.

## Requirements

- **macOS 13+**: nothing to install. Audio is recorded by fused-render's native capture; the first recording asks for Microphone and System Audio Recording permission. ffmpeg comes bundled through the `imageio-ffmpeg` wheel.
- **Linux**: PipeWire (or PulseAudio) with `ffmpeg` and `pactl` on PATH. When they are missing, the record card offers an **Install** button that runs the right package-manager command in the terminal.

Both need fused-render's local transcription and text models downloaded (Whisper via faster-whisper, a llama.cpp text model). No network is used at runtime.
