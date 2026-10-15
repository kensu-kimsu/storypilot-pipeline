# StoryPilot

StoryPilot is a Python-assisted production workflow for fact-led YouTube documentaries and Shorts. It prepares a topic, research ledger, scripts, asset manifest, reusable media downloads, thumbnail concepts, metadata, and an editable timeline handoff.

## Current implementation boundary

This repository automates **pre-production, Azure or local Fish Speech/Chatterbox narration and caption preparation, media acquisition, story-led editing, and local MP4 rendering**. For video scenes, it can search YouTube with the Data API and search TikTok links through OpenAI web search, download selected candidates with yt-dlp, send a timestamped contact sheet to OpenAI for visual moment selection, then use FFmpeg to retain a muted excerpt of up to five seconds. The `edit` command checks asset readiness, retries missing manifest assets through configured sources, writes `edit_readiness.json`, and blocks if required files remain missing. It renders an MP4, runs technical QA, and saves a QA report. It does not upload to YouTube or start/read YouTube's native thumbnail experiment.

## Story-led variable editing

StoryPilot keeps channel identity stable and lets the verified story guide episode and scene choices. The style planner does not rotate cosmetic presets just to make videos look different.

1. **Channel identity:** set a stable font, brand accent colors, caption safe zones, and signature motif in each channel's `visual_brand` object in `channels.json`.
2. **Episode direction:** the research-backed script asks for a visual mode, the evidence to show, and why that treatment fits each narration chunk. Topic cues select an episode palette while the channel brand remains available as its consistent anchor.
3. **Scene direction:** each timed segment can select from `minimalist_text`, `split_screen`, `animated_chart`, `document_focus`, `animated_map`, `parallax_portrait`, `kinetic_fact`, and `cinematic_broll`. If the director does not specify a valid mode, StoryPilot chooses within the tone-appropriate options.
4. **Controlled variation:** cut ranges, camera movement, caption zones, type treatments, transitions, and sound cues respond to the scene tone and energy. Captions retain the configured channel font; their safe-zone placement can move to suit the layout.
5. **Across-video memory:** the edit command reads and updates `style_history.json` in the production workspace. It tracks recent scene modes per channel and nudges automatic choices away from overused modes. A scene-specific mode from the creative director takes priority over that nudge.

This history helps the edit plan avoid falling into the same visual rhythm. It is not a monetization bypass or a guarantee of YouTube Partner Program approval. Original research, meaningful narration and analysis, topic-specific evidence, and viewer value still matter. The renderer applies captions, text treatments, image motion, transitions, selected music/SFX, and evidence-checked graphics, including optional coordinate maps, OCR document highlights, and layered portrait cutouts.

### Channel style configuration

Add a `visual_brand` object to each entry in `channels.json`. `channels.example.json` includes a starter profile:

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

The edit package records this identity and each shot's topic palette, visual mode, evidence/rationale, caption position, and text treatment. The MP4 renderer applies the selected font and text overlay where available. Palette selection is a topic heuristic with an optional job-level override.

### Edit planning command

```powershell
python main_pipeline.py edit `
  "D:\YouTubeProduction\jobs\job-name\timed_edit_job.json" `
  --out "D:\YouTubeProduction\jobs\job-name\edit_handoff" `
  --render-dir "D:\YouTubeProduction\rendered_outputs"
