import os
import sys
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Tuple
from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
MAIN_EDITOR_SCRIPT = PROJECT_ROOT / "main.py"


class EditorTool:
    """
    Wraps Phase 1 Video Editor tool.
    Applies channel branding (circular avatar, handle, verified badge, caption)
    using Pillow and FFmpeg with NVENC hardware acceleration.
    """

    def __init__(self, editor_script: Path = MAIN_EDITOR_SCRIPT, max_workers: int = 2):
        self.editor_script = editor_script
        self.max_workers = max_workers

    def edit_single_video(
        self,
        input_video_path: Path,
        caption: str,
        output_video_path: Path,
        dry_run: bool = False,
    ) -> Tuple[bool, str]:
        """
        Render branded video using Phase 1 tool.
        Returns (success: bool, message: str).
        """
        if not input_video_path or not input_video_path.is_file():
            return False, f"Input video not found or invalid: {input_video_path}"

        if dry_run:
            logger.info(f"[DRY-RUN] Would edit {input_video_path.name} -> {output_video_path.name} with caption '{caption}'")
            return True, "Dry-run simulated success"

        output_video_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            str(self.editor_script),
            str(input_video_path),
            "--caption", caption,
            "--output", str(output_video_path),
        ]

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            res = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=300,
            )
            if res.returncode == 0 and output_video_path.is_file():
                file_size = output_video_path.stat().st_size
                if file_size < 50_000:
                    try:
                        output_video_path.unlink()
                    except Exception:
                        pass
                    return False, f"Editor produced truncated video ({file_size} bytes < 50KB)"
                return True, f"Successfully edited {output_video_path.name}"
            else:
                err = res.stderr[-400:] if res.stderr else res.stdout[-400:]
                return False, f"Editor failed (code {res.returncode}): {err}"
        except Exception as e:
            return False, f"Editor exception: {e}"

    def batch_edit(
        self,
        tasks: List[Dict[str, Any]],
        output_dir: Path,
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Run multiple edits in parallel up to max_workers limit.
        Each task dict contains:
          - 'post_id': str
          - 'video_path': str or Path
          - 'caption': str
        Returns list of updated video dictionaries with 'edited_video_path' and 'edit_status'.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        results: List[Dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_map = {}
            for item in tasks:
                in_path = Path(item["video_path"])
                post_id = item["post_id"]
                out_path = output_dir / f"{post_id}_branded.mp4"
                caption = item.get("caption") or item.get("title", "")

                fut = executor.submit(
                    self.edit_single_video,
                    in_path,
                    caption,
                    out_path,
                    dry_run,
                )
                future_map[fut] = (item, out_path)

            for fut in as_completed(future_map):
                item, out_path = future_map[fut]
                success, msg = fut.result()
                item_copy = dict(item)
                if success:
                    item_copy["edited_video_path"] = str(out_path)
                    item_copy["edit_status"] = "success"
                    logger.success(f"Edited: {out_path.name}")
                else:
                    item_copy["edited_video_path"] = None
                    item_copy["edit_status"] = "failed"
                    item_copy["edit_error"] = msg
                    logger.error(f"Edit failed for {item.get('post_id')}: {msg}")
                results.append(item_copy)

        return results

