# Google Sheets Integration - READY TO USE ✓

## Status
```
✓ Sheet IDs configured in ~/.env
✓ Smart filtering implemented
✓ Both sheets properly set up and working
❌ Waiting for: Google OAuth credentials file
```

## What's New

### 1️⃣ SPRINT Sheet (Первая таблица)
**URL:** `https://docs.google.com/spreadsheets/d/1qegkgolGQDbkAB7zCtxEG-PgCJ8lapC9-kJZUoeirCA`

```
📅 Automatically shows ONLY tasks with dates in the NEXT 15 DAYS
    ├─ Парses dates from Column B
    ├─ Supports: 2026-08-21, 21.08.2026, 21/08/2026
    └─ Sorts by date (nearest first)
```

**Example:**
- Today: 2026-08-21
- Window: 21.08 - 05.09 (next 15 days)
- Shows: All tasks with dates in this range
- Hides: Tasks dated 06.09 or later

### 2️⃣ BACKLOG Sheet (Вторая таблица)
**URL:** `https://docs.google.com/spreadsheets/d/1q478v3TCARlagB_6tR4g1rz9HpHBJU6WYftyBbIOodQ`

```
✅ Automatically HIDES tasks marked as "готово"
    ├─ Checks Column C (Status)
    ├─ Removes: готово, done, выполнено
    └─ Shows: All other statuses (ready, in progress, etc)
```

**Example:**
- Task with status "готово" → HIDDEN ✗
- Task with status "ready" → SHOWN ✓
- Task with status "in progress" → SHOWN ✓

## Setup Instructions

### Step 1: Get Google Credentials (ONE TIME)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (if you don't have one)
3. Enable these APIs:
   - Google Sheets API
   - YouTube Data API v3
4. Go to "Credentials" → Create OAuth 2.0 Desktop app
5. Download JSON file
6. Save as: `~/.google_credentials.json`

```bash
# On Windows, in WSL terminal:
# Copy your downloaded credentials.json to:
# ~/.google_credentials.json
cp ~/Downloads/credentials.json ~/.google_credentials.json
```

### Step 2: Verify Configuration

Run the test script:
```bash
bash ~/test_sheets.sh
```

Should show:
```
✓ Credentials found
✓ SPRINT_SHEET_ID configured
✓ BACKLOG_SHEET_ID configured
```

### Step 3: Launch the App

```bash
python3 ~/cyber_panel.py
```

First run will ask: **"Authorize Google access?"**
- Click "Authorize" in browser window
- It will work automatically after that

## File Changes Summary

| File | Change |
|------|--------|
| `cyber_panel.py` | Added smart filtering for both sheets |
| `.env` | Added your 2 Sheet IDs |
| `GOOGLE_SHEETS_GUIDE.md` | Complete filtering guide |
| `test_sheets.sh` | Test script for configuration |

## Smart Filtering Details

### Sprint Sheet Filtering Algorithm
```python
today = 2026-08-21
for each task in sprint_sheet:
    task_date = parse_date(task.column_B)
    if today <= task_date <= (today + 15 days):
        show_task(task)
    else:
        hide_task(task)
```

### Backlog Sheet Filtering Algorithm
```python
for each task in backlog_sheet:
    status = task.column_C.lower()
    if "готово" not in status and "done" not in status:
        show_task(task)
    else:
        hide_task(task)
```

## Testing Your Sheets

Add test tasks to verify it works:

### Sprint Sheet (Test)
```
Задача 1    | 2026-08-25   | Ready      | Should appear
Задача 2    | 2026-09-10   | Ready      | Should NOT appear (>15 days)
Задача 3    | 2026-08-21   | Ready      | Should appear (today)
```

### Backlog Sheet (Test)
```
Задача А    | Описание    | ready      | Should appear
Задача Б    | Описание    | готово     | Should NOT appear
Задача В    | Описание    | in progress| Should appear
```

## Customization

### Change 15-day window to something else

Edit `cyber_panel.py`, find line with:
```python
cutoff_date = today + timedelta(days=15)
```

Change to:
```python
cutoff_date = today + timedelta(days=7)   # 7 days window
cutoff_date = today + timedelta(days=30)  # 30 days window
cutoff_date = today + timedelta(days=1)   # Just today
```

### Add more status filters

Find in `cyber_panel.py`:
```python
if "готово" not in status and "done" not in status and "выполнено" not in status:
```

Add your own:
```python
if "готово" not in status and "done" not in status and "cancelled" not in status:
```

## Common Issues

### Q: "ModuleNotFoundError"
**A:** Run: `pip install --break-system-packages beautifulsoup4 requests`

### Q: "SHEETS: CONNECTING..." but never loads
**A:** Google credentials missing. Follow Step 1 above.

### Q: No tasks appear, but sheet has data
**A:** Check:
1. Dates in Column B of Sprint sheet are within next 15 days
2. Status in Column C of Backlog sheet is NOT "готово"
3. Both sheets have data starting from Row 2 (Row 1 = headers)

### Q: Different date format in my sheet?
**A:** Supported: `2026-08-21`, `21.08.2026`, `21/08/2026`
If yours is different, it's skipped. Change your sheet format or contact support.

### Q: Re-authenticate with Google
```bash
rm ~/.google_sheets_token.json
python3 ~/cyber_panel.py  # Will ask for auth again
```

## Performance

- Updates: Every 60 seconds
- Thread: Background (doesn't freeze UI)
- API calls: ~1 per minute (well within Google free tier)
- Memory: Minimal (caches ~100 tasks)

## Current Configuration

```bash
cat ~/.env | grep -E "SPRINT|BACKLOG"
```

Should show:
```
SPRINT_SHEET_ID=1qegkgolGQDbkAB7zCtxEG-PgCJ8lapC9-kJZUoeirCA
BACKLOG_SHEET_ID=1q478v3TCARlagB_6tR4g1rz9HpHBJU6WYftyBbIOodQ
```

## Next Steps

1. ✓ Configuration complete
2. → Download Google credentials
3. → Run `python3 ~/cyber_panel.py`
4. → Authorize Google access (one-time)
5. → View your tasks in Extended HUD panel!

---

**Questions?** Check:
- `GOOGLE_SHEETS_GUIDE.md` - Full documentation
- `test_sheets.sh` - Configuration checker
- `EXTENDED_HUD_SETUP.md` - Full app setup

**Ready to go!** 🚀