```

The default `auto` uses CPU encoding for reliable compatibility. Use `--encoder nvenc` only when FFmpeg and an NVIDIA driver support NVENC.

The command automatically uses `D:\YouTubeProduction\style_history.json` based on the timed job's workspace location. To use a different history file, add `--style-history "D:\path\to\style_history.json"`. History records contain channel key, job ID, topic, timestamp, and a compact style signature; they do not store scripts, credentials, or media.

## Asset sources

| Source | Key needed? | What StoryPilot uses it for |
|---|---:|---|
| Pixabay | Yes, `PIXABAY_API_KEY` | Candidate stock photos and video clips. API search results are cached for 24 hours. |
| Wikimedia Commons | No | Public file search; image and video file candidates plus creator/license metadata. |
| Internet Archive | No | Search historic stills and video records, inspect item metadata, and download a matching file when available. |
| Library of Congress | No | Historic photo collection search and image downloads. |
| NASA Image and Video Library | No | NASA image/video searches and direct media files; most useful for space, Earth science, and aerospace topics. |
| Official company newsroom/media pages | No API key | Best place to find accurate current company/product visuals. OpenAI web research can identify likely official pages; add the chosen file/page to the asset manifest. Automated download from arbitrary company sites is not enabled. |
| YouTube | Yes, `YOUTUBE_API_KEY` | Searches topic ideas and scene-specific candidate video URLs; yt-dlp can download a selected public candidate temporarily for analysis and clipping. |
| TikTok | No TikTok API key; uses `OPENAI_API_KEY` web search | Searches web-indexed public TikTok links for scene candidates. Coverage is limited and does not reproduce TikTok's in-app recommendations. |
| yt-dlp | Installed from `requirements.txt` | Downloads a selected candidate temporarily. StoryPilot retains only the AI-selected excerpt (muted, up to five seconds) in `assets/video_clips/`. |
| YouTube Audio Library | No API key | Import tracks that you download yourself into the local `audio_library_seed` folders. |
| Freesound | Yes, `FREESOUND_API_KEY` | Optional CC0-filtered music/SFX preview search. Preview files are not original-quality downloads. |
| OpenAI image generation | Uses `OPENAI_API_KEY` | Optional illustrative scene stills. Thumbnail generation does not call the image API; it uses a real downloaded subject image and Pillow composition. |

The extra no-key sources are **collection-specific archives**, not a universal Google Images replacement. For a named modern company, its official press/media page is usually the most accurate source. When making a thumbnail, StoryPilot searches for the brief's named person, brand, or company in local reusable pictures and then keyless public archives. It places that real image directly in the thumbnail and records its source, creator, and license in `thumbnail_reference.json`; it does not ask an image model to imitate the subject. Wikimedia's public API does not require an API key, while each file's own license and attribution terms still need to be checked. The program records source URLs, creators, selected timecodes, and available source metadata in the asset catalog. TikTok web search finds indexed links, not every TikTok post; its official Research API is restricted to eligible researchers and is not used by StoryPilot. Downloaded social-video audio is stripped so narration and the local music/SFX mix remain under StoryPilot's control.

## 1. Install and configure

Use Python 3.10 or later. From the repository folder, create and activate a virtual environment, then run the dependency installer/checker:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
python main_pipeline.py check-deps --install --base-dir "D:\YouTubeProduction"
```

macOS/Linux:

```bash
source .venv/bin/activate
python main_pipeline.py check-deps --install --base-dir ./production
```

`check-deps` first creates the workspace and prompts for missing API keys required by the selected stages. It saves entered keys in the production workspace's `secrets.json`, so later runs reuse them. Optional provider keys are offered once during initial setup and can be skipped. It then checks Python, FFmpeg/ffprobe, credentials, and stage requirements. Add `--install` to install the core Python packages into the active environment; without it, dependency checking is read-only apart from first-run workspace/key setup. By default it checks the complete workflow (topic discovery, Azure narration, and editing); use `--stage edit` for a render-only machine, `--stage narration` for voice setup, or `--stage preproduction --topic-provided` when you will supply a topic instead of discovering one.

Optional features are deliberately opt-in because some have large downloads or platform-specific setup:

```bash
python main_pipeline.py check-deps --install --features maps,ocr,portraits,news --base-dir ./production
```

Feature mapping: `maps` installs Matplotlib and Cartopy; `ocr` installs pytesseract and checks for the separate Tesseract OCR executable; `portraits` installs CPU rembg (model weights download the first time a cutout is generated); `news` installs Playwright and Chromium for article-headline screenshots. FFmpeg and Tesseract are system programs, so the checker prints platform-specific FFmpeg guidance and a separate Tesseract reminder when they are missing. Optional media API keys are warnings unless their specific feature requires them.

