# Local Scribe

Offline transcription and transcript validation for research.
Jeremy Riel, UIC TRAILblazer Lab.

Local Scribe transcribes audio and video with OpenAI's Whisper models entirely
on your own computer, then gives you a browser workbench for correcting the
transcript against the audio. Nothing is uploaded: no cloud service, no
account, no telemetry, and no third-party assets in the interface.

> **Disclaimer.** Local Scribe is provided **"AS IS", without warranty of any
> kind**, express or implied. The authors and the UIC TRAILblazer Lab are
> **not responsible or liable** for any data loss, data breach, or other harm
> arising from installing, configuring, or using this software, including but
> not limited to mishandling of recordings, transcripts, or other data by the
> user, the operating environment, or third-party components it depends on.
> **You are solely responsible for how you use this application** — including
> compliance with your IRB protocol, institutional data-security policy, and
> applicable law, and for securing the computer it runs on. See
> [LICENSE](LICENSE) for the full legal terms.

## Start it

| Platform | Command |
|---|---|
| Windows | double-click **`run.bat`** |
| macOS | double-click **`run.command`** |
| Linux | **`./run.sh`** |

The first run creates a private Python environment in `.venv` and installs
dependencies (about 250 MB, a few minutes). Later runs skip that and start in
seconds. A browser opens automatically at <http://127.0.0.1:43707>.

Running the launcher again shuts down any existing instance first, so only one
copy is ever running.

### Requirements

- **Python 3.10 to 3.13.** Python 3.14 is not yet usable because CTranslate2,
  the runtime that executes the Whisper model, has no 3.14 build. The launcher
  finds a supported version automatically if you have one installed.
- No `ffmpeg` installation is needed; media handling is built in.
- A network connection is needed **once**, to install packages and download a
  model. After that the app runs fully offline.

## First use

1. Open **AI Settings** and download a model. `large-v3-turbo` (1.6 GB) is the
   recommended default; the page recommends one based on your hardware and
   explains why.
2. Turn on **Offline lock** in AI Settings → Privacy. All outbound network
   access is then blocked inside the process, enforced at the socket level.
3. Back on **Projects**, create a project, add recordings, and press
   **Transcribe**.
4. Open a transcribed file to review it: play the audio, click the time ribbon
   or any word to hear it again, and correct the text. Edits save
   automatically.
5. Set the number of **Speakers** at the top, give each a name, a short tag and
   a colour, then tag each speaking turn by clicking its tag or pressing
   <kbd>1</kbd>–<kbd>9</kbd>. A speaker's name is printed once per turn, so a
   pause in the middle of someone talking breaks the paragraph without
   repeating their name.
6. Use **Find** (or <kbd>Ctrl</kbd>+<kbd>F</kbd>) to fix a recurring error
   across the whole transcript. Red underlines mark words that are not in the
   dictionary, blue ones mark probable transcription slips; right-click either
   for suggestions or to accept the word.
7. When the human pass is done, press **Re-timestamp and rewrite captions**,
   then download the outputs.

## What you get per recording

Written into `projects/<project>/documents/<id>/outputs/`:

| File | Purpose |
|---|---|
| `transcript.txt` | Clean prose for reading and coding |
| `transcript.md` | Markdown with metadata front matter |
| `transcript.docx` | Word document with a metadata table |
| `transcript.vtt` | WebVTT captions for video software |
| `transcript.srt` | SubRip captions |
| `transcript.json` | Full word-level data for analysis |

## Supported formats

**Audio** wav, mp3, m4a, aac, flac, ogg, opus, wma, aiff, amr, ac3, mka, caf, au
**Video** mp4, mov, m4v, mkv, avi, webm, wmv, flv, mpg, mpeg, mts, m2ts, ts,
3gp, 3g2, ogv, asf, vob, divx, f4v, rm, rmvb

Audio is extracted from video automatically. Files are validated by inspecting
their actual contents, not their extension, so a mislabelled file still works.
Original files are never modified.

## Where things live

```
projects/            your projects: media, transcripts, exports
models/              downloaded Whisper weights
logs/                rotating application log and the pidfile
settings.json        your settings
.venv/               the private Python environment
```

A project is plain files, so you can inspect it, copy it to an encrypted
drive, or recover a transcript with a text editor. **Export project** writes a
single `.lsproj` archive; **Import project** reads one back.

## Options

```
run.bat --no-browser        start without opening a browser
run.bat --port 43711        use a different port
run.bat --reinstall         force dependency reinstallation
```

The port can also be set with the `LOCALSCRIBE_PORT` environment variable.

## Troubleshooting

**"Port 43707 is in use by ... which is not Local Scribe."**
Something else holds the port. Local Scribe deliberately refuses to move to
another port, so that it can never collide with another local service. Stop
that process, or start with `--port 43711`.

**Transcription is slower than expected.**
Check the status pill in the top bar. If it says CPU while you have an NVIDIA
GPU, open AI Settings — the hardware panel explains why the GPU is unavailable.
The usual causes are a missing CUDA support library (there is an install
button) or an NVIDIA driver older than 527.

**The audio will not play in the editor.**
Formats such as `.mkv`, `.avi` and `.wmv` cannot be played by a browser, so a
small `.m4a` companion is generated during transcription. If it is missing,
re-transcribe the file, or check that "Keep browser preview audio" is on in AI
Settings.

**The transcript repeats a phrase over and over.**
Turn off "Condition on previous text" in AI Settings → Quality gates, or raise
the repetition penalty slightly, then re-transcribe.

## Tests

```
.venv/Scripts/python.exe -m tests.test_realign      # re-timestamping
.venv/Scripts/python.exe -m tests.test_exporters     # captions and exports
.venv/Scripts/python.exe -m tests.test_assets        # front-end asset guards
.venv/Scripts/python.exe -m tests.test_speakers      # roster and speaking turns
.venv/Scripts/python.exe -m tests.test_proofread     # spelling and artefacts
```

On macOS and Linux use `.venv/bin/python` instead.

## Licensing

Local Scribe is released under the **MIT licence**, copyright © 2026 Jeremy
Riel, UIC TRAILblazer Lab. See [LICENSE](LICENSE). You may use, modify and
redistribute it freely, including commercially, provided the copyright notice
and licence text are retained.

The software is provided **"AS IS", without warranty of any kind**. The
authors and copyright holders are not liable for any claim, damages, or other
liability — including data loss or a data-security breach — arising from the
software or its use. By using Local Scribe you accept full responsibility for
your own actions with it: how you configure it, what data you put into it,
and how you secure the machine it runs on.

If you use Local Scribe in published research, a citation of the software and
the TRAILblazer Lab is appreciated but not required.

### Components

Whisper (OpenAI), faster-whisper, CTranslate2, python-docx, Silero VAD,
FastAPI, Starlette, uvicorn and Jinja2 are MIT or BSD licensed. PyAV is BSD
licensed and links FFmpeg, which is LGPL/GPL depending on build. The bundled
spelling dictionary is derived from SCOWL (BSD-style; its notice ships at
`app/data/dictionary/SCOWL-LICENSE.txt`). Model weights are redistributed by
their publishers under their own terms. Full detail is in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
