import os
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger
import httpx


def get_alerts_log_path() -> Path:
    env_path = os.getenv("ALERTS_LOG_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parent / "logs" / "alerts.log"


ALERTS_LOG_PATH = get_alerts_log_path()


def _log_alert_to_file(alert_type: str, message: str, details: Optional[Dict[str, Any]] = None):
    """Fallback: append alert to alerts.log when Telegram is unavailable or fails."""
    try:
        log_path = get_alerts_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        entry = {
            "timestamp": ts,
            "type": alert_type,
            "message": message,
            "details": details or {},
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info(f"Alert recorded in {log_path}")
    except Exception as e:
        logger.error(f"Failed to write alert to file: {e}")


def _show_windows_notification(title: str, message: str):
    """Show non-blocking native Windows toast or balloon notification."""
    if sys.platform != "win32" or os.getenv("PYTEST_RUNNING") == "1":
        return
        return
    try:
        # PowerShell script to show a Windows tray notification
        clean_title = title.replace('"', '`"')
        clean_msg = message.replace('"', '`"').replace("\n", " ")[:200]
        ps_cmd = (
            f"[void] [System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
            f"$notify = New-Object System.Windows.Forms.NotifyIcon;"
            f"$notify.Icon = [System.Drawing.SystemIcons]::Information;"
            f"$notify.Visible = $true;"
            f"$notify.ShowBalloonTip(5000, '{clean_title}', '{clean_msg}', [System.Windows.Forms.ToolTipIcon]::Info);"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        logger.debug(f"Could not display Windows notification: {e}")


def send_telegram_alert(
    alert_type: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """
    Send an automated alert. If Telegram is configured, posts via Telegram Bot API.
    If Telegram is unconfigured or errors out, logs to uploader/logs/alerts.log
    and triggers a Windows notification.
    """
    token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip("'\" ")
    cid = (chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip("'\" ")

    telegram_sent = False
    if token and cid:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = f"<b>⚠️ [100B Uploader Alert: {alert_type}]</b>\n\n{message}"
        if details:
            text += "\n\n<b>Details:</b>\n"
            for k, v in details.items():
                text += f"• <b>{k}:</b> <code>{v}</code>\n"

        payload = {
            "chat_id": cid,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info(f"Telegram alert '{alert_type}' sent successfully.")
                    telegram_sent = True
                else:
                    logger.warning(f"Telegram returned status {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.warning(f"Telegram alert delivery failed: {e}")

    # Fallback to file and Windows notification if Telegram didn't send
    if not telegram_sent:
        _log_alert_to_file(alert_type, message, details)
        _show_windows_notification(f"100B Bot: {alert_type}", message)

    return telegram_sent


def send_upload_summary(
    target_date: str,
    total_slots: int,
    uploaded_count: int,
    scheduled_count: int,
    failed_count: int,
    mode: str,
    quota_used: int,
    dry_run: bool = False,
    errors: Optional[list] = None,
) -> bool:
    """Send formatted upload execution summary."""
    status_emoji = "✅" if failed_count == 0 else "⚠️"
    mode_str = "DRY-RUN (Simulated)" if dry_run else f"PRODUCTION ({mode.upper()})"
    text = (
        f"<b>{status_emoji} YouTube Upload Completed</b>\n\n"
        f"<b>Target Date:</b> <code>{target_date}</code>\n"
        f"<b>Execution Mode:</b> {mode_str}\n"
        f"<b>Slots:</b> {total_slots} total\n"
        f"<b>Uploaded / Scheduled:</b> {uploaded_count + scheduled_count} / {total_slots}\n"
        f"<b>Failed:</b> {failed_count}\n"
        f"<b>Estimated Quota Used:</b> {quota_used:,} units\n"
    )
    if errors:
        text += f"\n<b>Failed Slots:</b>\n"
        for err in errors[:5]:
            text += f"• <code>{err.get('slot_time', '')}</code>: {err.get('error', '')}\n"

    details = {"Date": target_date, "Mode": mode_str, "Quota": quota_used}
    return send_telegram_alert("UPLOAD_SUMMARY", text, details=None)