The `--install` option uses the Python interpreter running the checker (`python -m pip`) and does not install system packages. On Windows, install FFmpeg separately and ensure both `ffmpeg.exe` and `ffprobe.exe` are on `PATH`. The workspace `.env` file is loaded by the pipeline; `secrets.json` and process environment variables are also supported. Do not commit either secret file.

Create the local workspace and save your API keys once:

```bash
python main_pipeline.py setup-apis --base-dir "D:\YouTubeProduction"
```

For macOS/Linux, use a path such as `./production` instead. Key entry is hidden. Values are saved in `secrets.json`, which is excluded from Git but is **not encrypted**. Keep that file private.

| Key | Needed when |
|---|---|
| `OPENAI_API_KEY` | Required for research, script/metadata generation, and optional images. |
| `YOUTUBE_API_KEY` | Needed for trend/topic discovery and YouTube scene-video search. TikTok web search uses the OpenAI key. |
| `PIXABAY_API_KEY` | Optional; enter your key to include Pixabay stock candidates/downloads. |
| `FREESOUND_API_KEY` | Optional; enables the CC0-filtered Freesound audio preview search. |
| `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION` | Required only to synthesize narration. Store them in the production workspace `secrets.json` or environment. |

Google result pages are not scraped. Google's policy treats automated queries and scraping of Search results without permission as prohibited machine-generated traffic. OpenAI web search is used to find an article only when the AI creative brief explicitly requests a headline visual.

Internet Archive, Wikimedia Commons, Library of Congress, and NASA Image and Video Library need no key for the public searches used by this project. Google CSE fields have been removed from setup.

The command creates the workspace folders, including `assets/pictures/`, `assets/video_clips/`, `assets/generated_media/`, `assets/background_music/`, `assets/sfx/`, `jobs/`, `discovery/`, `rendered_outputs/`, `temp_assets/`, and `logs/`.

## 2. Configure your channels

Copy `channels.example.json` to `channels.json` and replace the example channel details with your five channels. Each entry should include a real YouTube channel ID, handle, niche, audience, and desired narration tone. Keep your private channel data out of Git if you prefer.

## 3. Build a production package

To produce one chosen topic and search/download assets in the same run:

```powershell
python main_pipeline.py preproduction `
  --channels channels.json `
  --base-dir "D:\YouTubeProduction" `
  --channel @yourhandle `
  --topic "The rise and collapse of Example Company" `
  --discover-assets `
  --make-thumbnails `
  --acquire-assets `
  --youtube-audio-library-dir "D:\YouTubeProduction\audio_library_seed"
```

On macOS/Linux, use the same arguments on one line or replace the PowerShell backticks with shell line continuations. You can omit `--channel` to use the first channel in the JSON file.

To let the program research recent popular YouTube videos and choose the first non-duplicate idea, omit `--topic`:

```bash
python main_pipeline.py preproduction --channels channels.json --base-dir ./production --channel @yourhandle --discover-assets --make-thumbnails --acquire-assets
```

When the creative brief requests a news headline screenshot, StoryPilot uses Playwright/Chromium. Install the browser once so the conditional capture step is ready:

```bash
pip install playwright
python -m playwright install chromium
```

The script writer normally sets `news_article_screenshot` to `null`. It requests at most two article headlines only when a real headline materially helps a scene. Only then does Python search established news publishers with OpenAI web search, open each selected article, highlight requested topic keywords in yellow, and screenshot the visible headline (`h1`) only. If the creative brief requests none, no extra news search runs and `assets/screenshots/` is not created. Paywalls, blocked pages, missing headlines, and missing keyword matches are recorded as failures rather than replacing the requested visual with an unrelated screenshot.

Images are saved under `assets/screenshots/<job-name>/` with topic, publisher, and headline slugs in the filename. `article_screenshots.json` records the exact headline, source URL, highlighted terms, capture time, and status. This step does not scrape Google results or bypass site restrictions.

Topic discovery and YouTube scene-video search need `YOUTUBE_API_KEY`. YouTube's public counters do not provide competitor retention or watch-time data. The project processes one selected topic per run. It reads `history.json` to avoid repeats, but the current CLI does not automatically write completed topics to that file; the `record_history()` helper must be called by a later publishing/completion step.

