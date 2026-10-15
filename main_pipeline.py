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
    retry = sub.add_parser("retry-assets", help="Retry unresolved assets in an existing job without rerunning research or script generation")
    retry.add_argument("--job-dir", type=Path, required=True, help="Existing folder under the workspace jobs directory")
    retry.add_argument("--base-dir", type=Path, required=True)
    retry.add_argument("--youtube-audio-library-dir", type=Path, help="Local folder with music/ or background_music/ and sfx/ subfolders")
    edit = sub.add_parser("edit", help="Build the edit plan and render a QA-checked MP4 locally")
    edit.add_argument("job_json", type=Path, help="Timed narration segments and approved local asset manifest")
    edit.add_argument("--out", type=Path, required=True, help="Directory for the timeline handoff")
    edit.add_argument("--render-dir", type=Path, help="Directory for the final MP4 and QA report (defaults to a rendered_outputs folder beside the handoff)")
    edit.add_argument("--style-history", type=Path, help="Cross-video style history JSON (defaults to workspace/style_history.json)")
    edit.add_argument("--encoder", choices=("auto", "cpu", "nvenc"), default="auto", help="FFmpeg video encoder (default: CPU; choose nvenc when supported)")
    setup = sub.add_parser("setup-apis", help="Create the workspace and configure API keys")
    setup.add_argument("--base-dir", type=Path, required=True, help="Production workspace and local secrets-file directory")
    check = sub.add_parser("check-deps", help="Set up API keys, then check or install dependencies for selected stages")
    check.add_argument("--base-dir", type=Path, required=True, help="Production workspace containing secrets.json or .env")
    check.add_argument("--stage", dest="check_stage", choices=("all", "preproduction", "narration", "edit"), default="all")
    check.add_argument("--topic-provided", action="store_true", help="Skip the YouTube discovery key check")
    check.add_argument("--install", action="store_true", help="Install core Python packages in the active environment")
    check.add_argument("--features", default="", help="Optional packages: maps,ocr,portraits,news (or all)")
    args = parser.parse_args()
    if args.stage == "setup-apis":
        from workspace_setup import configure_api_keys
        status = configure_api_keys(args.base_dir, required_keys=("OPENAI_API_KEY",), force_optional=True)
        print(f"Workspace ready: {args.base_dir.expanduser().resolve()}")
        print(f"Secrets file: {status['secrets_file']}")
        print("Configured API keys: " + (", ".join(status["configured"]) or "none"))
        print("Key values are never displayed.")
        return
    if args.stage == "check-deps":
        from dependency_check import main as dependency_main
        sys.argv = [sys.argv[0], "--base-dir", str(args.base_dir)]
        sys.argv += ["--stage", args.check_stage]
        if args.topic_provided:
            sys.argv.append("--topic-provided")
        if args.install:
            sys.argv.append("--install")
        if args.features:
            sys.argv += ["--features", args.features]
        raise SystemExit(dependency_main())
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
    if args.stage == "retry-assets":
        from prepost_pipeline import retry_job_assets
        from workspace_setup import configure_api_keys
        configure_api_keys(args.base_dir, required_keys=("OPENAI_API_KEY",))
        job_dir = args.job_dir.expanduser().resolve()
        jobs_root = (args.base_dir.expanduser().resolve() / "jobs").resolve()
        if jobs_root not in job_dir.parents:
            raise SystemExit(f"Job folder must be inside {jobs_root}")
        result = retry_job_assets(job_dir, args.base_dir, args.youtube_audio_library_dir)
        print(f"Asset retry complete: {result.get('retry_summary', {}).get('resolved_this_retry', 0)} newly resolved; "
              f"{len(result.get('unresolved', []))} still unresolved.")
        print(f"Updated manifest: {job_dir / 'selected_assets.json'}")
        return
    plan = build_package(args.job_json, args.out, args.style_history)
    print(f"Edit package ready: {args.out.resolve()}")
    print(f"Planned {len(plan['shots'])} shots for {plan['duration']:.1f}s")
    print(f"Channel style identity: {plan['visual_brand'].get('primary_font')} / {plan['visual_brand'].get('brand_accent')}")
    from renderer import render_final_video
    render_dir = args.render_dir or args.out.parent / "rendered_outputs"
    result = render_final_video(args.job_json, render_dir, plan, encoder=args.encoder)
    import json
    edit_job = json.loads(args.job_json.read_text(encoding="utf-8"))
    from prepost_pipeline import mark_job_render_status
    mark_job_render_status(args.job_json, result, edit_job)
    print(f"Final MP4: {result['video_path']}")
    print(f"QA status: {result['status']} ({result['duration_seconds']:.1f}s, {result['canvas']['width']}x{result['canvas']['height']})")
    print(f"Motion graphics rendered: {len(result.get('motion_graphics', []))}")
    print(f"QA report: {(render_dir / (Path(result['video_path']).stem + '_qa.json')).resolve()}")
    print("Upload: not performed")


if __name__ == "__main__":
    main()
