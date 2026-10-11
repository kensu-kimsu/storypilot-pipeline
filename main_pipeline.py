"""Main CLI for the production pipeline.

`preproduction` builds the research, script, asset, and packaging files. `edit`
builds the story-led timeline and renders a QA-checked local MP4 after narration
timings and approved assets exist. It never uploads the video.
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
    pre.add_argument("--niche-prompts", type=Path, default=Path(__file__).with_name("niche_prompts.json"))
    pre.add_argument("--discover-assets", action="store_true")
    pre.add_argument("--make-thumbnails", action="store_true")
    pre.add_argument("--acquire-assets", action="store_true")
    pre.add_argument("--youtube-audio-library-dir", type=Path)
    edit = sub.add_parser("edit", help="Build the edit plan and render a QA-checked MP4 locally")
    edit.add_argument("job_json", type=Path, help="Timed narration segments and approved local asset manifest")
    edit.add_argument("--out", type=Path, required=True, help="Directory for the timeline handoff")
    edit.add_argument("--render-dir", type=Path, help="Directory for the final MP4 and QA report (defaults to a rendered_outputs folder beside the handoff)")
    edit.add_argument("--style-history", type=Path, help="Cross-video style history JSON (defaults to workspace/style_history.json)")
    edit.add_argument("--encoder", choices=("auto", "cpu", "nvenc"), default="auto", help="FFmpeg video encoder (default: CPU; choose nvenc when supported)")
    setup = sub.add_parser("setup-apis", help="Create the workspace and configure API keys")
    setup.add_argument("--base-dir", type=Path, required=True, help="Production workspace and local secrets-file directory")
    args = parser.parse_args()
    if args.stage == "setup-apis":
        from workspace_setup import configure_api_keys
        status = configure_api_keys(args.base_dir, required_keys=("OPENAI_API_KEY",), force_optional=True)
        print(f"Workspace ready: {args.base_dir.expanduser().resolve()}")
        print(f"Secrets file: {status['secrets_file']}")
        print("Configured API keys: " + (", ".join(status["configured"]) or "none"))
        print("Key values are never displayed.")
        return
    if args.stage == "preproduction":
        from prepost_pipeline import main as preproduction_main
        sys.argv = [sys.argv[0], "--channels", str(args.channels), "--base-dir", str(args.base_dir)]
        if args.channel:
            sys.argv += ["--channel", args.channel]
        if args.topic:
            sys.argv += ["--topic", args.topic]
        if args.niche_prompts:
            sys.argv += ["--niche-prompts", str(args.niche_prompts)]
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
    plan = build_package(args.job_json, args.out, args.style_history)
    print(f"Edit package ready: {args.out.resolve()}")
    print(f"Planned {len(plan['shots'])} shots for {plan['duration']:.1f}s")
    print(f"Channel style identity: {plan['visual_brand'].get('primary_font')} / {plan['visual_brand'].get('brand_accent')}")
    from renderer import render_final_video
    render_dir = args.render_dir or args.out.parent / "rendered_outputs"
    result = render_final_video(args.job_json, render_dir, plan, encoder=args.encoder)
    print(f"Final MP4: {result['video_path']}")
    print(f"QA status: {result['status']} ({result['duration_seconds']:.1f}s, {result['canvas']['width']}x{result['canvas']['height']})")
    print(f"Motion graphics rendered: {len(result.get('motion_graphics', []))}")
    print(f"QA report: {(render_dir / (Path(result['video_path']).stem + '_qa.json')).resolve()}")
    print("Upload: not performed")


if __name__ == "__main__":
    main()