**Social-video search/download behavior:** YouTube candidates come from the Data API; TikTok candidates come from OpenAI web search, so coverage depends on public pages indexed by search engines. yt-dlp receives a selected public video URL and does not use browser cookies or attempt private, login-gated, or DRM-protected downloads. The original full video is temporary and deleted after the excerpt is saved. Search may return no suitable clip; the acquisition report records the miss and continues to configured stock/archive sources. Platform terms and rights still apply in a non-monetized test.

## 4. What each stage does and writes

| Stage | What the program does | Expected output |
|---|---|---|
| Workspace/setup | Creates directories and loads keys from environment or `secrets.json`. | Workspace folders; local `secrets.json`; no media yet. |
| Topic discovery (unless `--topic` is supplied) | Searches YouTube for recent videos in the channel niche, ranks public results, asks OpenAI for five distinct ideas, and filters titles/topics against history. | `discovery/<channel>.json` and `discovery/ideas.json`. |
| Research | Uses OpenAI web search to assemble factual claims, source URLs, support notes, confidence labels, and unresolved questions. | Job folder `research.json`. Each factual claim should link to a source. |
| Independent fact check | A second OpenAI web-search pass checks each claim against cited and corroborating sources. Only verified or explicitly qualified claims with source URLs are passed to script writing; if none pass, script generation stops. | `research.json` includes `verification_summary`, checked sources, and `verified_claims`. |
| News headline screenshots (only when requested by the creative brief) | Searches approved major news publishers, captures only the visible article headline, and highlights matching topic terms in yellow. If no scene requests one, it performs no additional news article search. | `assets/screenshots/<job-name>/*.png` and `article_screenshots.json`. |
| Script and visual direction | Writes long-form narration chunks with claim IDs, tone/energy, a story-specific visual mode, evidence to show, and rationale, plus a complete short-form script. | `script.json`. The word-count estimate is only a duration proxy; measure actual narration time before editing. |
| Narration preparation | Synthesizes each chunk with Azure Speech, local Fish Speech S2, or the local Chatterbox bridge, applies delivery direction, and creates English captions. | `narration.wav`, `narration_manifest.json`, `word_timestamps.json`, `captions.srt`, and `timed_edit_job.json`. Azure supplies word boundaries; local providers use approximate word timing. |
| Niche creative direction | Loads the matching entry from `niche_prompts.json` for source priorities, story framing, retention beats, and narration delivery. | Five starter profiles: cybersecurity, finance, business, dark history, and future technology. |
| Asset planning | Converts each scene's visual, music, and SFX needs into provider search requests. Video requests carry the five-second maximum. | `asset_requests.json`, including query, scene, purpose, and candidate-source list. |
| Asset discovery (`--discover-assets`) | Searches YouTube for scene-specific candidate videos, uses OpenAI web search for public TikTok links, and searches no-key Commons, Internet Archive, Library of Congress, and NASA collections. Social discovery is capped by `SOCIAL_VIDEO_SEARCHES_PER_RUN` (default 10). | `asset_requests.json` updated with candidate URLs, titles, and source details. |
| Asset acquisition (`--acquire-assets`) | Reuses matching catalogued video first, then ranks social candidates. yt-dlp downloads one candidate temporarily; OpenAI inspects a timestamped contact sheet and marks a relevant moment; FFmpeg cuts and re-encodes a muted excerpt no longer than five seconds. If social candidates fail, StoryPilot tries Pixabay and video-capable public archives. It never substitutes a generated still for missing footage; misses remain unresolved and block pre-edit readiness. | `selected_assets.json`; retained excerpts in shared `assets/video_clips/`; reusable files in shared `assets/` folders; SQLite catalog and JSON sidecars. Social records include source URL, chosen in/out timecodes, selection rationale, and that source audio was excluded. Unavailable clips are listed under `unresolved`. |
| Metadata/thumbnail | Writes title hypotheses, description, tags, and one thumbnail brief naming a recognizable story subject. With `--make-thumbnails`, searches no-key public image archives for a matching real person/company image, stores it in the reusable `assets/pictures/` catalog, then composes one 1280x720 thumbnail locally with Pillow. It does not call paid image generation. | `metadata_ab_test.json`, `thumbnail_1.png`, and `thumbnail_reference.json` with source, creator, and license metadata. Missing or weakly matched references are reported and fail thumbnail QA instead of producing a made-up subject. |
| Pre-edit asset check and edit plan | Verifies referenced files, retries missing manifest assets through configured sources, and refuses to render if required assets or captions remain missing. Applies channel style, per-scene direction, and recent per-channel style history. | `edit_readiness.json`; FCPXML, `timeline.json`, `EDIT_NOTES.md`, and updated workspace `style_history.json`. |
| Final MP4 render and technical QA | Renders locally with FFmpeg; burns English captions, applies shot transitions and text treatments, animates verified chart/counter/route specs, adds slow motion to still images, mixes available narration/music/SFX, and checks duration, audio/video streams, canvas, aspect ratio, and a basic visual-diversity proxy. No upload action exists. | `<job>_long.mp4` or `<job>_short.mp4` in `rendered_outputs/`, plus a QA JSON report. Failed QA leaves a `_qa_failed.mp4` and report. |

