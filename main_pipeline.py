"""Main CLI for the production pipeline.

`preproduction` builds the research, script, asset, and packaging files. `edit`
builds the editable timeline after narration timings and approved assets exist.
"""
from pathlib import Path
import argparse
import sys

from creative_director import build_package


def main() -> None:
    parser = argparse.ArgumentParser(description="Documentary production pipeline")
    sub = parser.add_subparsers(dest="stage", required=True)
    pre = sub.add_parser("preproduction", help="Discover topics, research, script, asset briefs, metadata")
    pre.add_argument("--channels", type=Path, required=True)
    pre.add_argument("--base-dir", type=Path, required=True)
    pre.add_argument("--channel")
    pre.add_argument("--topic")
    pre.add_argument("--discover-assets", action="store_true")
    pre.add_argument("--make-thumbnails", action="store_true")
    pre.add_argument("--acquire-assets", action="store_true")
    pre.add_argument("--youtube-audio-library-dir", type=Path)
    edit = sub.add_parser("edit", help="Build FCPXML/JSON assistant-editor handoff")
    edit.add_argument("job_json", type=Path, help="Timed narration segments and approved local asset manifest")
    edit.add_argument("--out", type=Path, required=True, help="Directory for the timeline handoff")
    args = parser.parse_args()
    if args.stage == "preproduction":
        from prepost_pipeline import main as preproduction_main
        sys.argv = [sys.argv[0], "--channels", str(args.channels), "--base-dir", str(args.base_dir)]
        if args.channel:
            sys.argv += ["--channel", args.channel]
        if args.topic:
            sys.argv += ["--topic", args.topic]
        if args.discover_assets:
            sys.argv.append("--discover-assets")
        if args.make_thumbnails:
            sys.argv.append("--make-thumbnails")
        if args.acquire_assets:
            sys.argv.append("--acquire-assets")
        if args.youtube_audio_library_dir:
            sys.argv += ["--youtube-audio-library-dir", str(args.youtube_audio_library_dir)]
        preproduction_main()
        return
    plan = build_package(args.job_json, args.out)
    print(f"Handoff ready: {args.out.resolve()}")
    print(f"Planned {len(plan['shots'])} shots for {plan['duration']:.1f}s")
    print("Next: make the creative polish pass, export the master, then run final QA/upload.")


if __name__ == "__main__":
    main()
