# StoryPilot production walkthrough

This walkthrough describes how the repository prepares research, narration, media, metadata, and a story-led edit, then renders a local MP4 and runs technical QA. It does **not** upload the video.

## 1. Prepare the workspace and credentials

From the repository folder, install the requirements and create the production folders:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main_pipeline.py setup-apis --base-dir "D:\YouTubeProduction"
```

The setup command creates the workspace folders and prompts for required API credentials. Keep `secrets.json`, `.env`, OAuth files, and private channel details out of Git.

Copy `channels.example.json` to `D:\YouTubeProduction\channels.json`, then fill in each channel's ID, handle, niche, audience, narration settings, and `visual_brand`. Keep brand choices consistent for one channel. For example, keep its primary font and accent colors stable while allowing its scene layouts and topic palette to support each episode.

## 2. Start one documentary topic

To choose a topic yourself and skip YouTube trend discovery:

```powershell
python main_pipeline.py preproduction `
  --channels "D:\YouTubeProduction\channels.json" `
  --base-dir "D:\YouTubeProduction" `
  --channel @yourhandle `
  --topic "The rise and collapse of Example Company" `
  --discover-assets `
  --acquire-assets `
  --make-thumbnails `
  --youtube-audio-library-dir "D:\YouTubeProduction\audio_library_seed"
```

Omit `--topic` to let StoryPilot discover candidate topics, compare them with `history.json`, and prepare one selected idea for the chosen channel. The current command processes one topic per run.

### What preproduction does

1. **Research:** OpenAI gathers sourced factual claims. A separate verification pass checks them and stops the script stage if no claims pass.
2. **Script and creative direction:** the script uses verified claims only, builds a retention arc, and tags each narration chunk with tone, energy, a visual mode, specific evidence to show, rationale, asset searches, music mood, and sound-effect cues.
3. **Topic-specific media requests:** the program writes asset search requests. With `--discover-assets` and `--acquire-assets`, it searches the configured providers, reuses catalogued material where appropriate, and records available source/license metadata.
4. **Packaging:** it creates title/description/tag hypotheses and two thumbnail concepts. `--make-thumbnails` also attempts to create the two image files.

The job folder is `D:\YouTubeProduction\jobs\<channel-topic>\`. Expect `research.json`, `script.json`, `asset_requests.json`, `metadata_ab_test.json`, and `job.json`; asset downloads and generated thumbnails appear when requested and available. Any unresolved media is recorded for follow-up.

## 3. Set up the channel's visual identity once

In `channels.json`, define a stable identity such as:

```json
"visual_brand": {
  "primary_font": "Montserrat",
  "caption_font_weight": "bold",
  "caption_position": "lower_third",
  "caption_positions": ["lower_third", "top_safe", "center_emphasis"],
  "brand_accent": "#FFD34E",
  "brand_secondary": "#C7CEDB",
  "signature_motif": "subtle documentary grain"
}
```

The brand is the recurring visual anchor. The episode palette responds to the topic, and the scene mode responds to the evidence and tone. For example, a document can use an angled document treatment with a restrained highlight; a verified route can use a map; a pause after a revelation can hold a minimalist text card. The scene should justify the treatment.

## 4. Create narration and timed captions

Synthesize narration using Azure Speech, Fish Speech, or the local Chatterbox bridge selected in `.env`:

```powershell
python azure_tts.py `
  "D:\YouTubeProduction\jobs\job-name\script.json" `
  --out-dir "D:\YouTubeProduction\jobs\job-name" `
  --base-dir "D:\YouTubeProduction" `
  --assets-manifest "D:\YouTubeProduction\jobs\job-name\selected_assets.json"
```

The step uses chunk emotion and delivery cues to guide TTS. It writes narration audio, a manifest, word timings, captions, and `timed_edit_job.json`. Check narration duration and caption alignment before editing. Local Fish and Chatterbox word timings are approximate in the current integration. See README's Chatterbox setup section to install and start its local bridge.

The long-form script targets at least 20 minutes. The preproduction stage also writes a standalone 30-second Shorts script. Create timed narration jobs for each format you want rendered; a `short_form` job must have a 30-second narration and a 9:16 canvas.

## 5. Build the edit and render the MP4

Run the edit stage after the narration and required assets exist:

```powershell
python main_pipeline.py edit `
  "D:\YouTubeProduction\jobs\job-name\timed_edit_job.json" `
  --out "D:\YouTubeProduction\jobs\job-name\edit_handoff" `
  --render-dir "D:\YouTubeProduction\rendered_outputs"
```