Each job is written to `jobs/<channel-topic>/`. Typical files are `research.json`, `script.json`, `asset_requests.json`, `selected_assets.json` (when acquisition is run), `metadata_ab_test.json`, optional thumbnail PNGs, and `job.json`.

`niche_prompts.json` is the project's editable creative-direction file. Add a profile whose key matches the channel's `niche`; it can define `research_prompt`, `system_prompt`, `retention_guidance`, `narration_direction`, `voice_name`, and `default_search_fallback`. The script prompts for a truthful opening, new evidence or stakes through the story, meaningful re-hooks, and a resolved ending. Each narration chunk has tone, emotion, pace, intensity, and pause guidance. `azure_tts.py` turns those cues into speech and outputs word timings and `captions.srt`. Azure MAI remains the default unless `NARRATION_PROVIDER` is changed. Local providers estimate word timings, so inspect captions before editing.

For MAI Voice 2.1, create a Speech resource in a supported region. Copy `.env.example` to your production workspace as `.env`, set `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION`, or enter them when prompted; keep real credentials out of Git. `AZURE_TTS_VOICE` can be used in `.env` to try another voice.

The uploaded `queue.json` is not currently consumed by the CLI. Its order can define the intended channel sequence for a future queue runner, but this version processes one selected channel/topic per run. The existing OAuth token filenames can be used by a later YouTube uploader; this version has no upload stage. Keep token JSON files and OAuth client secrets local, out of Git, and referenced only by filename in private channel configuration.

## 5. Music and sound effects

The workflow can index locally downloaded YouTube Audio Library tracks. Put your files in:

```text
D:\YouTubeProduction\audio_library_seed\background_music\
D:\YouTubeProduction\audio_library_seed\sfx\
```

The pipeline does not automatically download from YouTube Audio Library. Freesound is the optional API-backed fallback, using CC0-filtered MP3 previews. Pixabay's documented API searches photos and videos; it is not used here as an audio API. Keep track-specific attribution and rights notes with imported audio.

## 6. Edit handoff

The edit stage expects a JSON job that includes timed narration segments, paths to approved local assets, and an English SRT caption file. See `example_job.json` for the shape. Install FFmpeg and make `ffmpeg` and `ffprobe` available on `PATH`. Then run:

```bash
python main_pipeline.py edit path/to/timed_edit_job.json --out "D:\YouTubeProduction\jobs\example\edit_handoff" --render-dir "D:\YouTubeProduction\rendered_outputs"
```

The command writes the story-led shot plan and FCPXML, then renders an MP4 locally. A long-form plan must be at least 20 minutes. A `short_form` plan must be 30 seconds and 9:16; render a separate timed job for the short script. English SRT captions are burned into the picture by default. The creative director may request evidence-backed charts, counters, schematic routes, optional coordinate maps, OCR document highlights, and layered portrait cutouts. Python checks chart/map/document data against the scene's verified claims before Pillow and FFmpeg render them. Routes are labelled not to scale; geographic maps note projection distortion. Technical QA blocks the final filename when checks fail. QA measures format, duration, streams, and edit-diversity proxies, but cannot determine whether a story is engaging or guarantee retention. The command never uploads.

