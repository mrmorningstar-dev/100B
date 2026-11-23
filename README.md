# 🎬 Auto Video Editor — Automated Channel Branding & Framing

A modular, high-performance automated video editing suite built with **Python**, **Pillow**, and **FFmpeg**. Automatically adds social channel branding (circular logo, bold channel name, verified checkmark badge, `@handle`, and multi-line caption with full-color emojis) to portrait and landscape videos with zero quality degradation and hardware-accelerated speeds.

---

## 📁 Project Architecture

```
project/
  main.py            # CLI entry point & interactive workflow
  config.yaml        # Layout, colors, fonts, and channel branding settings
  core/
    __init__.py      # Package exports
    probe.py         # ffprobe wrapper for metadata & rotation analysis
    header.py        # Pillow template layout & caption rendering
    compose.py       # FFmpeg filter-complex builder & encoding runner
    utils.py         # Path helpers, font loader, circular image mask & text wrapper
  assets/            # Logo (a-logo.png), verified badge icon, custom fonts
  templates/         # Reference template screenshots (portrait & landscape)
  output/            # Destination directory for edited videos
  requirements.txt   # Python package dependencies
  README.md          # Full project documentation & guide
```

---

## ⚡ Key Capabilities

- **Auto Orientation Detection**: Automatically inspects video aspect ratio:
  - **Portrait (9:16 / 4:5)**: Header is pinned near the top; video scales and frames cleanly below.
  - **Landscape (16:9)**: Header, caption, and video are grouped together and **vertically centered** on the 9:16 canvas (matching viral meme/repost formats).
- **Pixel-Calibrated Typography**: Matches Twitter / X mobile typography with bold channel name, secondary gray handle, and full-color emoji rendering (`😂`, `🔥`, etc.) via Windows Segoe UI Emoji.
- **Hardware Acceleration**: Automatic NVIDIA NVENC (`h264_nvenc`) GPU detection delivering **25x–27x real-time encoding** (over 1,600 FPS) on NVIDIA RTX/GTX GPUs, with fallback to multi-threaded CPU `libx264`.
- **Fault-Tolerant Path Handling**: CLI automatically stitches unquoted Windows paths containing spaces (e.g. `C:\Drive-D\python tut\100B\video.mp4`).
- **Companion File Auto-Caption**: If `video.txt` exists alongside `video.mp4`, the caption is read automatically without typing command-line flags.

---

## 🚀 Installation & Prerequisites

### 1. Requirements
- **Python**: 3.10+
- **FFmpeg & FFprobe**: Ensure `ffmpeg` and `ffprobe` are installed and added to system `PATH`.
- **GPU (Optional)**: NVIDIA RTX/GTX graphics card for NVENC hardware encoding.

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

---

## 🛠 Usage Guide

### Basic Command
```bash
python main.py video.mp4 --caption "How does your brain come up with this stuff? 😂"
```

### Auto-Detect Caption from `.txt` Companion File
If you have `video.mp4` and `video.txt` in the same directory:
```bash
python main.py video.mp4
```

### Full Path with Spaces (Quotes Optional)
```bash
python main.py "C:\Drive-D\python tut\100B\video-1.mp4" -c "Bro trying not to laugh 😂"
```

### Interactive Mode
Run without arguments to trigger the interactive drag-and-drop prompt:
```bash
python main.py
```

### Overriding Settings via CLI
```bash
python main.py video.mp4 -c "Caption" --name "My Channel" --handle "myhandle" --logo assets/custom_logo.png --gpu nvenc --preset p4
```

---

## ⚙️ Configuration (`config.yaml`)

All visual styling, layout metrics, colors, and encoding presets are centralized in `config.yaml`:

