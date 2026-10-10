# StoryPilot

StoryPilot is a Python-assisted production workflow for fact-led YouTube documentaries and Shorts. It prepares a topic, research ledger, scripts, asset manifest, reusable media downloads, thumbnail concepts, metadata, and an editable timeline handoff.

## Current implementation boundary

This repository automates **pre-production, Azure narration/caption preparation, media acquisition, and the edit handoff**. The `edit` command now checks asset readiness, retries missing manifest assets through configured sources, writes `edit_readiness.json`, and blocks if required files remain missing. It still does not render a finished MP4, upload to YouTube, or start/read YouTube's native thumbnail experiment. The FCPXML is an assistant-editor handoff, not a completed or QA-passed video.

## Asset sources

| Source | Key needed? | What StoryPilot uses it for |
|---|---:|---|
| Pixabay | Yes, `PIXABAY_API_KEY` | Candidate stock photos and video clips. API search results are cached for 24 hours. |
| Wikimedia Commons | No | Public file search; image and video file candidates plus creator/license metadata. |
| Internet Archive | No | Search historic stills and video records, inspect item metadata, and download a matching file when available. |
| Library of Congress | No | Historic photo collection search and image downloads. |
| NASA Image and Video Library | No | NASA image/video searches and direct media files; most useful for space, Earth science, and aerospace topics. |
| Official company newsroom/media pages | No API key | Best place to find accurate current company/product visuals. OpenAI web research can identify likely official pages; add the chosen file/page to the asset manifest. Automated download from arbitrary company sites is not enabled. |
| YouTube | Yes, `YOUTUBE_API_KEY` for discovery | Finds public videos for topic discovery/reference. StoryPilot does not download YouTube media. |
| YouTube Audio Library | No API key | Import tracks that you download yourself into the local `audio_library_seed` folders. |
| Freesound | Yes, `FREESOUND_API_KEY` | Optional CC0-filtered music/SFX preview search. Preview files are not original-quality downloads. |
| OpenAI image generation | Uses `OPENAI_API_KEY` | Optional illustrative stills and thumbnail art. Generated imagery must not be presented as real archival evidence. |

The extra no-key sources are **collection-specific archives**, not a universal Google Images replacement. For a named modern company, its official press/media page is usually the most accurate source. The program records source URLs, creators, and available license metadata in the asset catalog.

## 1. Install and configure

Use Python 3.10 or later. From the repository folder:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Create the local workspace and save your API keys once:

```bash
python main_pipeline.py setup-apis --base-dir "D:\YouTubeProduction"
```

For macOS/Linux, use a path such as `./production` instead. Key entry is hidden. Values are saved in `secrets.json`, which is excluded from Git but is **not encrypted**. Keep that file private.

| Key | Needed when |
|---|---|
| `OPENAI_API_KEY` | Required for research, script/metadata generation, and optional images. |
| `YOUTUBE_API_KEY` | Needed for trend/topic discovery. Skip it when you pass `--topic`. |
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

That discovery mode needs `YOUTUBE_API_KEY`. YouTube's public counters do not provide competitor retention or watch-time data. The project processes one selected topic per run. It reads `history.json` to avoid repeats, but the current CLI does not automatically write completed topics to that file; the `record_history()` helper must be called by a later publishing/completion step.

## 4. What each stage does and writes

| Stage | What the program does | Expected output |
|---|---|---|
| Workspace/setup | Creates directories and loads keys from environment or `secrets.json`. | Workspace folders; local `secrets.json`; no media yet. |
| Topic discovery (unless `--topic` is supplied) | Searches YouTube for recent videos in the channel niche, ranks public results, asks OpenAI for five distinct ideas, and filters titles/topics against history. | `discovery/<channel>.json` and `discovery/ideas.json`. |
| Research | Uses OpenAI web search to assemble factual claims, source URLs, support notes, confidence labels, and unresolved questions. | Job folder `research.json`. Each factual claim should link to a source. |
| Independent fact check | A second OpenAI web-search pass checks each claim against cited and corroborating sources. Only verified or explicitly qualified claims with source URLs are passed to script writing; if none pass, script generation stops. | `research.json` includes `verification_summary`, checked sources, and `verified_claims`. |
| News headline screenshots (only when requested by the creative brief) | Searches approved major news publishers, captures only the visible article headline, and highlights matching topic terms in yellow. If no scene requests one, it performs no additional news article search. | `assets/screenshots/<job-name>/*.png` and `article_screenshots.json`. |
| Script | Writes long-form narration chunks with claim IDs, tone/energy and visual directions, plus a complete short-form script. | `script.json`. The word-count estimate is only a duration proxy; measure actual narration time before editing. |
| Narration preparation | Synthesizes each chunk with Azure SSML and delivery cues, captures word boundaries, and creates timed English captions. | `narration.wav`, `narration_manifest.json`, `word_timestamps.json`, `captions.srt`, and `timed_edit_job.json`. |
| Niche creative direction | Loads the matching entry from `niche_prompts.json` for source priorities, story framing, retention beats, and narration delivery. | Five starter profiles: cybersecurity, finance, business, dark history, and future technology. |
| Asset planning | Converts each scene's visual, music, and SFX needs into provider search requests. Video requests carry the five-second maximum. | `asset_requests.json`, including query, scene, purpose, and candidate-source list. |
| Asset discovery (`--discover-assets`) | Searches no-key Commons, Internet Archive, Library of Congress, and NASA collections. It adds source candidates and links to the manifest. | `asset_requests.json` updated with `candidates`. |
| Asset acquisition (`--acquire-assets`) | Reuses matches from the catalog first, then tries Pixabay (if configured) and the public archive adapters. It can generate a clearly labeled illustrative still when no photo matches. It indexes local Audio Library seeds and can try Freesound previews. | `selected_assets.json`; reusable files in shared `assets/` folders; SQLite catalog and JSON sidecars. Unavailable assets are listed under `unresolved`. |
| Metadata/thumbnails | Writes title hypotheses, description, tags, and two thumbnail briefs. With `--make-thumbnails`, asks OpenAI for two thumbnail images. | `metadata_ab_test.json` plus `thumbnail_1.png` and `thumbnail_2.png` when generation succeeds. |
| QA helper | `qa_package()` can check package files, claim IDs/URLs, script-length estimate, rights metadata, and optionally supplied final files, but the current CLI does not call it as an automatic gate. | No automatic QA report is produced by the current command. Final video QA is not implemented. |
| Pre-edit asset check and edit handoff | Verifies referenced files, retries missing manifest items through configured sources, and refuses handoff if required assets or captions remain missing. | `edit_readiness.json`; on success, FCPXML, timeline JSON, and edit notes. This is still an editor handoff, not an MP4. |
| Final render and upload | Not implemented in this repository version. | Render the timeline in a compatible editor, verify duration/audio/captions/aspect ratio, then upload privately yourself. |

