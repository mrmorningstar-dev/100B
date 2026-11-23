import os
import subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from loguru import logger


def get_rights_alerts_log_path() -> Path:
    env_path = os.getenv("RIGHTS_ALERTS_LOG_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parent.parent.parent / "logs" / "rights_alerts.log"


LOG_FILE = get_rights_alerts_log_path()


def log_alert(message: str) -> None:
    """Append rights alert message to agent/logs/rights_alerts.log."""
    log_file = get_rights_alerts_log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    ist_now = datetime.now(ZoneInfo("Asia/Kolkata"))
    timestamp = ist_now.strftime("%Y-%m-%dT%H:%M:%S IST")
    log_entry = f"[{timestamp}] {message}\n"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_entry)
    logger.info(f"Rights Alert: [{timestamp}] {message}")


def send_windows_notification(title: str, message: str) -> bool:
    """
    Trigger Windows desktop toast notification via PowerShell WinRT API.
    Fails gracefully if running headless or unsupported.
    """
    if os.getenv("PYTEST_RUNNING") == "1":
        return True
    safe_title = title.replace('"', '\\"').replace("'", "’")
    safe_msg = message.replace('"', '\\"').replace("'", "’")

    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $template = @"
    <toast>
        <visual>
            <binding template="ToastGeneric">
                <text>{safe_title}</text>
                <text>{safe_msg}</text>
            </binding>
        </visual>
    </toast>
"@
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($template)
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("100B Video Bot")
    $notifier.Show($toast)
    """

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        return True
    except Exception as e:
        logger.debug(f"Failed to send Windows toast notification: {e}")
        return False


def notify_rights_alert(title: str, message: str) -> None:
    """Log alert and trigger Windows desktop notification."""
    log_alert(f"{title}: {message}")
    send_windows_notification(title, message)