```yaml
channel:
  name: "Cheerful Videos"
  handle: "Cheerfulvids"
  logo_path: "assets/a-logo.png"
  badge_path: "assets/badge.png"
  verified: true

canvas:
  width: 1080
  height: 1920
  bg_color: [255, 255, 255]

layout:
  padding_left: 72               # Left padding for avatar and caption
  padding_top_portrait: 160      # Top padding for portrait template
  avatar_size: 124               # Avatar circle diameter
  logo_name_gap: 26              # Gap between logo and channel name
  header_caption_gap: 58         # Gap between handle and caption text
  caption_video_gap: 46          # Gap between caption and video
  video_side_margin: 28          # Outer side margin framing the video
  bottom_buffer_portrait: 60     # Minimum bottom margin in portrait mode

colors:
  text: [15, 20, 25]             # Dark text
  handle: [83, 100, 113]         # Secondary gray
  verified: [29, 155, 240]       # Twitter/X verification blue

typography:
  font_size_name: 44
  font_size_handle: 35
  font_size_caption: 44
  caption_line_spacing: 16
  font_bold: "arialbd.ttf"
  font_regular: "arial.ttf"
  font_emoji: "seguiemj.ttf"

encoding:
  gpu: "auto"                    # auto, nvenc, or cpu
  cpu_preset: "fast"
  cpu_crf: 18
  nvenc_preset: "p4"
  nvenc_cq: 19
  audio_codec: "aac"
  audio_bitrate: "192k"
```

---

## 🤖 Phase 3.6: Automated Video Curation Agent & Smarter Supply

The repository includes a LangGraph-orchestrated daily curation pipeline with automated discovery, two-step ingestion, Vision LLM evaluation, and rank-based daily scheduling:

```
                  ┌──────────────────────┐
                  │ Loop 1: Discovery    │
                  └──────────┬───────────┘
                             ▼
┌──────────────┐  Loop 2/3   ┌──────────────────────┐
│ Adaptive     │◄────────────┤ Source Registry      │
│ Deeper Pages │             │ (80/20 Bandit)       │
└──────────────┘             └──────────┬───────────┘
                                        ▼
                             ┌──────────────────────┐
                             │ Pre-Download Filter  │ 5s-90s Duration Filter & Remote ffprobe
                             └──────────┬───────────┘
                                        ▼
                             ┌──────────────────────┐
                             │ Title Pre-Screen     │ Zero-Cost Fast LLM
                             └──────────┬───────────┘
                                        ▼
                             ┌──────────────────────┐
                             │ Parallel Downloader  │ Deduplication & Perceptual Hash
                             └──────────┬───────────┘
                                        ▼
                             ┌──────────────────────┐
                             │ Vision LLM Judge     │ Gemma / Gemini Vision
                             └──────────┬───────────┘
                                        ▼
                             ┌──────────────────────┐
                             │ Rank-Based Decide    │ Daily Top-6 (Score >= 6.5)
                             └──────────┬───────────┘
                                        ▼
                             ┌──────────────────────┐
                             │ Borderline Policy    │ approve_top / review / reject
                             └──────────┬───────────┘
                                        ▼
                             ┌──────────────────────┐
                             │ 6 Peak Hourly Slots  │ 12:00, 14:00, 17:00, 19:00, 20:00, 21:00
                             └──────────────────────┘
```

### Key Capabilities in Phase 3.6:
1. **Duration Filtering Prior to Download**:
   - `min_duration_seconds: 5.0` and `max_duration_seconds: 90.0` defined in `scraper/config.yaml` as single source of truth and synced by the agent.
   - Known Reddit video durations from JSON/PRAW metadata are filtered in `--list-only` mode before downloading bytes.
   - Unknown durations (e.g. RSS feeds) are probed via lightweight stream URL `ffprobe` inspection prior to full file download.
   - Hard ceiling: Any clip over 180 seconds is rejected unconditionally (YouTube Shorts limit).
2. **`after` Cursor Pagination**:
   - For `top_week`, `top_month`, `top_year`, `top_all` and search queries, queries fetch `limit=100` and pass `after=<cursor>`.
   - The cursor is automatically stored in the `sources` registry table and resumed across runs.
   - `hot` listings never use a cursor (they start from the top).
   - If a source yields 3 consecutive empty pages, it is marked `exhausted` and automatically reset after 7 days.
3. **Daily Top-6 Selection by Rank**:
   - Daily target defaults to **6 videos** (matching channel daily upload quotas).
   - Evaluated clips are ranked by composite hybrid score (LLM humor + normalized engagement). Rank ties are broken by raw engagement, then recency (`created_utc`).
   - Top 6 meeting `min_publish_score: 6.5` are auto-approved.
   - Clips between 5.0 and the lowest chosen score enter the borderline pool.
   - `--borderline-policy`:
     - `approve_top`: Fills remaining slots from best borderline clips meeting `min_publish_score: 6.5`. (Default for unattended `--no-review` runs).
     - `review`: Sends borderline candidates to interactive review. (Default for interactive runs).
     - `reject`: Drops all borderline candidates.
   - Safety exclusions (watermarks, ads-unsafe, copyrighted broadcasts, non-English) are permanently rejected regardless of score.
   - When fewer than 6 good clips exist, empty slots are logged and scheduled without lowering the quality bar.