See [WALKTHROUGH.md](WALKTHROUGH.md) for the full production walkthrough, output list, and the current boundary between automated planning and final rendering.

### Narration with Azure Speech, Fish Speech, or Chatterbox

Run pre-production with asset acquisition, then synthesize the script and create a timed edit job:

```bash
python main_pipeline.py preproduction --channels channels.json --base-dir "D:\\YouTubeProduction" --channel @yourhandle --topic "Your researched topic" --acquire-assets
python azure_tts.py "D:\\YouTubeProduction\\jobs\\job-name\\script.json" --out-dir "D:\\YouTubeProduction\\jobs\\job-name" --base-dir "D:\\YouTubeProduction" --assets-manifest "D:\\YouTubeProduction\\jobs\\job-name\\selected_assets.json"
python main_pipeline.py edit "D:\\YouTubeProduction\\jobs\\job-name\\timed_edit_job.json" --out "D:\\YouTubeProduction\\jobs\\job-name\\edit_handoff"
```

For Azure Speech, use the existing voice defaults and set your Speech key and region in the workspace `secrets.json` or `.env`. For local Fish Speech or Chatterbox, run the model server separately; keep it in a dedicated WSL/Linux environment so its PyTorch/CUDA dependencies do not conflict with StoryPilot.

#### Local Chatterbox setup on Windows

StoryPilot's Chatterbox provider uses the official English `ChatterboxTTS` model through the small `chatterbox_server.py` bridge included in this repository. The bridge stays on `127.0.0.1:8090`; StoryPilot calls it for each narration chunk. No TTS API key or per-character fee is needed. The upstream project documents Python 3.11 and `pip install chatterbox-tts`; it uses a consented reference WAV for voice prompting. Its expressive controls adjust emphasis and cadence, but do not guarantee exact emotion acting. Chatterbox adds an inaudible watermark to generated audio. See the [official Chatterbox repository](https://github.com/resemble-ai/chatterbox).

1. In **PowerShell as Administrator**, install WSL2 Ubuntu if it is not already installed, then restart if prompted:

   ```powershell
   wsl --install -d Ubuntu-24.04
   ```

2. Open Ubuntu from the Start menu, create its Linux username/password, and check that WSL can see your NVIDIA GPU:

   ```bash
   nvidia-smi
   ```

   If this command fails, update the Windows NVIDIA driver and confirm WSL GPU support before continuing. StoryPilot has a separate Windows Python environment; do not install Chatterbox into that environment.

