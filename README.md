# StoryPilot

**An AI-assisted production pipeline for YouTube documentaries and Shorts.**

StoryPilot turns a topic into source-linked research, a retention-aware script, a reusable asset plan, metadata and thumbnail concepts, and a dynamic editing handoff.

> **Project status:** This repository builds pre-production packages and editable timeline handoffs. It does not yet render the final video, upload to YouTube, or run a YouTube thumbnail experiment automatically. See the pipeline stages below for details.

The project contains a **pre-production package builder** and an **assistant-editor handoff**. It creates production documents and an editable timeline plan; it does not yet make a finished MP4 end to end or automatically run a YouTube Studio thumbnail experiment.

## Pipeline stages

1. **Discover:** YouTube Data API searches niche videos published in the last 90 days. It ranks by views/day and public likes/comments per view. These public counters do not expose competitor impressions, retention, or watch time. The model proposes five original ideas; a local history file filters near duplicate titles/topics.
2. **Research:** OpenAI Responses API with web search produces a source ledger and confidence labels. The script references those claim IDs. This improves traceability, but automated source-backed research is not a guarantee that every claim is correct; final QA should verify citations and disputed claims.
3. **Write:** OpenAI writes a long documentary, segment-level tone/energy/visual directions, and a separate complete ~30-second Short. The long script is estimated at 2,500+ words as an initial 20-minute proxy; measure the actual narration audio duration before edit QA.
4. **Assets:** The AI writes one `asset_requests.json` handoff with one row per visual, music cue, and SFX cue, including the scene, purpose, search terms, preferred source, and five-second maximum for video. `--acquire-assets` passes that manifest to local Python. `media_library.py` creates a reusable catalog with `assets/pictures/`, `assets/video_clips/`, `assets/generated_media/`, `assets/background_music/`, and `assets/sfx/`. It checks the catalog first, downloads Pixabay picture/video candidates when needed, filters Freesound audio searches to CC0 and caches MP3 previews when `FREESOUND_API_KEY` is set, and generates an illustrative still if a picture search has no match and `OPENAI_API_KEY` is set. A SQLite catalog plus sidecar JSON records hashes, tags, source, creator, terms, and metadata so duplicate files are reused across future videos. Google Images and YouTube remain discovery/reference sources; the current code does not download their media.
5. **Packaging:** Creates three title hypotheses, description/tags, two thumbnail briefs, plus optional generated thumbnail images. Generated art needs a separate text/layout check and 1280x720 crop/export check.
6. **Edit handoff:** Convert timed narration, selected assets, and burned subtitle cues into `example_job.json` format, then use `main_pipeline.py edit ...`. It exports FCPXML, timeline JSON, and edit notes with topic-led palette, layouts, motion, pacing, and SFX cues.
7. **Polish, QA, publish:** Complete final rendering in a compatible editor, run local QA (duration, audio, aspect ratio, burned-in captions, files, and rights manifest), then upload. This package does **not** currently implement Google OAuth upload, private-to-public scheduling, YouTube Analytics polling, or automated thumbnail/title replacement.

## Setup