4. **Peak Hours Scheduling**:
   - 6 daily slots are scheduled across channel peak engagement hours: **12:00, 14:00, 17:00, 19:00, 20:00, 21:00** EST.

### Curation CLI Usage:
```bash
# Production daily curation run (Target: 6 slots, unattended auto-approval)
python agent/curate.py --target 6 --date tomorrow --no-review

# Simulation mode (No API charges, mock evaluation)
python agent/curate.py --target 6 --date tomorrow --dry-run

# Custom borderline policy
python agent/curate.py --target 6 --date tomorrow --no-review --borderline-policy approve_top
```

---

## 🚀 Phase 4: YouTube Shorts Upload & Daily Automation

Phase 4 automates uploading the 6 curated daily videos to YouTube as Shorts and orchestrates the end-to-end daily pipeline on Windows.

### 1. Google Cloud Console Setup

1. **Create Project**: Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project (e.g. `100B-Shorts-Bot`).
2. **Enable YouTube Data API v3**:
   - In APIs & Services $\rightarrow$ Library, search for **YouTube Data API v3** and click **Enable**.
3. **Configure OAuth Consent Screen**:
   - User Type: **External**.
   - App Name: `100B YouTube Shorts Bot`.
   - Add scopes:
     - `https://www.googleapis.com/auth/youtube.upload`
     - `https://www.googleapis.com/auth/youtube.readonly`
   - Publishing Status: Set to **In production** (if kept in "Testing", refresh tokens expire after 7 days).
4. **Create OAuth Client ID**:
   - In APIs & Services $\rightarrow$ Credentials, click **Create Credentials** $\rightarrow$ **OAuth client ID**.
   - Application Type: **Desktop Application**.
   - Name: `100B Desktop Client`.
   - Download the JSON file, rename it to `client_secret.json`, and place it in:
     `uploader/secrets/client_secret.json` (guarded by `.gitignore`).

---

### 2. Timezone & Quota Accounting

* **Channel Timezone & Display**: The pipeline's target dates and `--date tomorrow` resolution use **`America/New_York` (US Eastern Time)** as the scheduling source of truth for the US audience, while the dashboard displays and accepts times in **IST (`Asia/Kolkata`, UTC+5:30)**.
  * When running daily at 18:00 local time, "tomorrow" corresponds to tomorrow's date in New York.
  * The 6 daily slots (12:00, 14:00, 17:00, 19:00, 20:00, 21:00 ET) are converted to UTC RFC 3339 for YouTube `publishAt`, and are displayed in the dashboard in IST (e.g. 10:30 PM IST, 12:30 AM IST next day).
* **Daily Quota Budget**:
  * Default project quota: **10,000 units/day** (resets at midnight Pacific Time / PT).
  * `videos.insert` costs **1,600 units** (6 daily slots = 9,600 units).
  * `videos.list` costs **1 unit**.
  * Quota costs and daily caps are configured in `uploader/config.yaml` (`daily_cap: 10000`, `insert_cost: 1600`).
* **YouTube Audit & Private Lock**:
  * For unaudited/unverified API projects, YouTube may lock uploaded videos as `private`. The client detects this via post-upload `videos.list(part="status")` and logs an informative warning rather than crashing. Complete the YouTube API Verification in Cloud Console if public unlisted/scheduled publishing is restricted.

---

### 3. First-Run Checklist

Follow this sequence before automating:

1. **One-Time OAuth Authorization**:
   ```bash
   python upload.py auth
   ```
   * Opens your browser to grant YouTube upload permissions to your Google account.
   * Saves `uploader/secrets/token.json` for automatic non-interactive token refreshes.

2. **Dry-Run Upload Test**:
   ```bash
   python upload.py upload --date tomorrow --dry-run
   ```
   * Verifies schedule parsing, metadata formatting, and 9:16 video validation without consuming API quota.

3. **Single Private Test Upload**:
   ```bash
   python upload.py upload --date tomorrow --limit 1
   ```
   * Uploads exactly 1 video as a private draft (`first_run_private: true` safety net, `--limit 1`).

