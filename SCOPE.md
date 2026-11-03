# Meeting notes — scope

Local-only meeting recorder modelled on Notion's AI Meeting Notes: one click to
record, a transcript that fills in while the call runs, and an AI-written
summary with action items when it ends. Every model runs on this machine.

## What was found on this machine (2026-10-28)

| Need | What exists | Decision |
|---|---|---|
| System audio capture | Linux + PipeWire. `fused.capture.audio({source:"system"})` is refused on Linux; `fused.capture.screen({audio:"system"})` needs a browser share picker and records video too. `ffmpeg -f pulse` on the default sink's `.monitor` and the mic works and survives a `runPython` return when started detached (verified). | **Native PipeWire capture via ffmpeg**, spawned detached by `recorder.py`. Records 16 kHz stereo, mic left and system right, so "Me" is known for free; `stop` also writes a mono mix for the player. Mic is optional; meetings recorded before this stay mono. |
| System audio capture, macOS (2026-11-03) | `fused.capture.audio` records system audio (Core Audio tap, 14.2+) and the mic natively, but `source: "both"` mixes them into one track, and its `.m4a` cannot be read until it is closed. Writing to a `.aac` path gives ADTS, which decodes while it grows (verified), and `ffmpeg -follow 1` tails it. No `pactl`/PipeWire on macOS. | **Two captures (system + mic) into `.aac` take files**, followed by the same detached ffmpeg into the same stereo PCM, so slicing, the final pass and "Me" work unchanged. ffmpeg bundled via `imageio-ffmpeg`. A pause or stop on either recording pill is applied to both. The mic is the system's default input (audio-only capture cannot pick one). |
| Transcription | fused-render's local `faster-whisper` runner (CTranslate2, CPU). `faster-whisper-small` and `large-v3-turbo` already downloaded. | **`fused.ai.transcribe`**: whisper small for live chunks (cut at pauses, 1 s overlap, language pinned, last words as prompt); Parakeet TDT v3 for the final pass (whisper large-v3-turbo when the language is outside Parakeet's 25). |
| Speaker detection | `diarize: true` on transcribe runs sherpa-onnx pyannote segmentation + speaker embeddings (already downloaded). | **Diarize the system channel on the final pass** into people − 1 voices (count asked at stop); the mic channel is "Me", minus lines that echo the call. Without a count, voices with little talk time are folded into their neighbours. Live chunks are not diarized (too short). |
| Summary | local `llama.cpp` (Vulkan, Radeon RX 9060 XT). `Qwen3.5-4B-Q4_K_M.gguf` downloaded, recommended. Context = 8192 tokens. | **`fused.ai.text` with the catalog's recommended local text model**. Long transcripts are summarised in chunks, then merged. No cloud provider ever. |
| Storage | `.fused/data/` | One folder per meeting: `audio.wav`, `meeting.json` (title, notes, transcript, summary, speaker names). |

## Notion feature parity

Notion AI Meeting Notes gives: a start-recording block, live transcript beside
your own notes, speaker labels, summary templates (Auto, Standup, Sales call,
Interview, Team meeting…), a "Next steps" checklist, and a Transcript tab with
timestamps. Mapped here:

- **Record / Stop** on the meeting page. Elapsed time and level of activity shown. The recording also appears as a download-manager job row so it survives navigation.
- **Live transcript**: every ~20 s the new slice of audio is transcribed and appended.
- **Final pass** at stop: whole file re-transcribed with the big model + diarization, replacing the live text.
- **Summary** with a format picker (Auto, Standup, 1:1, Sales call, Interview, Brainstorm) producing: Summary, Key points, Decisions, Action items (checkboxes), Open questions.
- **Your notes**: a free-text area that is saved with the meeting and fed to the summariser as context.
- **Transcript tab**: speaker-coloured, timestamped, click-to-rename speakers, search.
- **Meetings sidebar**: list, new, delete, title edit.
- **Copy as Markdown** for pasting into Notion or anywhere.

## Out of scope (v1)

- Calendar detection of meetings, auto-start.
- Per-word speaker labels: diarization labels whole segments and the render API returns no turns to split them on.
- Windows (fused.capture has no Windows backend; the page shows why recording is unavailable).
- Translation, multi-language summaries, export to Notion API.

## Files

- `index.html` — the whole UI.
- `recorder.py` — start / status / stop / slice of the ffmpeg capture; device listing; macOS take paths (`prepare`).
- `meetings.py` — CRUD over `.fused/data/meetings/<id>/meeting.json`.
- `SKILL.md` — bot-callable description of the two `.py` files.
- `pyproject.toml` — calendar parsing, plus `imageio-ffmpeg` on macOS; locked to prebuilt wheels.
