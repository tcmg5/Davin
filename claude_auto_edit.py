#!/usr/bin/env python3
"""
Claude Auto Edit: a DaVinci Resolve script that has Claude edit your footage.

How it works
------------
1. Collects the video clips you selected in the Media Pool (or, if nothing is
   selected, every video clip in the current Media Pool bin).
2. For each clip, gathers what Claude needs to "watch" it:
     - name, duration, frame rate
     - a time-stamped speech transcript (if faster-whisper or openai-whisper is installed)
     - a handful of still frames (if ffmpeg is on your PATH)
3. Sends all of that, plus your editing instructions, to Claude.
4. Claude returns an edit decision list (which clip, from when to when, in what order).
5. The script builds a brand-new timeline from that list and drops a marker on
   every cut explaining why Claude chose it. Your original footage and timelines
   are never modified.

Run it from inside Resolve: Workspace > Scripts > Edit > claude_auto_edit
or from a terminal with Resolve open:  python3 claude_auto_edit.py --prompt "..."

See README.md for installation.
"""

import argparse
import base64
import datetime
import json
import os
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# Configuration: edit these defaults, or override them from the dialog / CLI.
# ---------------------------------------------------------------------------
CONFIG = {
    # What you want Claude to do with the footage.
    "instructions": (
        "Create a tight, engaging edit. Remove dead air, false starts, "
        "repeated takes and filler. Keep the best take of each idea and "
        "order the segments so the story flows naturally."
    ),
    # Optional target length of the finished edit in seconds (0 = let Claude decide).
    "target_seconds": 0,
    "model": "claude-opus-5-5",
    # low / medium / high / xhigh / max: higher = more careful edit, slower and pricier.
    "effort": "high",
    # Still frames sampled per clip and sent to Claude (0 disables; needs ffmpeg).
    "frames_per_clip": 6,
    # Hard cap on frames for the whole request.
    "max_total_frames": 90,
    # Transcribe speech (needs `pip install faster-whisper` or `openai-whisper`).
    "transcribe": True,
    "whisper_model": "base",  # tiny / base / small / medium / large-v3
    # Add a marker with Claude's reasoning at every cut.
    "add_markers": True,
    # Where transcripts and the edit plan are cached / saved.
    "work_dir": os.path.join(os.path.expanduser("~"), ".claude_auto_edit"),
}

SYSTEM_PROMPT = """You are a professional video editor working inside DaVinci Resolve.
You receive a set of source clips, each with its duration, a time-stamped transcript
of any speech, and still frames sampled at known timestamps. You produce an edit
decision list: an ordered list of segments, each taken from one source clip
between a start and end time (in seconds from the start of that clip).

Editing guidelines:
- Follow the user's instructions above all else.
- Cut on natural boundaries: begin a segment slightly before speech starts and end it
  just after a sentence finishes. Never cut mid-word; use the transcript timestamps.
- Prefer the cleanest, most confident take when material repeats.
- Use the frames to judge visual quality (focus, framing, exposure) and to pick
  good B-roll moments from clips without speech.
- Segments must lie within the clip's duration, and end must be greater than start.
- The same clip may be used more than once; segments may appear in any order.
- Give each segment a short label and a one-sentence reason."""

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "timeline_name": {"type": "string"},
        "summary": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clip_index": {"type": "integer"},
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                    "label": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["clip_index", "start_seconds", "end_seconds", "label", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["timeline_name", "summary", "segments"],
    "additionalProperties": False,
}

VIDEO_TYPES = ("Video", "Video + Audio")


def log(msg):
    print("[Claude Auto Edit] " + str(msg), flush=True)


# ---------------------------------------------------------------------------
# Resolve connection
# ---------------------------------------------------------------------------
def get_resolve():
    """Return the Resolve object whether we run inside Resolve or externally."""
    g = globals()
    if g.get("resolve"):  # Injected when launched from Workspace > Scripts
        return g["resolve"]
    if g.get("app"):
        try:
            return g["app"].GetResolve()
        except Exception:
            pass
    try:
        import DaVinciResolveScript as dvr  # noqa: N813
    except ImportError:
        # Default module locations so external runs work without extra setup.
        candidates = {
            "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
            "win32": os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                                  "Blackmagic Design", "DaVinci Resolve", "Support",
                                  "Developer", "Scripting", "Modules"),
            "linux": "/opt/resolve/Developer/Scripting/Modules",
        }
        path = candidates.get(sys.platform if sys.platform != "linux2" else "linux")
        if path and path not in sys.path:
            sys.path.append(path)
        import DaVinciResolveScript as dvr  # noqa: N813
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise RuntimeError("Could not connect to DaVinci Resolve. Is it running, and is "
                           "Preferences > System > General > External scripting set to Local?")
    return resolve