4. **Post-Upload Verification**:
   ```bash
   python upload.py verify --date tomorrow
   ```
   * Checks processing status, confirms resolution, and logs quota usage.

5. **Inspect Status Table**:
   ```bash
   python upload.py status --days 7
   ```

6. **Go Live**:
   * Add `--confirm-public` or set `upload.go_live: true` and `upload.first_run_private: false` in `uploader/config.yaml` to enable scheduled publishing.

---

### 4. Upload CLI Commands

```bash
# Authenticate OAuth
python upload.py auth

# Upload scheduled batch for tomorrow (evening before run)
python upload.py upload --date tomorrow --confirm-public

# Realtime mode (run every 15 mins to upload due slots)
python upload.py run-due --confirm-public

# Check processing status
python upload.py verify --date tomorrow

# Display last 7 days status table
python upload.py status --days 7

# Retry failed slots
python upload.py retry-failed --date tomorrow
```

---

### 5. Windows Task Scheduler Setup

Automate the daily pipeline by scheduling `run_daily.py`:

#### Recommended Schedule: Daily at 18:00 (6:00 PM) Local Time

1. Open **Task Scheduler** in Windows (`taskschd.msc`).
2. Click **Create Task...** (not Basic Task):
   * **General**:
     * Name: `100B Daily Video Bot`
     * Select: **Run whether user is logged on or not** (or Run only when user is logged on)
     * Check: **Run with highest privileges**
   * **Triggers**:
     * New Trigger: **Daily** at `18:00:00`
   * **Actions**:
     * Action: **Start a program**
     * Program/script: `C:\Python314\python.exe` (or your Python path)
     * Add arguments: `run_daily.py --date tomorrow --confirm-public`
     * Start in: `C:\Drive-D\python tut\100B`
   * **Conditions**:
     * ✅ **Wake the computer to run this task**
   * **Settings**:
     * ✅ **Run task as soon as possible after a missed start**
     * If the task fails, restart every: `10 minutes` (up to 3 times)
     * Stop the task if it runs longer than: `2 hours`

#### Optional: Realtime Runner (Every 15 Minutes)
If running in realtime mode (`upload.mode: "realtime"`), create a secondary task:
* Trigger: **Daily**, repeat every **15 minutes** for a duration of **1 day**.
* Program: `C:\Python314\python.exe`
* Arguments: `upload.py run-due --confirm-public`
* Start in: `C:\Drive-D\python tut\100B`
* Settings: ✅ **Run as soon as possible after a missed start**

---

## 📊 Phase 5 — Local Pipeline Dashboard

A clean, responsive local dashboard for monitoring and managing the automated YouTube Shorts pipeline, built strictly on design tokens from Figma (Frames: Agent `3:2`, Schedule `4:63`, YouTube `5:111`, components `2:126`).

### 1. Launching the Dashboard