3. In Ubuntu, install Miniforge and create a dedicated Python 3.11 environment:

   ```bash
   sudo apt update
   sudo apt install -y git curl ffmpeg
   curl -L -o Miniforge3.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
   bash Miniforge3.sh
   ```

   Follow the installer prompts, close/reopen Ubuntu, then run:

   ```bash
   conda create -n chatterbox python=3.11 -y
   conda activate chatterbox
   pip install chatterbox-tts
   python -c "import torch; print('CUDA available:', torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
   ```

   Continue with `--device cuda` only if the last command prints `CUDA available: True`. If it reports `False`, check the current [PyTorch install selector](https://pytorch.org/get-started/locally/) for the matching Linux CUDA wheel, install it in this `chatterbox` environment, and run the check again. CPU mode can test the connection, but will take longer to synthesize long narration.

4. Record a clean 5–10 second WAV sample for the narrator. Use your own voice or a voice you have permission to use. Save it under `D:\YouTubeProduction\assets\narrator_reference.wav`. WSL sees that file at `/mnt/d/YouTubeProduction/assets/narrator_reference.wav`.

5. In Ubuntu, start StoryPilot's local Chatterbox bridge from the cloned project directory. It loads the model on first start, which can take a while and download model weights:

   ```bash
   cd /mnt/d/YouTubeProduction/storypilot-pipeline
   conda activate chatterbox
   python chatterbox_server.py --device cuda --reference-audio /mnt/d/YouTubeProduction/assets/narrator_reference.wav
   ```

   Leave this terminal open. The server binds only to localhost and has no authentication, so do not change `--host` to `0.0.0.0` on a networked machine.

6. In a new Windows PowerShell window, check the service:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8090/v1/health
   ```

   The response should show `status: ok`, `provider: chatterbox`, and `device: cuda`. If Windows cannot reach the WSL service through localhost, check that WSL's localhost forwarding is enabled before changing the bind address.

7. Edit `D:\YouTubeProduction\.env` and preserve your other settings. Set:

   ```dotenv
   NARRATION_PROVIDER=chatterbox
   CHATTERBOX_URL=http://127.0.0.1:8090
   ```

   No Azure Speech key or region is required for narration when Chatterbox is selected. Run the StoryPilot health check from PowerShell:

   ```powershell
   cd D:\YouTubeProduction\storypilot-pipeline
   python main_pipeline.py check-deps --stage narration --base-dir D:\YouTubeProduction
   ```

8. Generate a narration test from a prepared job. Replace `job-name` with the real job folder:

   ```powershell
   python azure_tts.py "D:\YouTubeProduction\jobs\job-name\script.json" --out-dir "D:\YouTubeProduction\jobs\job-name" --base-dir "D:\YouTubeProduction" --assets-manifest "D:\YouTubeProduction\jobs\job-name\selected_assets.json" --provider chatterbox
   ```

   This writes `narration.wav`, `captions.srt`, `word_timestamps.json`, `narration_manifest.json`, and `timed_edit_job.json`. Chatterbox does not return word alignments, so caption timing is estimated; listen to the audio and inspect the captions before rendering. The bridge maps each chunk's `delivery_emotion`, `tone`, `delivery_pace`, and `vocal_intensity` to Chatterbox's exaggeration and CFG controls. Pace is approximate; listen to the first test and tune the mapping in `chatterbox_server.py` if needed.

To stop the local model, press **Ctrl+C** in its Ubuntu terminal. Start it again before the next narration run.

#### Local Fish Speech setup on Windows

The current Fish Audio S2 instructions target Linux or WSL and recommend a GPU with at least 24 GB VRAM for inference. Windows users should use WSL2/Ubuntu or Docker with NVIDIA GPU passthrough. A CPU mode exists, but it may be too slow for practical long-form production. Check the Fish Audio requirements before downloading the large model weights: [installation guide](https://speech.fish.audio/install/) and [inference guide](https://speech.fish.audio/inference/).

In WSL Ubuntu, install the separate Fish environment (choose the CUDA extra compatible with your NVIDIA driver; upstream currently documents `cu126`, `cu128`, and `cu129`):

```bash
sudo apt update
sudo apt install -y git ffmpeg libsox-dev portaudio19-dev
git clone https://github.com/fishaudio/fish-speech.git ~/fish-speech
cd ~/fish-speech
conda create -n fish-speech python=3.12 -y
conda activate fish-speech
# Example only: use cu126/cu128/cu129 as supported by your driver.
pip install --extra-index-url https://download.pytorch.org/whl/cu128 -e ".[cu128]"
hf download fishaudio/s2-pro --local-dir checkpoints/s2-pro
python tools/api_server.py \
  --llama-checkpoint-path checkpoints/s2-pro \
  --decoder-checkpoint-path checkpoints/s2-pro/codec.pth \
  --listen 0.0.0.0:8080
```

Leave that terminal running. In Windows PowerShell, check that the local server is reachable:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/health
```

The expected response is `status: ok`. If Windows cannot reach WSL through localhost forwarding, use the WSL IP address in `FISH_SPEECH_URL` instead.

For a consistent narrator across the script's separate narration chunks, prepare a short clean WAV reference and a UTF-8 text file containing the exact spoken words. Fish uses that pair as voice conditioning for each chunk. Use your own voice or a voice you have explicit permission to use. Put the files somewhere persistent, for example:

```text
D:/YouTubeProduction/assets/narrator_reference.wav
D:/YouTubeProduction/assets/narrator_reference.txt
```

Edit `D:\YouTubeProduction\.env` (copy `.env.example` there first) and set:

```dotenv
NARRATION_PROVIDER=fish
FISH_SPEECH_URL=http://127.0.0.1:8080
FISH_SPEECH_REFERENCE_AUDIO=D:/YouTubeProduction/assets/narrator_reference.wav
FISH_SPEECH_REFERENCE_TEXT_FILE=D:/YouTubeProduction/assets/narrator_reference.txt
```

Alternatively, set `FISH_SPEECH_REFERENCE_ID` to a reference already installed in the Fish server and leave the two reference-file settings blank. Do not set both reference methods at once. No Azure credentials are needed for Fish. The existing synthesis command then works with `--provider fish`:

```powershell
python azure_tts.py "D:\YouTubeProduction\jobs\job-name\script.json" --out-dir "D:\YouTubeProduction\jobs\job-name" --base-dir "D:\YouTubeProduction" --assets-manifest "D:\YouTubeProduction\jobs\job-name\selected_assets.json" --provider fish
```

The script's emotion and tone fields are translated to Fish's inline direction tags. Fish's local endpoint does not return word-alignment timestamps, so the pipeline currently estimates Fish caption times from word length and audio duration; inspect `captions.srt` before editing. The narration manifest marks these timings as `approximate`.

**License check before publishing:** Fish Speech's source and weights use the Fish Audio Research License. Fish Audio describes commercial self-hosting as requiring a separate commercial license. A monetized YouTube channel may count as commercial use, so confirm the license with Fish Audio before publishing content made with the local weights. See the [official repository license notice](https://github.com/fishaudio/fish-speech#license-notice) and [Fish Audio developer page](https://fish.audio/developers/).

Audition a short sample before generating a full episode, and listen to the WAV and inspect the SRT. Azure MAI voice styles are model-specific; Fish receives mapped inline directions such as curious, near-whisper, fearful, and determined. Neither system guarantees a human performance or audience retention: listen to the output and adjust the script, pacing, emotion directions, and mix.

## 7. A/B testing and publishing

`metadata_ab_test.json` contains packaging alternatives; it does not start an experiment. YouTube's native thumbnail/title experiment must be started and reviewed through YouTube Studio unless a supported integration is added later. Sequentially changing a title or thumbnail is not a controlled concurrent A/B test. This version does not publish videos or switch them from private to public.

## Tests

From the repository folder:

```bash
python -m unittest discover -s tests -v
```

## Optional evidence graphics: maps, documents, and 2.5D portraits

Install only the add-ons you intend to use:

```powershell
pip install -r requirements-maps.txt
pip install -r requirements-ocr.txt
pip install -r requirements-portraits.txt
```

- **Geographic maps:** research records exact coordinates with direct source URLs. The independent verifier must confirm the same coordinates before the script may use them. Cartopy draws Natural Earth coastlines/borders; first use may download its boundary dataset. Frames identify projection distortion, so use verified chart/text for exact measurements.
- **Document highlights:** install `requirements-ocr.txt` and the separate Tesseract OCR executable with English language data. A `document_highlight` spec supplies a claim-linked phrase; OCR must find its words in the selected image before Pillow marks the detected word boxes. A miss stops the render.
- **2.5D portraits:** install `requirements-portraits.txt`. The timed job must set a portrait image as `parallax_foreground_asset_id` and a distinct picture as the shot's `asset_id` background plate. rembg extracts the transparent foreground; FFmpeg moves it separately over the provided background. rembg does not reconstruct the original scene behind a removed subject, so the original image cannot also be the backdrop. First use downloads model weights.

These packages are optional. Install them in the project environment and confirm compatibility with the installed Python version. Tesseract is a separate executable, not a pip package. See the [Cartopy installation guide](https://cartopy.readthedocs.io/v0.26.0/installing.html), [Cartopy map feature guide](https://cartopy.readthedocs.io/latest/matplotlib/feature_interface.html), [pytesseract prerequisites](https://github.com/madmaze/pytesseract#installation), and [rembg usage and model notes](https://github.com/danielgatis/rembg).