def collect_clips(media_pool):
    """Selected Media Pool clips, or every video clip in the current bin."""
    items = []
    try:
        items = media_pool.GetSelectedClips() or []
        if isinstance(items, dict):
            items = list(items.values())
    except Exception:
        items = []
    source = "selected clips"
    if not items:
        items = media_pool.GetCurrentFolder().GetClipList() or []
        source = "current bin"

    clips = []
    for item in items:
        props = item.GetClipProperty() or {}
        if props.get("Type") not in VIDEO_TYPES:
            continue
        try:
            fps = float(props.get("FPS") or 0) or 24.0
            frames = int(float(props.get("Frames") or 0))
        except ValueError:
            continue
        if frames <= 0:
            continue
        clips.append({
            "item": item,
            "name": item.GetName(),
            "path": props.get("File Path", ""),
            "fps": fps,
            "frames": frames,
            "duration": frames / fps,
        })
    log("Found %d video clip(s) from %s." % (len(clips), source))
    return clips


# ---------------------------------------------------------------------------
# Analysis: transcripts and frames
# ---------------------------------------------------------------------------
_whisper = None


def transcribe(path, cfg):
    """Return [(start, end, text), ...], cached on disk. Empty list if unavailable."""
    global _whisper
    if not cfg["transcribe"] or not path or not os.path.exists(path):
        return []
    st = os.stat(path)
    key = "%s_%d_%d_%s" % (os.path.basename(path), st.st_size, int(st.st_mtime), cfg["whisper_model"])
    cache = os.path.join(cfg["work_dir"], "transcripts", key + ".json")
    if os.path.exists(cache):
        with open(cache, "r", encoding="utf-8") as f:
            return [tuple(s) for s in json.load(f)]

    segments = None
    try:
        from faster_whisper import WhisperModel
        if _whisper is None:
            log("Loading faster-whisper model '%s'..." % cfg["whisper_model"])
            _whisper = ("faster", WhisperModel(cfg["whisper_model"], compute_type="int8"))
        segs, _ = _whisper[1].transcribe(path, vad_filter=True)
        segments = [(round(s.start, 2), round(s.end, 2), s.text.strip()) for s in segs]
    except ImportError:
        try:
            import whisper
            if _whisper is None:
                log("Loading openai-whisper model '%s'..." % cfg["whisper_model"])
                _whisper = ("openai", whisper.load_model(cfg["whisper_model"]))
            result = _whisper[1].transcribe(path)
            segments = [(round(s["start"], 2), round(s["end"], 2), s["text"].strip())
                        for s in result["segments"]]
        except ImportError:
            log("No Whisper package installed; skipping transcripts "
                "(pip install faster-whisper to enable).")
            cfg["transcribe"] = False
            return []
    except Exception as e:  # Clip without audio, codec issue, etc.
        log("Transcription failed for %s: %s" % (os.path.basename(path), e))
        return []

    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(segments, f)
    return segments


def extract_frames(path, duration, count):
    """Return [(timestamp, jpeg_bytes), ...] sampled evenly through the clip."""
    if count <= 0 or not path or not os.path.exists(path) or not shutil.which("ffmpeg"):
        return []
    frames = []
    for i in range(count):
        t = duration * (i + 0.5) / count
        cmd = ["ffmpeg", "-loglevel", "error", "-ss", "%.3f" % t, "-i", path,
               "-frames:v", "1", "-vf", "scale=512:-2", "-q:v", "5",
               "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=60).stdout
        except Exception:
            out = b""
        if out:
            frames.append((round(t, 2), out))
    return frames


def build_request_content(clips, cfg):
    """Build the user message: per-clip text + frames, then the instructions."""
    content = []
    frame_budget = cfg["max_total_frames"]
    per_clip = cfg["frames_per_clip"]
    if clips and per_clip * len(clips) > frame_budget:
        per_clip = max(1, frame_budget // len(clips)) if frame_budget >= len(clips) else 0

    if not shutil.which("ffmpeg") and per_clip:
        log("ffmpeg not found on PATH; Claude will edit from transcripts only.")

    for idx, clip in enumerate(clips):
        log("Analysing clip %d/%d: %s" % (idx + 1, len(clips), clip["name"]))
        transcript = transcribe(clip["path"], cfg)
        lines = ["## Clip %d: %s" % (idx, clip["name"]),
                 "Duration: %.2f s  |  Frame rate: %.3f fps" % (clip["duration"], clip["fps"])]
        if transcript:
            lines.append("Transcript (start-end seconds):")
            lines += ["[%.2f-%.2f] %s" % s for s in transcript]
        elif cfg["transcribe"]:
            lines.append("Transcript: (no speech detected)")
        content.append({"type": "text", "text": "\n".join(lines)})

        for t, jpg in extract_frames(clip["path"], clip["duration"], per_clip):
            content.append({"type": "text", "text": "Clip %d frame at %.2f s:" % (idx, t)})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(jpg).decode("ascii")}})

    request = "# Editing instructions\n" + cfg["instructions"].strip()
    if cfg["target_seconds"]:
        request += "\n\nTarget total length: about %d seconds." % cfg["target_seconds"]
    request += ("\n\nUse clip_index values 0-%d. Return the segments in the order they "
                "should appear on the timeline." % (len(clips) - 1))
    content.append({"type": "text", "text": request})
    return content


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------
def make_client():
    import anthropic

    # Apps launched from the Dock/Start menu often don't see shell env vars,
    # so also accept a key stored in ~/.claude_auto_edit/api_key.
    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.Anthropic()
    key_file = os.path.join(CONFIG["work_dir"], "api_key")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            return anthropic.Anthropic(api_key=f.read().strip())
    return anthropic.Anthropic()  # Falls back to `ant auth login` profiles, etc.


