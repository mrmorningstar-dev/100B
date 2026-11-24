import os
import sys
import shutil
import yaml
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def load_config(config_path="config.yaml"):
    """Load configuration from a YAML file with robust fallback defaults."""
    project_root = Path(__file__).parent.parent.resolve()
    path = Path(config_path)
    if not path.is_absolute():
        if (project_root / config_path).is_file():
            path = project_root / config_path

    if path.is_file():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                # Ensure channel section exists
                if "channel" not in data:
                    data["channel"] = {
                        "name": "LaughVault",
                        "handle": "laughvault",
                        "logo_path": str(project_root / "assets" / "a-logo.png"),
                        "badge_path": str(project_root / "assets" / "badge.png"),
                        "verified": False,
                    }
                # Resolve relative asset paths against project root
                channel = data.get("channel", {})
                for key in ["logo_path", "badge_path"]:
                    val = channel.get(key)
                    if val and not os.path.isabs(val):
                        resolved = project_root / val
                        if resolved.is_file():
                            channel[key] = str(resolved)
                return data

    # Default fallback config if file not found
    return {
        "channel": {
            "name": "LaughVault",
            "handle": "laughvault",
            "logo_path": str(project_root / "assets" / "a-logo.png"),
            "badge_path": str(project_root / "assets" / "badge.png"),
            "verified": False,
        },
        "canvas": {
            "width": 1080,
            "height": 1920,
            "bg_color": [255, 255, 255],
        },
        "layout": {
            "padding_left": 72,
            "padding_top_portrait": 160,
            "avatar_size": 124,
            "logo_name_gap": 26,
            "header_caption_gap": 58,
            "caption_video_gap": 46,
            "video_side_margin": 28,
            "bottom_buffer_portrait": 60,
        },
        "colors": {
            "text": [15, 20, 25],
            "handle": [83, 100, 113],
            "verified": [29, 155, 240],
        },
        "typography": {
            "font_size_name": 44,
            "font_size_handle": 35,
            "font_size_caption": 44,
            "caption_line_spacing": 16,
            "font_bold": "arialbd.ttf",
            "font_regular": "arial.ttf",
            "font_emoji": "seguiemj.ttf",
        },
        "encoding": {
            "gpu": "auto",
            "cpu_preset": "fast",
            "cpu_crf": 18,
            "nvenc_preset": "p4",
            "nvenc_cq": 19,
            "audio_codec": "aac",
            "audio_bitrate": "192k",
        },
    }


def check_dependencies():
    """Verify that FFmpeg and FFprobe binaries are present in PATH."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        print(f"❌ Missing required dependencies: {', '.join(missing)}")
        print("   Please ensure FFmpeg is installed and added to your system PATH.")
        sys.exit(1)


def load_font(font_name, size):
    """Load TrueType font with cross-platform search paths."""
    candidates = [
        f"C:/Windows/Fonts/{font_name}",
        f"/usr/share/fonts/truetype/dejavu/{font_name}",
        f"/usr/share/fonts/truetype/liberation/{font_name}",
        f"/System/Library/Fonts/{font_name}",
        f"/Library/Fonts/{font_name}",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue

    try:
        return ImageFont.truetype(font_name, size)
    except Exception:
        return ImageFont.load_default(size)


def create_circular_image(image_path, size):
    """Load an image, center-crop to a square, and apply an antialiased circular mask."""
    img = Image.open(image_path).convert("RGBA")

    # Center-crop square
    w, h = img.size
    min_dim = min(w, h)
    left = (w - min_dim) // 2
    top = (h - min_dim) // 2
    img = img.crop((left, top, left + min_dim, top + min_dim))
    img = img.resize((size, size), Image.LANCZOS)

    # Circular mask
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)

    output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    output.paste(img, (0, 0), mask)
    return output


def wrap_text(text, font, max_width, draw):
    """Word-wrap text within a given pixel width."""
    words = text.split()
    if not words:
        return [text]

    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        try:
            bbox = draw.textbbox((0, 0), test, font=font, embedded_color=True)
        except Exception:
            bbox = draw.textbbox((0, 0), test, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if lines else [text]


def normalize_path(path_input):
    """Join unquoted space-separated arguments and strip enclosing quotes."""
    if isinstance(path_input, (list, tuple)):
        raw = " ".join(path_input)
    else:
        raw = str(path_input)
    return raw.strip('\'"')


def format_size(num_bytes):
    """Format file size in human-readable KB or MB."""
    if num_bytes > 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"