Start the FastAPI backend (serves the bundled React SPA):
```bash
python dashboard.py
```
Then open [http://127.0.0.1:8765](http://127.0.0.1:8765) in your browser.
* Bound strictly to `127.0.0.1` (localhost only) with host header validation and security headers.
* Read-only SQLite database access (`mode=ro`).

### 2. Frontend Development & Build

The frontend is built with React 19, TypeScript, Tailwind CSS v4, `@fontsource/bricolage-grotesque`, and `@fontsource/instrument-sans`.

```bash
# Navigate to frontend directory
cd dashboard/frontend

# Install dependencies (if not already done)
npm install

# Run frontend unit tests (Vitest)
npm run test

# Build static production bundle (outputs to dashboard/frontend/dist)
npm run build

# Start Vite dev server with proxy to backend (port 5173 -> 8765)
npm run dev
```

### 3. Dashboard Sections

1. **Agent View**:
   - 7-day picker with daily found / kept metrics and live running indicator dot.
   - Proportional funnel ribbon: dropped before judging, rejected by judge, stored in reserve, selected for tomorrow.
   - Tomorrow's curated lineup with hybrid score chips and 9:16 thumbnails. Clicking any lineup row immediately jumps to the YouTube section with that clip selected.
   - Reserve pool carousel, rejection reasons frequency chart, and source subreddit yield analysis.

2. **Schedule View**:
   - Daily volume stepper (1 to 12 videos per day).
   - Presets chips: *Evening heavy*, *Even spread*, and *Morning and night*.
   - 24-hour visual timeline divided into 4 day-parts (Night, Morning, Afternoon, Evening) with draggable markers snapping strictly to 15-minute intervals (also controllable via Left/Right arrow keys).
   - 12-hour editable slot times with collision avoidance.
   - Atomic config updates to `agent/config.yaml` (`target_count`, `peak_hours`) and `uploader/config.yaml` (`max_uploads_per_day`) preserving comments with `.bak` backups.

3. **YouTube View**:
   - Day switcher: *Today*, *Yesterday*, or custom native date picker.
   - Daily broadcast rundown with live views, likes, and status badges (*Published*, *On air*, *Scheduled*, *Draft*, *Failed*).
   - 9:16 player panel: YouTube embed for public videos, HTTP Range streaming (`/api/video/{post_id}/stream`) for private, draft, or local playback, and ffmpeg thumbnail generation (`/api/thumbs/{post_id}.jpg`).
   - "Open on YouTube" and "Open in Studio" shortcuts.
   - Auto-refreshes every 30 seconds when tab is active (automatically pauses when browser tab is hidden).

### 4. YouTube Manage Scope for Retiming

To enable live retiming of scheduled clips directly from the dashboard:
```bash
python upload.py auth --manage
```
This authenticates with the `https://www.googleapis.com/auth/youtube` scope and stores the manage token in `uploader/secrets/token_manage.json`. When saving a modified schedule, the dashboard will automatically call `videos.update` to shift `publishAt` for tomorrow's scheduled clips without touching public videos.

---

## 🛡️ Phase 6.1: Content ID Claims Sync & Video Lifecycle

Automated Content ID claim detection and lifecycle management via Gmail read-only notifications, avoiding non-compliant browser scraping.

### 1. Commands
```bash
# One-time interactive OAuth setup for Gmail read-only (houseinyourhand@gmail.com)
python claims_sync.py auth

# Check Gmail connection status, quota, and sync state
python claims_sync.py status

# Redacted dry-run inspection of real YouTube copyright emails
python claims_sync.py inspect

# Execute full sync (run manually or scheduled)
python claims_sync.py sync --dry-run   # Simulation mode (default for 3 days)
python claims_sync.py sync             # Live execution
```

### 2. Windows Task Scheduler Setup (Runs Every 30 Minutes)
To schedule automatic claims sync in Windows PowerShell (Administrator):
```powershell
$action = New-ScheduledTaskAction -Execute "python" -Argument "claims_sync.py sync" -WorkingDirectory "C:\Drive-D\python tut\100B"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "100B_Claims_Sync" -Action $action -Trigger $trigger -Settings $settings
```
*Note: `-StartWhenAvailable` ensures the task runs as soon as possible after a missed start (e.g., if the PC was asleep or restarted).*

### 3. Safety Controls & Safeguards
- **Isolated Credentials**: Dedicated `token_gmail.json` with `gmail.readonly` scope only. Never reused or mixed with YouTube upload tokens.
- **Dry-Run Gating**: Default `claims.dry_run: true` for the first 3 days. Can be toggled live via the Claims Dashboard.
- **Reversible Holds**: Claimed scheduled videos are immediately marked private with `publishAt` removed to prevent publication, while deletion is simulated during dry run.
- **Pipeline Boundary**: Only videos recorded in the `uploads` table are matched. The ~30 older legacy channel videos are strictly ignored and recorded under "Unmatched".
- **Strike Alert Escalation**: Copyright strikes (`is_strike=True`) trigger urgent Telegram/Windows desktop alerts and **never** auto-delete.
- **Daily Rate Cap**: Maximum 6 automated actions per day (`max_auto_actions_per_day: 6`).
- **Live YouTube Probe**: Each sync checks `videos.list` to verify pipeline videos exist. Missing videos are marked `deleted`, freeing their slot for `curate.py --fill-slots <date>`.
- **Studio Paste Fallback**: Direct text parsing in the Dashboard for manual claims copied out of YouTube Studio.

---

---

## 🖥 Windows Drag & Drop Helper

Double-click or drag-and-drop any video onto [`run.bat`](file:///c:/Drive-D/python%20tut/100B/run.bat) to launch the editor directly in Windows File Explorer.