def ask_claude(content, cfg):
    import anthropic

    client = make_client()
    log("Asking %s for an edit (this can take a minute or two)..." % cfg["model"])
    try:
        with client.beta.messages.stream(
            model=cfg["model"],
            max_tokens=64000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": cfg["effort"],
                "format": {"type": "json_schema", "schema": EDIT_SCHEMA},
            },
            # If a safety classifier declines, retry automatically on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise RuntimeError("Anthropic API key missing or invalid. Set ANTHROPIC_API_KEY or put "
                           "your key in %s" % os.path.join(cfg["work_dir"], "api_key"))
    except anthropic.RateLimitError:
        raise RuntimeError("Rate limited by the Anthropic API; wait a moment and try again.")
    except anthropic.APIStatusError as e:
        raise RuntimeError("Anthropic API error %s: %s" % (e.status_code, e.message))
    except anthropic.APIConnectionError:
        raise RuntimeError("Could not reach the Anthropic API; check your internet connection.")

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to edit this material.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's response was cut off; try fewer clips.")
    text = next(b.text for b in response.content if b.type == "text")
    u = response.usage
    log("Tokens used: %d in / %d out." % (u.input_tokens, u.output_tokens))
    return json.loads(text)


# ---------------------------------------------------------------------------
# Timeline building
# ---------------------------------------------------------------------------
def plan_to_clip_infos(plan, clips):
    """Validate Claude's segments and convert them to AppendToTimeline dicts."""
    infos, notes = [], []
    for seg in plan.get("segments", []):
        idx = seg.get("clip_index")
        if not isinstance(idx, int) or not 0 <= idx < len(clips):
            log("Skipping segment with invalid clip_index %r" % idx)
            continue
        clip = clips[idx]
        start = max(0, int(round(seg["start_seconds"] * clip["fps"])))
        end = min(clip["frames"], int(round(seg["end_seconds"] * clip["fps"])))
        if end - start < 2:
            log("Skipping too-short segment in %s (%.2f-%.2f s)" %
                (clip["name"], seg["start_seconds"], seg["end_seconds"]))
            continue
        infos.append({"mediaPoolItem": clip["item"], "startFrame": start, "endFrame": end - 1})
        notes.append({"frames": end - start, "fps": clip["fps"],
                      "label": seg.get("label", ""), "reason": seg.get("reason", "")})
    return infos, notes


def build_timeline(project, media_pool, plan, clips, cfg):
    infos, notes = plan_to_clip_infos(plan, clips)
    if not infos:
        raise RuntimeError("Claude's plan contained no usable segments.")

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H%M")
    base = (plan.get("timeline_name") or "Claude Edit").strip()[:60]
    name = "%s (%s)" % (base, stamp)
    timeline = media_pool.CreateEmptyTimeline(name)
    if not timeline:
        raise RuntimeError("Resolve could not create timeline '%s'." % name)
    project.SetCurrentTimeline(timeline)

    if not media_pool.AppendToTimeline(infos):
        raise RuntimeError("Resolve refused to append the clips to the timeline.")

    if cfg["add_markers"]:
        tl_fps = float(project.GetSetting("timelineFrameRate") or 24)
        pos = 0.0  # Offset from timeline start, in timeline frames.
        for n in notes:
            timeline.AddMarker(int(round(pos)), "Blue", n["label"][:60] or "Cut",
                               n["reason"], 1, "")
            pos += n["frames"] / n["fps"] * tl_fps

    total = sum(n["frames"] / n["fps"] for n in notes)
    log("Built timeline '%s': %d segments, %.1f s." % (name, len(infos), total))
    return name


