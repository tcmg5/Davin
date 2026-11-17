# Claude Auto Edit for DaVinci Resolve

A script for DaVinci Resolve that sends your footage to Claude and builds a new edited timeline from what Claude decides.

1. Select clips in the Media Pool. If you select nothing, it uses every video clip in the current bin.
2. Run **Workspace → Scripts → Edit → claude_auto_edit**.
3. Type what you want, for example *"60-second highlight reel, upbeat, open on the best laugh"*, then click **Edit with Claude**.
4. A new timeline appears, named after the edit, with a marker at each cut that explains why Claude made it.

Your original clips and timelines are never changed.

## How Claude "watches" the footage

For each clip the script sends Claude:

- **A time-stamped transcript** of the speech, made locally with Whisper. This lets Claude cut on sentence boundaries and drop filler, false starts and retakes.
- **Still frames** sampled through the clip with ffmpeg. This lets Claude judge framing and focus, and pick B-roll.
- The clip's duration and frame rate.

Claude sends back an edit decision list. The script checks it (bad clip numbers are skipped and cuts are kept inside each clip) and builds the timeline. Each plan is also saved as JSON in `~/.claude_auto_edit/`.

## Install

**1. Python packages.** Use the same Python that Resolve uses (3.10+ recommended):

```bash
pip install anthropic            # required
pip install faster-whisper       # optional but recommended: speech transcripts
```

**2. ffmpeg** (optional, for frames). Install it and make sure it is on your `PATH`, for example `brew install ffmpeg` or `winget install ffmpeg`.

**3. API key.** Get one at https://console.anthropic.com. Apps opened from the Dock or Start menu often can't see shell environment variables, so the simplest option is to save the key to a file:

```bash
mkdir -p ~/.claude_auto_edit && echo "sk-ant-..." > ~/.claude_auto_edit/api_key
```

Setting `ANTHROPIC_API_KEY` also works.

**4. Copy the script** into Resolve's Scripts folder:

| OS | Folder |
|---|---|
| macOS | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/` |
| Windows | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit\` |
| Linux | `~/.local/share/DaVinciResolve/Fusion/Scripts/Edit/` |

Restart Resolve. The script then appears under **Workspace → Scripts → Edit**. Progress and errors appear in **Workspace → Console**.

## Running from a terminal

With Resolve open and **Preferences → System → General → External scripting using** set to **Local**:

```bash
python3 claude_auto_edit.py --prompt "Cut this interview down to 90 seconds" --target-seconds 90
```

Options: `--effort low|medium|high|xhigh|max`, `--frames-per-clip N`, `--no-transcribe`, `--no-markers`, `--plan-only` (saves the plan without building a timeline), `--model`.

## Settings

The `CONFIG` block at the top of `claude_auto_edit.py` controls the defaults: the instructions, the target length, the model (`claude-opus-5-5`), the effort, the number of frames per clip, the Whisper model size, and the markers.

## Notes and limits

- **Cost:** each run is one API call. Frames make up most of the input, so for long projects lower `frames_per_clip` or turn frames off. The first transcription of each clip takes time, but transcripts are cached afterwards.
- **Refusal fallback:** the script turns on the API's server-side `fallbacks: "default"`. If a safety classifier declines the request, it is retried automatically on a fallback model.
- **What's edited:** the script cuts and arranges video clips on a single track (audio comes with them). It doesn't do color, effects, titles or music.
- **Dialog:** the dialog uses Resolve's UI Manager. If that isn't available in your Resolve version, the script falls back to the `CONFIG` instructions. Use `--prompt` from a terminal instead.
