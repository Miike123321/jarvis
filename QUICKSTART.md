# Quick Start Guide - Cyber Panel with Extended HUD

## ✅ Dependencies Installed

All required Python packages are now installed:
- ✓ PyQt6 & PyQt6-WebEngine
- ✓ beautifulsoup4 & requests
- ✓ Google APIs (Sheets, Calendar, YouTube)
- ✓ Telethon (Telegram)
- ✓ psutil, python-dotenv

## 🚀 Quick Start

### Option 1: Using the startup script
```bash
bash ~/start_cyber_panel.sh
```

### Option 2: Direct command
```bash
python3 ~/cyber_panel.py
```

## ⚙️ Configuration (REQUIRED before first run)

### 1. Setup environment variables
```bash
cp ~/.env.example ~/.env
nano ~/.env
```

### 2. Google Sheets Setup (for task management)
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project
3. Enable "Google Sheets API"
4. Download OAuth credentials as JSON
5. Save as `~/google_credentials.json`
6. Add your sheet IDs to `.env`:
   ```
   SPRINT_SHEET_ID=your_sprint_sheet_id
   BACKLOG_SHEET_ID=your_backlog_sheet_id
   ```

### 3. YouTube API Setup (for video recommendations)
1. In Google Cloud Console, enable "YouTube Data API v3"
2. Create an API Key (not OAuth)
3. Add to `.env`:
   ```
   YOUTUBE_API_KEY=your_youtube_api_key
   ```

### 4. Optional: Telegram Integration
1. Go to [Telegram Apps](https://my.telegram.org/apps)
2. Create an app to get API ID and hash
3. Add to `.env`:
   ```
   TELEGRAM_API_ID=your_id
   TELEGRAM_API_HASH=your_hash
   TELEGRAM_PHONE=+1234567890
   ```

## 🖥️ System Requirements

### Minimum
- WSL2 (Windows Subsystem for Linux)
- Python 3.9+
- Display output capability

### Recommended
- **Dual monitors** for best experience
  - Primary: Main cyber_panel dashboard
  - Secondary: Extended HUD with tasks, news, and financial data

### Optional
- Second monitor connected (Extended HUD will auto-detect and use it)
- X server (for WSL display) or use Windows Python installation

## 🔧 Troubleshooting

### "No module named 'X'"
All dependencies should be installed. Verify:
```bash
python3 ~/check_imports.py
```

### Extended HUD not appearing on second monitor
1. Ensure second monitor is connected and active
2. Check WSL display settings: `echo $DISPLAY`
3. Extended HUD auto-launches when second screen detected

### Tasks not loading from Google Sheets
1. Verify `~/google_credentials.json` exists
2. Check sheet IDs in `.env` are correct
3. Grant read permissions to Google API
4. First run will prompt for OAuth authorization

### Financial data not updating
1. Check internet connection
2. Verify API keys are valid in `.env`
3. Check API rate limits:
   - CoinGecko: 10-50 calls/min (free)
   - exchangerate-api: 1500/month (free)

### News not loading
1. Verify internet connection
2. Some sites block automated requests
3. Check if TechCrunch website structure has changed

## 📊 Features Overview

### Primary Panel (First Monitor)
- **NODE TELEMETRY**: CPU/RAM gauges
- **EMBEDDED TERMINAL**: Interactive bash shell
- **CALENDAR INTEGRATION**: Google Calendar events
- **WEATHER**: Real-time Odesa weather
- **TELEGRAM**: Latest incoming messages
- **SHORTCUTS**: Quick access to Chrome, Drive, RDP

### Extended HUD (Second Monitor) - Auto-launches
- **SPRINT ИМ**: Active sprint tasks from Google Sheets
- **ЗАДАЧИ ДЛЯ ЮРЫ**: Backlog items for your team
- **ADD TASK**: Interactive inline form (no dialogs)
- **FINANCIAL TERMINAL**: 
  - BTC/USDT price with 24h change
  - USD/UAH exchange rate
  - Radial visual indicators
- **MEDIA CAROUSEL**: Latest YouTube recommendations
- **NEWS HUB**: 
  - Animated news ticker
  - Clickable news articles
  - Real-time updates

## 🎨 Visual Theme

Dark cyberpunk aesthetic:
- Background: Dark graphite (#0b1113)
- Accents: Bright cyan neon (#73f6de)
- Text: Light gray (#d6fff7)
- No window frames, only floating radial/arc elements

## ⌨️ Keyboard Shortcuts

| Action | Key |
|--------|-----|
| Run app | `python3 ~/cyber_panel.py` |
| Exit | `Ctrl+C` or close window |
| Add task | Click "+ ДОБАВИТЬ В БЭКЛОГ" button |
| Play video | Double-click on title |
| Open news | Click on news item |
| Copy terminal | Click "COPY ALL" button |
| Paste terminal | Click "PASTE" button |

## 🔄 Auto-updates

- **Tasks**: Every 60 seconds
- **Financial data**: Every 60 seconds
- **Calendar events**: Every 5 minutes
- **YouTube videos**: Every 2 minutes
- **News**: Every 5 minutes

## 🐛 Debug Mode

Enable debug output:
```bash
python3 -u ~/cyber_panel.py 2>&1 | tee cyber_panel.log
```

This logs all output to both terminal and `cyber_panel.log`

## 📚 More Information

See `EXTENDED_HUD_SETUP.md` for:
- Detailed API configuration
- Custom news sources
- Performance optimization
- Rate limit information
- Advanced customization

---

**Ready to launch?** Run: `python3 ~/cyber_panel.py`