Each job is written to `jobs/<channel-topic>/`. Typical files are `research.json`, `script.json`, `asset_requests.json`, `selected_assets.json` (when acquisition is run), `metadata_ab_test.json`, optional thumbnail PNGs, and `job.json`.

`niche_prompts.json` is the project's editable creative-direction file. Add a profile whose key matches the channel's `niche`; it can define `research_prompt`, `system_prompt`, `retention_guidance`, `narration_direction`, `voice_name`, and `default_search_fallback`. The script prompts for a truthful opening, new evidence or stakes through the story, meaningful re-hooks, and a resolved ending. Each narration chunk has tone, emotion, pace, intensity, and pause guidance. `azure_tts.py` turns those cues into speech and outputs word timings and `captions.srt`. The default is `en-AU-Isla:MAI-Voice-2.1`; pass `--voice en-AU-WilliamNeural` to audition the standard male voice, which currently has no listed emotion styles. A channel-level `voice_name` controls the voice written into the script; `--voice` overrides it at synthesis time.

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

The edit stage expects a JSON job that includes timed narration segments, paths to approved local assets, and caption text/timing. See `example_job.json` for the shape. Then run:

```bash
python main_pipeline.py edit path/to/timed_edit_job.json --out "D:\YouTubeProduction\jobs\example\edit_handoff"
```

The handoff includes a varied shot plan using each segment's tone and energy, subtitle timing, motion/layout notes, and FCPXML. The pre-edit report confirms that requested source files exist. It does not mean every visual treatment is rendered: the FCPXML does not yet burn in the SRT, composite 2.5D layers, animate chart/map graphics, apply all planned transitions, mix selected music/SFX tracks, or export MP4. Those are remaining final-render features.

### Narration with Azure Speech

Run pre-production with asset acquisition, then synthesize the script and create a timed edit job:

```bash
python main_pipeline.py preproduction --channels channels.json --base-dir "D:\\YouTubeProduction" --channel @yourhandle --topic "Your researched topic" --acquire-assets
python azure_tts.py "D:\\YouTubeProduction\\jobs\\job-name\\script.json" --out-dir "D:\\YouTubeProduction\\jobs\\job-name" --base-dir "D:\\YouTubeProduction" --assets-manifest "D:\\YouTubeProduction\\jobs\\job-name\\selected_assets.json"
python main_pipeline.py edit "D:\\YouTubeProduction\\jobs\\job-name\\timed_edit_job.json" --out "D:\\YouTubeProduction\\jobs\\job-name\\edit_handoff"
```

Audition a short sample before generating a full episode, and listen to the WAV and inspect the SRT before edit. SSML styles are model-specific: the Australian MAI voice supports expressive styles such as `fearful`, `hopeful`, `softvoice`, and `whispering`, while the standard Australian William voice has no listed emotion styles. A voice model cannot guarantee natural performance or retention; the script, punctuation, delivery tags, and audio mix all affect the result.

## 7. A/B testing and publishing

`metadata_ab_test.json` contains packaging alternatives; it does not start an experiment. YouTube's native thumbnail/title experiment must be started and reviewed through YouTube Studio unless a supported integration is added later. Sequentially changing a title or thumbnail is not a controlled concurrent A/B test. This version does not publish videos or switch them from private to public.

## Tests

From the repository folder:

```bash
python -m unittest discover -s tests -v
```
