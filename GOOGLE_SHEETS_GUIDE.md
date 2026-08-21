# Google Sheets Integration - Updated Logic

## What Changed

### SPRINT Sheet (`1qegkgolGQDbkAB7zCtxEG-PgCJ8lapC9-kJZUoeirCA`)
**Smart Filtering: Only shows tasks with dates in the NEXT 15 DAYS**

```
TODAY ──────→ [15 DAYS] ←──
|                         |
Only tasks in this range are shown
```

- Automatically filters by date in Column B
- Supports multiple date formats:
  - `2026-08-21` (ISO format)
  - `21.08.2026` (Russian format)
  - `21/08/2026` (European format)
- Shows only tasks where: **Today ≤ Task Date ≤ Today + 15 days**
- Sorts results by date (earliest first)

### BACKLOG Sheet (`1q478v3TCARlagB_6tR4g1rz9HpHBJU6WYftyBbIOodQ`)
**Smart Filtering: Only shows INCOMPLETE tasks**

- Filters out tasks with status "готово" (done)
- Also filters out: "done", "выполнено", "completed"
- Shows all other tasks (любой статус)
- Works with Column C (Status column)

## Sheet Structure Expected

### Sprint Sheet Format
```
| Задача           | Дата      | Статус | Примечание |
|------------------|-----------|--------|-----------|
| Task 1           | 21.08.2026| Ready  | Note 1     |
| Task 2           | 25.08.2026| In Prog| Note 2     |
```
**Important**: Column B must contain dates

### Backlog Sheet Format
```
| Задача           | Описание   | Статус    | Ответств  |
|------------------|-----------|-----------|-----------|
| Task A           | Desc A    | Начало    | Someone   |
| Task B           | Desc B    | готово    | Someone   |
```
**Important**: Column C must contain status

## How to Test

1. **Update your .env:**
   ```bash
   nano ~/.env
   ```
   Should have:
   ```
   SPRINT_SHEET_ID=1qegkgolGQDbkAB7zCtxEG-PgCJ8lapC9-kJZUoeirCA
   BACKLOG_SHEET_ID=1q478v3TCARlagB_6tR4g1rz9HpHBJU6WYftyBbIOodQ
   ```

2. **Ensure Google credentials exist:**
   ```bash
   ls -la ~/.google_credentials.json
   ```
   If not found, download from [Google Cloud Console](https://console.cloud.google.com/)

3. **Run the app:**
   ```bash
   python3 ~/cyber_panel.py
   ```

4. **First run** will ask for OAuth authorization - approve it

## Troubleshooting

### Tasks not appearing?

**Check 1: Date Format**
- Make sure dates are in one of the supported formats
- Invalid dates are ignored

**Check 2: Date Range**
- Sprint sheet only shows tasks within next 15 days
- If all tasks are further in the future, none will appear
- Example: Today is 2026-08-21
  - ✓ Shows: 2026-08-25 (4 days away)
  - ✗ Shows: 2026-09-10 (20 days away - beyond 15-day window)

**Check 3: Status Filter (Backlog)**
- Make sure "готово" status is spelled exactly: `готово`
- Case-insensitive: `ГОТОВО`, `Готово` also work
- Also filters: `done`, `выполнено`

### Permissions Error?
- Check that Google API scopes include:
  - `https://www.googleapis.com/auth/spreadsheets.readonly`
- Re-authenticate:
  ```bash
  rm ~/.google_sheets_token.json
  python3 ~/cyber_panel.py  # Will prompt for auth again
  ```

## API Data Flow

```
┌─────────────────┐
│  Google Sheets  │
│    (2 tabs)     │
└────────┬────────┘
         │
    (Read data)
         │
         ↓
┌──────────────────────┐
│   TasksWorker Thread │
├──────────────────────┤
│ Sprint Filtering:    │
│ - Parse dates        │
│ - Filter by 15 days  │
│ - Sort by date       │
│                      │
│ Backlog Filtering:   │
│ - Check status       │
│ - Remove "готово"    │
└────────┬─────────────┘
         │
    (Updates UI)
         │
         ↓
┌─────────────────────┐
│  Extended HUD Panel │
│ - Sprint ИМ         │
│ - Задачи ДЛЯ Юры   │
└─────────────────────┘
```

## Advanced Configuration

### Change the date range (currently 15 days)

Edit `cyber_panel.py`, find:
```python
cutoff_date = today + timedelta(days=15)  # Change 15 to whatever you want
```

Examples:
- `timedelta(days=7)` - Show only next week
- `timedelta(days=30)` - Show next month
- `timedelta(days=365)` - Show next year

### Add more status filters

Find in `cyber_panel.py`:
```python
if "готово" not in status and "done" not in status and "выполнено" not in status:
```

Add more:
```python
if "готово" not in status and "done" not in status and "выполнено" not in status and "skip" not in status:
```

### Change which columns are read

Find:
```python
range="A2:D100"  # Reads columns A to D, rows 2-100
```

Change to:
```python
range="A2:F50"   # Reads columns A to F, rows 2-50
```

## Performance

- Updates every **60 seconds** (adjustable in code)
- Runs in background thread - doesn't freeze UI
- Google API calls: ~1 per minute (well within free tier limits)

---

**Sheet IDs already configured in `.env`** ✓