def save_plan(plan, clips, cfg):
    os.makedirs(cfg["work_dir"], exist_ok=True)
    path = os.path.join(cfg["work_dir"],
                        "edit_%s.json" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"clips": [{"index": i, "name": c["name"], "path": c["path"]}
                             for i, c in enumerate(clips)], "plan": plan}, f, indent=2)
    log("Edit plan saved to %s" % path)


# ---------------------------------------------------------------------------
# Optional dialog (Resolve's built-in UI Manager)
# ---------------------------------------------------------------------------
def show_dialog(cfg):
    """Ask for instructions in a Resolve dialog. Returns False if cancelled.
    Silently keeps the CONFIG defaults if the UI Manager isn't available."""
    g = globals()
    fu, bmd_mod = g.get("fusion"), g.get("bmd")
    if fu is None or bmd_mod is None:
        try:
            fu = get_resolve().Fusion()
            import DaVinciResolveScript as bmd_mod  # noqa: N813
        except Exception:
            return True
    try:
        ui = fu.UIManager
        disp = bmd_mod.UIDispatcher(ui)
    except Exception:
        return True

    win = disp.AddWindow(
        {"ID": "ClaudeAutoEdit", "WindowTitle": "Claude Auto Edit",
         "Geometry": [200, 200, 560, 360]},
        ui.VGroup([
            ui.Label({"Text": "What should Claude do with the selected clips?"}),
            ui.TextEdit({"ID": "Instructions", "PlainText": cfg["instructions"]}),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Target length (s, 0 = auto):"}),
                ui.SpinBox({"ID": "Target", "Minimum": 0, "Maximum": 36000,
                            "Value": cfg["target_seconds"]}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.CheckBox({"ID": "Transcribe", "Text": "Transcribe speech",
                             "Checked": cfg["transcribe"]}),
                ui.CheckBox({"ID": "Frames", "Text": "Send frames",
                             "Checked": cfg["frames_per_clip"] > 0}),
                ui.CheckBox({"ID": "Markers", "Text": "Add markers",
                             "Checked": cfg["add_markers"]}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Cancel", "Text": "Cancel"}),
                ui.Button({"ID": "Run", "Text": "Edit with Claude"}),
            ]),
        ]),
    )
    result = {"run": False}
    items = win.GetItems()

    def on_run(ev):
        cfg["instructions"] = items["Instructions"].PlainText or cfg["instructions"]
        cfg["target_seconds"] = int(items["Target"].Value)
        cfg["transcribe"] = bool(items["Transcribe"].Checked)
        if not items["Frames"].Checked:
            cfg["frames_per_clip"] = 0
        cfg["add_markers"] = bool(items["Markers"].Checked)
        result["run"] = True
        disp.ExitLoop()

    def on_close(ev):
        disp.ExitLoop()

    win.On.Run.Clicked = on_run
    win.On.Cancel.Clicked = on_close
    win.On.ClaudeAutoEdit.Close = on_close
    win.Show()
    disp.RunLoop()
    win.Hide()
    return result["run"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(cfg):
    p = argparse.ArgumentParser(description="Edit Resolve footage with Claude.")
    p.add_argument("--prompt", help="Editing instructions (skips the dialog).")
    p.add_argument("--target-seconds", type=int)
    p.add_argument("--model")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--frames-per-clip", type=int)
    p.add_argument("--no-transcribe", action="store_true")
    p.add_argument("--no-markers", action="store_true")
    p.add_argument("--plan-only", action="store_true",
                   help="Save Claude's edit plan without building a timeline.")
    args, _ = p.parse_known_args()
    if args.prompt:
        cfg["instructions"] = args.prompt
    for k in ("target_seconds", "model", "effort", "frames_per_clip"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)
    if args.no_transcribe:
        cfg["transcribe"] = False
    if args.no_markers:
        cfg["add_markers"] = False
    return args


def main():
    cfg = dict(CONFIG)
    args = parse_args(cfg)
    try:
        resolve = get_resolve()
        project = resolve.GetProjectManager().GetCurrentProject()
        if not project:
            raise RuntimeError("Open a project first.")
        media_pool = project.GetMediaPool()

        if not args.prompt and not show_dialog(cfg):
            log("Cancelled.")
            return

        clips = collect_clips(media_pool)
        if not clips:
            raise RuntimeError("No video clips found. Select clips in the Media Pool "
                               "or open a bin that contains them.")

        content = build_request_content(clips, cfg)
        plan = ask_claude(content, cfg)
        log("Claude's plan: " + plan.get("summary", ""))
        save_plan(plan, clips, cfg)
        if not args.plan_only:
            build_timeline(project, media_pool, plan, clips, cfg)
        log("Done.")
    except Exception as e:
        log("ERROR: %s" % e)


# Resolve injects `resolve` into the script globals when run from Workspace > Scripts.
if __name__ == "__main__" or "resolve" in globals():
    main()
