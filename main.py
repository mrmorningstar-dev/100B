#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║             AUTO VIDEO EDITOR — Channel Branding Tool               ║
║                                                                      ║
║  Modular, automated video editor powered by Pillow & FFmpeg:        ║
║    - Reads settings from config.yaml                                 ║
║    - Auto-detects portrait vs landscape orientations                 ║
║    - Renders circular avatar, channel name, verified badge & caption ║
║    - NVIDIA NVENC GPU hardware acceleration for 25x+ encoding speed  ║
╚══════════════════════════════════════════════════════════════════════╝

Usage:
    python main.py video.mp4 --caption "Your caption here 😂"
    python main.py "C:\\path\\video.mp4" -c "Caption"
    python main.py video.mp4 (auto-reads from video.txt if present)
"""

import sys
import os
import shutil
import argparse
from pathlib import Path

# Ensure UTF-8 output when piped or redirected on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add project root to path for core imports
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from core import (
    get_video_metadata,
    generate_template,
    compose_video,
    load_config,
    check_dependencies,
    normalize_path,
    format_size,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Auto Video Editor — Add channel branding to videos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "input", nargs="*",
        help="Path to the input video file (unquoted paths with spaces supported)",
    )
    parser.add_argument(
        "--caption", "-c",
        help="Caption text (or reads automatically from <video>.txt)",
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path (default: output/<name>_edited.mp4)",
    )
    parser.add_argument(
        "--name", "-n",
        help="Channel name (overrides config.yaml)",
    )
    parser.add_argument(
        "--handle",
        help="Channel handle without @ (overrides config.yaml)",
    )
    parser.add_argument(
        "--logo", "-l",
        help="Path to channel logo image (overrides config.yaml)",
    )
    parser.add_argument(
        "--gpu", choices=["auto", "nvenc", "cpu"], default=None,
        help="Hardware acceleration mode (default: from config.yaml)",
    )
    parser.add_argument(
        "--preset",
        help="Encoding preset (CPU: ultrafast/fast/medium, NVENC: p1-p7)",
    )
    parser.add_argument(
        "--crf", type=int,
        help="Quality CRF/CQ (default: 18-20)",
    )
    parser.add_argument(
        "--no-verified", action="store_true",
        help="Hide the verified badge",
    )

    return parser.parse_args()


def resolve_caption(input_path, cli_caption):
    """Determine caption from CLI argument, companion .txt file, or interactive prompt."""
    if cli_caption is not None:
        return cli_caption

    # Look for companion .txt file (e.g. video.txt)
    txt_candidate = input_path.with_suffix(".txt")
    if txt_candidate.is_file():
        try:
            caption = txt_candidate.read_text(encoding="utf-8").strip()
            print(f"📄 Auto-loaded caption from companion file: {txt_candidate.name}")
            return caption
        except Exception:
            pass

    # Interactive prompt fallback
    try:
        user_cap = input(f"\n💬 Enter caption for '{input_path.name}': ").strip()
        return user_cap
    except (EOFError, KeyboardInterrupt):
        return ""


def main():
    check_dependencies()
    args = parse_args()

    # 1. Resolve input video path
    if not args.input:
        print("=" * 60)
        print("  AUTO VIDEO EDITOR — Interactive Mode")
        print("=" * 60)
        try:
            user_in = input("\n📁 Drag & drop or paste video path: ").strip().strip('\'"')
        except (EOFError, KeyboardInterrupt):
            user_in = ""
        if not user_in:
            print("❌ No input specified.")
            sys.exit(1)
        raw_input = user_in
    else:
        raw_input = normalize_path(args.input)

    input_path = Path(raw_input).resolve()
    if not input_path.is_file():
        print(f"\n❌ Input file not found: {input_path}")
        sys.exit(1)

    # 2. Load configuration from YAML
    config = load_config(args.config)

    # Apply CLI overrides
    project_root = Path(__file__).parent.resolve()
    channel_cfg = config.setdefault("channel", {})
    if args.name:
        channel_cfg["name"] = args.name
    if args.handle:
        channel_cfg["handle"] = args.handle
    if args.logo:
        channel_cfg["logo_path"] = args.logo
    for p_key in ["logo_path", "badge_path"]:
        p_val = channel_cfg.get(p_key)
        if p_val and not os.path.isabs(p_val):
            resolved = project_root / p_val
            if resolved.is_file():
                channel_cfg[p_key] = str(resolved)
    if args.no_verified:
        channel_cfg["verified"] = False
    if args.gpu:
        config.setdefault("encoding", {})["gpu"] = args.gpu
    if args.preset:
        if config.get("encoding", {}).get("gpu") == "nvenc":
            config["encoding"]["nvenc_preset"] = args.preset
        else:
            config["encoding"]["cpu_preset"] = args.preset
    if args.crf is not None:
        config["encoding"]["cpu_crf"] = args.crf
        config["encoding"]["nvenc_cq"] = args.crf

    # 3. Resolve caption
    caption = resolve_caption(input_path, args.caption)

    # 4. Setup output path
    script_dir = Path(__file__).parent.resolve()
    output_dir = script_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = output_dir / f"{input_path.stem}_edited.mp4"

    # 5. Inspect video streams
    print("=" * 60)
    print("  AUTO VIDEO EDITOR")
    print("=" * 60)
    print(f"\n📹 Input: {input_path.name}")

    video_info = get_video_metadata(input_path)
    orientation = "Portrait (9:16)" if video_info["is_portrait"] else "Landscape (16:9)"

    print(f"   Resolution : {video_info['width']}×{video_info['height']} ({orientation})")
    print(f"   Duration   : {video_info['duration']:.1f}s")
    print(f"   FPS        : {video_info['fps']:.1f}")
    print(f"   Audio      : {'✅ Yes' if video_info['has_audio'] else '❌ No'}")

    channel_name = config.get("channel", {}).get("name", "LaughVault")
    handle = config.get("channel", {}).get("handle", "laughvault").lstrip("@")
    logo = config.get("channel", {}).get("logo_path", "assets/a-logo.png")

    print(f"\n🏷 Channel    : {channel_name} (@{handle})")
    print(f"   Caption    : {caption}")
    print(f"   Logo       : {logo}")

    layout_label = (
        "Portrait (Header at top)" if video_info["is_portrait"]
        else "Landscape (Centered block)"
    )
    print(f"\n🎨 Layout: {layout_label}")

    # 6. Generate template background
    temp_dir = str(output_dir / ".temp")
    os.makedirs(temp_dir, exist_ok=True)

    bg_path, placement = generate_template(config, video_info, caption, temp_dir)

    # 7. Compose video via FFmpeg
    compose_video(
        input_path=str(input_path),
        bg_path=bg_path,
        placement=placement,
        video_info=video_info,
        output_path=str(output_path),
        config=config,
    )

    # 8. Clean up temp files
    shutil.rmtree(temp_dir, ignore_errors=True)

    # 9. Summary
    out_size = output_path.stat().st_size
    print(f"\n{'=' * 60}")
    print(f"  ✅ DONE!")
    print(f"  📁 Output: {output_path}")
    print(f"  📊 Size  : {format_size(out_size)}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()

