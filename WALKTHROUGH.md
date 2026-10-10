# StoryPilot production walkthrough

This walkthrough describes what the current repository actually does. It prepares research, narration, media, metadata, and a story-led edit handoff. It does **not** render a finished MP4 or upload it yet.

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

Synthesize narration using Azure Speech or your configured local Fish Speech endpoint:

```powershell
python azure_tts.py `
  "D:\YouTubeProduction\jobs\job-name\script.json" `
  --out-dir "D:\YouTubeProduction\jobs\job-name" `
  --base-dir "D:\YouTubeProduction" `
  --assets-manifest "D:\YouTubeProduction\jobs\job-name\selected_assets.json"
```

The step uses chunk emotion and delivery cues to guide TTS. It writes narration audio, a manifest, word timings, captions, and `timed_edit_job.json`. Check the narration duration and caption alignment before editing. Fish Speech word timings are approximate in the current integration.

The long-form script targets at least 20 minutes. The preproduction stage also writes a standalone 30-second Shorts script. The current TTS/edit handoff path is set up around the long-form narration; a separate complete 9:16 Shorts render is not implemented yet.

## 5. Build the edit handoff and compare recent styles

Run the edit stage after the narration and required assets exist:

```powershell
python main_pipeline.py edit `
  "D:\YouTubeProduction\jobs\job-name\timed_edit_job.json" `
  --out "D:\YouTubeProduction\jobs\job-name\edit_handoff"
```

Before creating a handoff, StoryPilot checks referenced local assets and captions. If required items are missing, it writes `edit_readiness.json` and stops. When ready, it creates:

- `edit_readiness.json` — required files and ready/blocked status.
- `timeline.json` — per-shot timing, mode, story evidence, visual rationale, palette, caption placement, typography treatment, motion, transition, and audio cues.
- `assistant_edit.fcpxml` — editable rough-cut timeline with narration and available visuals.
- `EDIT_NOTES.md` — human-readable shot list and optional focus points.
- `job_manifest.json` — input job snapshot used to make this handoff.
- `D:\YouTubeProduction\style_history.json` — recent per-channel style signatures for future plans.

Style history nudges automatic choices away from recently common scene modes while the story director's explicit mode takes priority. It is a diversity aid, not a monetization guarantee. Re-running the same job replaces its history record instead of adding a duplicate. Use `--style-history "D:\somewhere\style_history.json"` to choose another history file.

## 6. What QA currently covers

The edit preflight checks that required narration, caption, and referenced media files exist and that referenced video assets have duration metadata. Research QA helpers check claim IDs, sources, estimated script length, media rights notes, and optional final-file size. They do not currently inspect a rendered video's actual duration, pacing, audio mix, captions on screen, or visual repetition because no final renderer is included.

## 7. Current expected outputs and missing stage

| Requested result | Current status |
|---|---|
| Verified research and story-specific narration | Prepared in the job folder |
| Expressive English narration and timed captions | Prepared through Azure or Fish Speech |
| Downloaded/reused visual and audio assets | Attempted from configured sources; unresolved items are reported |
| Channel-branded, story-led shot plan | Written to timeline JSON, notes, and FCPXML handoff |
| 20-minute final MP4 | Not rendered by this repository yet |
| 30-second 9:16 Shorts MP4 | Script exists; separate vertical TTS/render pipeline is not implemented |
| Burned-in subtitles, animated charts/maps, 2.5D scenes, transitions and final audio mix | Planned in the edit handoff; final rendering not implemented |
| Automatic final-video QA and upload | Not implemented |

The current output is therefore a complete production package up to the editable timeline handoff, not an upload-ready video. The next engineering stage is a renderer that applies the timed plan, caption styling, animations, transitions, music/SFX, and format-specific composition, followed by measured video/audio QA.