Install FFmpeg and ensure `ffmpeg` and `ffprobe` are on `PATH`. The default `--encoder auto` uses CPU (`libx264`) for compatibility. Use `--encoder nvenc` only if your FFmpeg build and NVIDIA driver support it.

Before creating a handoff, StoryPilot checks referenced local assets and captions. If required items are missing, it writes `edit_readiness.json` and stops. When ready, it creates:

- `edit_readiness.json` — required files and ready/blocked status.
- `timeline.json` — per-shot timing, mode, story evidence, visual rationale, palette, caption placement, typography treatment, motion, transition, and audio cues.
- `assistant_edit.fcpxml` — editable rough-cut timeline with narration and available visuals.
- `EDIT_NOTES.md` — human-readable shot list and optional focus points.
- `job_manifest.json` — input job snapshot used to make this handoff.
- `D:\YouTubeProduction\style_history.json` — recent per-channel style signatures for future plans.
- `rendered_outputs\<job>_long.mp4` or `<job>_short.mp4` — final local MP4 after QA passes.
- `rendered_outputs\<job>_<format>_qa.json` — measured duration, streams, canvas, diversity proxy, and QA status.

Style history nudges automatic choices away from recently common scene modes while the story director's explicit mode takes priority. It is a diversity aid, not a monetization guarantee. Re-running the same job replaces its history record instead of adding a duplicate. Use `--style-history "D:\somewhere\style_history.json"` to choose another history file.

## 6. What rendering and QA cover

The pre-edit gate checks that required narration, English captions, and referenced media files exist and that referenced video assets have duration metadata. The renderer burns in captions, applies shot transitions and text overlays, adds slow zoom motion to stills, and mixes the narration with available background music/SFX. Pillow creates evidence-backed charts, counters, and schematic routes. Optional Cartopy maps use independently checked coordinates and Natural Earth features. Optional OCR highlights phrases it finds in the selected document image. Optional rembg cutouts are composited over separate background plates with independent movement. The renderer creates a temporary MP4, probes the result, and gives it the final filename only when technical checks pass. Failed output is retained as `<job>_<format>_qa_failed.mp4` with a report for troubleshooting.

QA checks container/streams, planned duration, minimum length (20 minutes for long-form and about 30 seconds for Shorts), canvas, 9:16 Shorts ratio, and a basic visual-diversity proxy. It cannot judge whether a story is boring or guarantee click-through or retention; those need audience analytics and creative evaluation. The generated edit is not uploaded. Maps, OCR, and portrait cutouts require their optional packages; if unavailable or if evidence/assets are missing, the affected render fails instead of silently substituting an inaccurate graphic.

## 7. Expected outputs

| Requested result | Current status |
|---|---|
| Verified research and story-specific narration | Prepared in the job folder |
| Expressive English narration and timed captions | Prepared through Azure, Fish Speech, or Chatterbox |
| Downloaded/reused visual and audio assets | Attempted from configured sources; unresolved items are reported |
| Channel-branded, story-led shot plan | Written to timeline JSON, notes, and FCPXML handoff |
| 20-minute final MP4 | Rendered from a long-form timed job after QA passes |
| 30-second 9:16 Shorts MP4 | Rendered from a separate short-form timed job after QA passes |
| Burned-in English subtitles, transitions, text treatments, still-image motion, available music/SFX mix | Applied by local FFmpeg render |
| Technical final-video QA | Writes a QA report; failed files are marked `_qa_failed.mp4` |
| Evidence-led maps, document highlights, and portrait parallax | Optional; requires separately installed Python packages/Tesseract and the specified checked coordinates or assets |
| YouTube upload | Not performed; the program has no upload stage |

The final output is a local MP4 and its QA report. Review the report and video, then upload manually when ready.