Python 3.10+; install `requests` and `python-dotenv` (and dependencies needed by the existing FCPXML module if applicable). Set credentials in the environment; never commit secrets:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5
OPENAI_IMAGE_MODEL=gpt-image-1
YOUTUBE_API_KEY=...
PIXABAY_API_KEY=...
FREESOUND_API_KEY=...
PIXABAY_MAX_SEARCHES_PER_RUN=10
GOOGLE_CSE_API_KEY=...
GOOGLE_CSE_CX=...
```

### API keys and first run

Run the setup command once to create the workspace folders and enter API keys:

```bash
python main_pipeline.py setup-apis --base-dir ./production
```

API keys are entered with hidden typing and saved to `./production/secrets.json`. The file is excluded from Git. Keep it private and do not share it; it is local JSON, not encrypted. You can also provide keys as environment variables, which take precedence over saved values. The program does not print key values.

Required for pre-production:

- `OPENAI_API_KEY` — research, scriptwriting, metadata, and optional OpenAI image generation.
- `YOUTUBE_API_KEY` — required only when the program must discover trending videos; skip discovery by passing `--topic`.

Optional providers:

- `PIXABAY_API_KEY` — search/download candidate pictures and video clips.
- `FREESOUND_API_KEY` — search CC0 audio previews for music and sound effects.
- `GOOGLE_CSE_API_KEY` and `GOOGLE_CSE_CX` — find image references. Both are needed together.

The first pre-production run asks for missing required keys and offers one prompt round for optional providers. Leave an optional entry blank to skip it. To add skipped providers later, rerun `setup-apis`; it asks for missing optional keys again. The video edit handoff stage does not need API keys.

The workspace setup creates `discovery/`, `jobs/`, `assets/` and its media folders, `rendered_outputs/`, `temp_assets/`, `logs/`, and YouTube Audio Library seed folders automatically.

For the user's preferred YouTube Audio Library, download tracks into a seed folder once, organized as `background_music/` and `sfx/`. The pipeline imports and indexes them, then selects and copies matching tracks into each job's asset plan. It does not automate downloads from YouTube Audio Library because this implementation has no supported bulk-download API adapter for it. Freesound is the automatic audio fallback when no local track matches and `FREESOUND_API_KEY` is set; it requests CC0-only results and downloads their MP3 previews. Freesound's original-quality download endpoint requires OAuth, while previews do not. CC0 lowers attribution/licensing obligations but does not promise that a platform's automated fingerprinting will never issue a false or third-party claim.

Pixabay's documented API searches images and videos; its website offers music and SFX but there is no documented public audio-search API in the API reference. The current pipeline does not scrape Pixabay's audio pages. Its image/video search responses are cached locally for 24 hours, with a default cap of 10 uncached searches per run (`PIXABAY_MAX_SEARCHES_PER_RUN`). Pixabay asks developers to avoid lots of automated queries and systematic bulk downloads; this limit is a conservative guard, not provider approval for any particular use. For Pixabay music downloaded through its website, save the track page and any Content ID certificate alongside the audio. Pixabay says some free tracks are Content ID registered; a certificate can support a dispute, but it does not prevent a claim or guarantee the dispute outcome.

`YOUTUBE_API_KEY` is for public research only. Upload requires separate OAuth credentials and a channel owner grant. YouTube Audio Library assets should be downloaded/curated by the operator into a local licensed music/SFX catalog; the Audio Library does not provide the general public asset API this code would need for automatic bulk retrieval.

## Run

Prepare one topic selected from discovery (or pass a topic directly):

```bash
python main_pipeline.py preproduction --channels channels.example.json --base-dir ./production --channel @example --discover-assets --make-thumbnails --acquire-assets --youtube-audio-library-dir ./audio_library_seed
```

Or let it discover and select the first non-duplicate idea:

```bash
python main_pipeline.py preproduction --channels channels.example.json --base-dir ./production --channel @example
```

The first run creates `production/history.json` (empty until you record completed/uploaded topics), `production/discovery/`, and `production/jobs/<channel-topic>/` containing:

- `research.json` — source ledger, confidence flags, open questions
- `script.json` — long script chunks, claims, visual/tone directions, Short script
- `asset_requests.json` — search queries/candidates, rights fields, max clip length
- `selected_assets.json` — selected/reused/downloaded assets and music/SFX assignments
- `assets/` — shared reusable catalog, sorted by media type, with `catalog.sqlite3` and per-file metadata sidecars
- `metadata_ab_test.json` — title and thumbnail hypotheses
- `thumbnail_1.png`, `thumbnail_2.png` — only when image generation is enabled
- `job.json` — stage state and paths

After producing narrator audio, getting word timestamps, and selecting assets with rights metadata, build a timeline:

```bash
python main_pipeline.py edit path/to/timed_edit_job.json --out path/to/edit_handoff
```

## Automatic A/B testing limits

`metadata_ab_test.json` is the experiment plan and packaging variants. YouTube's native concurrent thumbnail/title experiment is a Studio feature, and there is no supported Data API endpoint in this implementation to start/read that experiment. Do not sequentially replace the title/thumbnail and describe it as a controlled test. Analytics can support a later decision, but observed CTR/retention differences can be confounded by traffic and time.

## QA gate

The pre-production QA helper checks required files, sourced claims, script length estimate, unsupported claim IDs, asset rights fields, and basic final file size. A production-grade final QA gate must additionally inspect actual video duration and 16:9/9:16 geometry with `ffprobe`, audio peaks/loudness, caption presence/synchronization, black frames, media rights, thumbnail dimensions, and YouTube upload status. Upload should be blocked whenever those checks fail.

## Tests

```bash
python -m unittest discover -s tests -v
```
