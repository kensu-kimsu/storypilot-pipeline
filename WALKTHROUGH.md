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
3. **Topic-specific media requests:** the program writes scene-by-scene search requests from the script. With `--discover-assets`, it searches YouTube and public archives, then uses OpenAI web search to find public TikTok links. With `--acquire-assets`, it ranks candidates, downloads a selected social video temporarily with yt-dlp, sends a timestamped contact sheet to OpenAI to select a relevant moment, and uses FFmpeg to save only a muted excerpt up to five seconds. Pixabay and public archives are fallback video sources.
4. **Packaging:** it creates title/description/tag hypotheses and one thumbnail brief naming a recognizable person, brand, or company. With `--make-thumbnails`, StoryPilot searches Wikimedia Commons and other keyless public archives for that subject, saves a matching real image into the reusable picture library, and uses Pillow to compose one 1280x720 thumbnail. This image step uses no paid image-generation API. If no sufficiently relevant source image is found, the thumbnail is left missing and the QA gate reports the reason; it does not substitute an invented face or logo. See `thumbnail_reference.json` for the image source and license details.

The job folder is `D:\YouTubeProduction\jobs\<channel-topic>\`. Expect `research.json`, `script.json`, `asset_requests.json`, `selected_assets.json`, `metadata_ab_test.json`, and `job.json`; downloaded excerpts are stored for reuse in `assets/video_clips/`. Social-video records include source URL, AI-selected in/out timecodes, visual rationale, and a note that original audio was stripped. Any unresolved media is listed in `selected_assets.json` and blocks edit readiness.

If the manifest has unresolved rows, retry them without repeating research or script generation:

```powershell
python main_pipeline.py retry-assets `
  --job-dir "D:\YouTubeProduction\jobs\<channel-topic>" `
  --base-dir "D:\YouTubeProduction" `
  --youtube-audio-library-dir "D:\YouTubeProduction\audio_library_seed"
```

Place YouTube Audio Library files in `audio_library_seed\background_music\` and `audio_library_seed\sfx\` (or use `music\` for the music folder). The retry searches unresolved requests, expands creative prose into simpler media search terms, preserves files already selected, and updates `selected_assets.json`. A match still depends on provider availability and relevance.

For social video candidates, YouTube search uses `YOUTUBE_API_KEY`. TikTok search uses OpenAI web search and can only find pages indexed by the search engine; it is not TikTok's in-app search feed. yt-dlp and FFmpeg must be installed and available to the same Python environment/PATH. The full candidate download is temporary. If a candidate cannot be downloaded or AI finds no visually relevant moment, StoryPilot tries the next candidate and then Pixabay/archive footage. It does not download private/login-gated/DRM-protected videos or use browser cookies.

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

StoryPilot blocks the next preproduction run while a topic remains unfinished. A failed or interrupted job stays locked. Only a passed long-form QA render records the topic as complete in `job.json` and unlocks the next topic; a short-only render does not.

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


## Cost controls and reruns

At the end of a production run, open the job folder and review
`cost_report.json`. It breaks usage down by stage: research, verification,
script, metadata, TikTok search, social candidate ranking, and clip-frame
analysis. Token counts are recorded even when the configured model is absent
from the built-in reference rate card. Check current provider pricing before
using the estimated total as an invoice amount.

When you rerun the same topic with the same channel and prompts, StoryPilot
reuses valid results for research, independent verification, script writing,
and metadata. The console prints a `Resume: reusing cached … result` message
for each reused stage. The cache is stored in `stage_cache.json` alongside
the output JSON files.

To deliberately rerun a stage, add one or more of these names to the workspace
`D:\\YouTubeProduction\\.env`:

```dotenv
STORYPILOT_REFRESH_STAGES=research,verification
```

Supported names are `research`, `verification`, `scripting`, and
`metadata`. Use `*` to refresh all. Remove the setting after that run.

The media library remembers the exact selected file for a scene. If it still
exists, a rerun skips downloading and social-video frame analysis. If you delete
the selected file, StoryPilot searches again. This cache lives in
`assets\\.asset_request_cache.json`.

For a lower-cost packaging trial, keep the global model on your tested model
and set a separate metadata model in the workspace `.env`:

```dotenv
OPENAI_MODEL_METADATA=gpt-6-luna
OPENAI_MODEL_METADATA_FALLBACK=gpt-5
```

The model IDs must be enabled for your API account. The fallback is used only
when the first response is not valid JSON. Run several topics and check titles,
descriptions, and thumbnail briefs before keeping this override. Keep research,
verification, and scripting on the model that passes your fact and writing
quality checks.

Custom image generation is skipped by default. It runs only when the script's
creative director marks a custom background illustration as required. Missing
real photos and evidence imagery stay unresolved for source search or QA rather
than incurring an automatic image-generation charge.
