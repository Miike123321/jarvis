import sys
import os
import asyncio
import shutil
import threading
import json
import subprocess
import webbrowser
import urllib.request
from datetime import datetime, timezone, timedelta
import psutil
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse

load_dotenv(os.path.expanduser("~/.env"))

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QLineEdit,
    QInputDialog,
    QDialog,
    QStyle,
    QScrollArea,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineSettings
from PyQt6.QtGui import QIcon, QPixmap, QColor, QPainter, QPen, QFont, QPolygon
from PyQt6.QtCore import Qt, QUrl, QTimer, QThread, QProcess, QProcessEnvironment, QSize, pyqtSignal, QPoint, QRect
from telethon import TelegramClient
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Блокировка аппаратного ускорения для подавления ошибок MESA/libEGL в среде WSL
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
# Read-write scope so tasks added in the UI can be appended to the backlog sheet
GOOGLE_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def is_wsl() -> bool:
    if "WSL_DISTRO_NAME" in os.environ:
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as version_file:
            return "microsoft" in version_file.read().lower()
    except OSError:
        return False


def run_google_oauth(flow):
    if is_wsl():
        original_open = webbrowser.open
        webbrowser.open = lambda authorization_url, *args, **kwargs: subprocess.Popen(
            ["cmd.exe", "/c", "start", "", authorization_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).poll() is None
        try:
            return flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
        finally:
            webbrowser.open = original_open
    return flow.run_local_server(port=0, open_browser=True)


ENV_PATH = os.path.expanduser("~/.env")
GOOGLE_CREDENTIALS_PATH = os.path.expanduser("~/google_credentials.json")

SETUP_FIELDS = [
    ("TELEGRAM_API_ID", "Telegram API ID", False),
    ("TELEGRAM_API_HASH", "Telegram API Hash", False),
    ("TELEGRAM_PHONE", "Telegram Phone (+380...)", False),
    ("YOUTUBE_API_KEY", "YouTube Data API Key", False),
    ("SPRINT_SHEET_ID", "Google Sheet ID — Sprint", False),
    ("BACKLOG_SHEET_ID", "Google Sheet ID — Backlog", False),
    ("TELEGRAM_EXE_PATH", "Telegram.exe path", True),
    ("CHROME_EXE_PATH", "chrome.exe path", True),
    ("RDP_FILE_PATH", "RDP file path", True),
]

REQUIRED_ENV_KEYS = [key for key, _, optional in SETUP_FIELDS if not optional]


def missing_env_keys():
    return [key for key in REQUIRED_ENV_KEYS if not os.getenv(key)]


def save_env_values(values):
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as env_file:
            lines = env_file.read().splitlines()
    for key, value in values.items():
        if not value:
            continue
        for index, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as env_file:
        env_file.write("\n".join(lines) + "\n")


class FirstRunSetupDialog(QDialog):
    """One-time setup form shown on startup while required settings are missing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("JARVIS — First Run Setup")
        self.setMinimumWidth(520)
        self.setStyleSheet(
            """
            QWidget {
                color: #d6fff7;
                background-color: #0b1113;
                font-family: 'Cascadia Mono', 'DejaVu Sans Mono', monospace;
            }
            QLineEdit {
                background-color: #101f21;
                border: 1px solid #286e6b;
                color: #b8eee4;
                padding: 6px;
                border-radius: 4px;
            }
            QPushButton {
                color: #bffef1;
                background-color: #142326;
                border: 1px solid #286e6b;
                border-radius: 6px;
                padding: 7px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: #071112;
                background-color: #73f6de;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        header = QLabel("SETUP REQUIRED\n\nEnter your keys below — they will be saved to ~/.env")
        header.setStyleSheet("color: #73f6de; font-size: 13px; font-weight: bold;")
        layout.addWidget(header)

        self.inputs = {}
        for key, label, optional in SETUP_FIELDS:
            field_label = QLabel(f"{label}{' (optional)' if optional else ' *'}")
            field_label.setStyleSheet("color: #9effef; font-size: 11px;")
            layout.addWidget(field_label)
            input_field = QLineEdit()
            input_field.setText(os.getenv(key, ""))
            self.inputs[key] = input_field
            layout.addWidget(input_field)

        credentials_row = QHBoxLayout()
        credentials_exists = os.path.exists(GOOGLE_CREDENTIALS_PATH)
        self.credentials_status = QLabel(
            "google_credentials.json: FOUND" if credentials_exists
            else "google_credentials.json: NOT FOUND"
        )
        self.credentials_status.setStyleSheet(
            f"color: {'#73f6de' if credentials_exists else '#ff4444'}; font-size: 11px;"
        )
        browse_button = QPushButton("BROWSE...")
        browse_button.clicked.connect(self.pick_credentials)
        credentials_row.addWidget(self.credentials_status, 1)
        credentials_row.addWidget(browse_button)
        layout.addLayout(credentials_row)

        buttons = QHBoxLayout()
        save_button = QPushButton("SAVE && START")
        save_button.clicked.connect(self.save_and_close)
        skip_button = QPushButton("SKIP")
        skip_button.clicked.connect(self.reject)
        buttons.addWidget(save_button)
        buttons.addWidget(skip_button)
        layout.addLayout(buttons)

    def pick_credentials(self):
        source, _ = QFileDialog.getOpenFileName(
            self, "Select google_credentials.json", os.path.expanduser("~"), "JSON files (*.json)"
        )
        if source:
            shutil.copy(source, GOOGLE_CREDENTIALS_PATH)
            self.credentials_status.setText("google_credentials.json: COPIED")
            self.credentials_status.setStyleSheet("color: #73f6de; font-size: 11px;")

    def save_and_close(self):
        save_env_values({key: field.text().strip() for key, field in self.inputs.items()})
        self.accept()


def append_backlog_task(name, description):
    """Append a task row to the backlog Google Sheet. Returns (ok, message)."""
    backlog_sheet_id = os.getenv("BACKLOG_SHEET_ID", "")
    if not backlog_sheet_id:
        return False, "BACKLOG_SHEET_ID not set"
    token_path = os.path.expanduser("~/.google_sheets_token.json")
    credentials = None
    if os.path.exists(token_path):
        credentials = Credentials.from_authorized_user_file(token_path, GOOGLE_SHEETS_SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        return False, "Sheets not authorized (restart app to sign in)"
    service = build("sheets", "v4", credentials=credentials)
    try:
        service.spreadsheets().values().append(
            spreadsheetId=backlog_sheet_id,
            range="A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[name, description, "Начало"]]},
        ).execute()
    except Exception as error:
        if "insufficient" in str(error).lower() or "403" in str(error):
            return False, "No write access: delete ~/.google_sheets_token.json and restart to re-auth"
        raise
    return True, "Saved to backlog sheet"


def save_task_to_backlog_sheet(name, description, notifier, item):
    """Save a task in the background; result arrives via notifier.sheet_save_result."""
    def worker():
        try:
            ok, message = append_backlog_task(name, description)
        except Exception as error:
            ok, message = False, str(error)[:60]
        notifier.sheet_save_result.emit(item, ok, message)

    threading.Thread(target=worker, daemon=True).start()


# Extended HUD Data Workers
class FinancialDataWorker(QThread):
    data_updated = pyqtSignal(dict)
    status_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                # Fetch BTC/USDT from CoinGecko
                btc_response = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
                    timeout=10
                )
                btc_data = btc_response.json().get("bitcoin", {})
                btc_price = btc_data.get("usd", 0)
                btc_change = btc_data.get("usd_24h_change", 0)

                # Fetch USD/UAH exchange rate
                try:
                    uah_response = requests.get(
                        "https://api.exchangerate-api.com/v4/latest/USD",
                        timeout=10
                    )
                    uah_rate = uah_response.json()["rates"].get("UAH", 0)
                except:
                    uah_rate = 41.0  # Fallback

                self.data_updated.emit({
                    "btc_price": btc_price,
                    "btc_change": btc_change,
                    "uah_rate": uah_rate
                })
            except Exception as e:
                self.status_changed.emit(f"FINANCIAL: {str(e)[:50]}")

            for _ in range(60):
                if not self.running:
                    return
                threading.Event().wait(1)


class TasksWorker(QThread):
    tasks_updated = pyqtSignal(list, list)  # sprint_tasks, backlog_tasks
    status_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                # Try to fetch from Google Sheets
                credentials_path = os.path.expanduser("~/google_credentials.json")
                token_path = os.path.expanduser("~/.google_sheets_token.json")
                
                credentials = None
                if os.path.exists(token_path):
                    credentials = Credentials.from_authorized_user_file(
                        token_path, GOOGLE_SHEETS_SCOPES
                    )
                
                if credentials and credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                
                if not credentials or not credentials.valid:
                    if not os.path.exists(credentials_path):
                        self.status_changed.emit("TASKS: NO CREDENTIALS")
                        self.tasks_updated.emit([], [])
                        for _ in range(60):
                            if not self.running:
                                return
                            threading.Event().wait(1)
                        continue
                    
                    flow = InstalledAppFlow.from_client_secrets_file(
                        credentials_path, GOOGLE_SHEETS_SCOPES
                    )
                    credentials = run_google_oauth(flow)
                
                with open(token_path, "w", encoding="utf-8") as token_file:
                    token_file.write(credentials.to_json())
                
                service = build("sheets", "v4", credentials=credentials)
                
                # Get sheet IDs from environment
                sprint_sheet_id = os.getenv("SPRINT_SHEET_ID", "")
                backlog_sheet_id = os.getenv("BACKLOG_SHEET_ID", "")
                
                sprint_tasks = []
                backlog_tasks = []
                
                if sprint_sheet_id:
                    try:
                        result = service.spreadsheets().values().get(
                            spreadsheetId=sprint_sheet_id,
                            range="A2:D100"
                        ).execute()
                        all_sprint = result.get("values", [])
                        
                        # Filter: only tasks with dates in the next 15 days from today
                        today = datetime.now()
                        cutoff_date = today + timedelta(days=15)
                        
                        for task in all_sprint:
                            if len(task) >= 2:
                                try:
                                    # Try to parse date from column B (assuming format like "2026-08-21" or "21.08.2026")
                                    date_str = task[1].strip()
                                    
                                    # Try different date formats
                                    task_date = None
                                    for fmt in ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"]:
                                        try:
                                            task_date = datetime.strptime(date_str, fmt)
                                            break
                                        except ValueError:
                                            continue
                                    
                                    if task_date and today <= task_date <= cutoff_date:
                                        sprint_tasks.append(task)
                                except:
                                    # If date parsing fails, include the task anyway
                                    sprint_tasks.append(task)
                        
                        # Sort by date
                        sprint_tasks.sort(key=lambda x: x[1] if len(x) > 1 else "")
                    except Exception as e:
                        self.status_changed.emit(f"SPRINT LOAD ERROR: {str(e)[:40]}")
                
                if backlog_sheet_id:
                    try:
                        result = service.spreadsheets().values().get(
                            spreadsheetId=backlog_sheet_id,
                            range="A2:D100"
                        ).execute()
                        all_backlog = result.get("values", [])
                        
                        # Filter: only tasks without "готово" status
                        for task in all_backlog:
                            if len(task) >= 1:
                                # Check status in column C (or any column that contains status)
                                status = task[2].lower() if len(task) > 2 else ""
                                
                                # Skip tasks marked as "готово" (done)
                                if "готово" not in status and "done" not in status and "выполнено" not in status:
                                    backlog_tasks.append(task)
                    except Exception as e:
                        self.status_changed.emit(f"BACKLOG LOAD ERROR: {str(e)[:40]}")
                
                self.tasks_updated.emit(sprint_tasks, backlog_tasks)
                
            except Exception as e:
                self.status_changed.emit(f"TASKS: {str(e)[:50]}")
            
            for _ in range(60):
                if not self.running:
                    return
                threading.Event().wait(1)


class YouTubeWorker(QThread):
    videos_updated = pyqtSignal(list)  # list of (title, video_id, thumbnail)
    status_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                youtube_api_key = os.getenv("YOUTUBE_API_KEY", "")
                if not youtube_api_key:
                    self.status_changed.emit("YOUTUBE: NO API KEY")
                    for _ in range(60):
                        if not self.running:
                            return
                        threading.Event().wait(1)
                    continue
                
                from googleapiclient.discovery import build as yt_build
                youtube = yt_build("youtube", "v3", developerKey=youtube_api_key)
                
                # Get recommendations (search for recent videos)
                request = youtube.search().list(
                    q="AI marketing technology",
                    part="snippet",
                    maxResults=3,
                    order="date",
                    type="video"
                )
                response = request.execute()
                
                videos = []
                for item in response.get("items", []):
                    video_id = item["id"].get("videoId")
                    title = item["snippet"].get("title", "")
                    thumbnail = item["snippet"]["thumbnails"]["default"]["url"]
                    videos.append((title, video_id, thumbnail))
                
                self.videos_updated.emit(videos)
            except Exception as e:
                self.status_changed.emit(f"YOUTUBE: {str(e)[:50]}")
            
            for _ in range(120):
                if not self.running:
                    return
                threading.Event().wait(1)


class NewsWorker(QThread):
    news_updated = pyqtSignal(list)  # list of (title, link)
    status_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                news_items = []
                
                # Monitor AI news from major sources via RSS/Atom feeds
                sources = [
                    ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch AI"),
                    ("https://feeds.arstechnica.com/arstechnica/technology-lab", "Ars Technica"),
                    ("https://www.wired.com/feed/tag/ai/latest/rss", "Wired AI"),
                    ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "The Verge AI"),
                ]

                for url, source_name in sources:
                    try:
                        response = requests.get(url, timeout=10, headers={
                            "User-Agent": "Mozilla/5.0"
                        })
                        try:
                            soup = BeautifulSoup(response.content, "xml")
                        except Exception:
                            soup = BeautifulSoup(response.content, "html.parser")

                        # RSS uses <item>, Atom uses <entry> with <link href="..."/>
                        for entry in soup.find_all(["item", "entry"])[:3]:
                            title_tag = entry.find("title")
                            link_tag = entry.find("link")
                            title = title_tag.get_text().strip() if title_tag else ""
                            link = ""
                            if link_tag:
                                link = link_tag.get("href") or link_tag.get_text().strip()
                            if title and link:
                                news_items.append((title[:80], link, source_name))
                    except Exception:
                        pass
                
                self.news_updated.emit(news_items)
            except Exception as e:
                self.status_changed.emit(f"NEWS: {str(e)[:50]}")
            
            for _ in range(300):
                if not self.running:
                    return
                threading.Event().wait(1)


class CalendarWorker(QThread):
    events_ready = pyqtSignal(str)
    weather_ready = pyqtSignal(str)
    status_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        credentials_path = os.path.expanduser("~/google_credentials.json")
        token_path = os.path.expanduser("~/.google_calendar_token.json")
        try:
            credentials = None
            if os.path.exists(token_path):
                credentials = Credentials.from_authorized_user_file(
                    token_path, GOOGLE_CALENDAR_SCOPES
                )
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            if not credentials or not credentials.valid:
                if not os.path.exists(credentials_path):
                    self.status_changed.emit("CALENDAR: CREDENTIALS NOT FOUND")
                    self.update_weather()
                    return
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path, GOOGLE_CALENDAR_SCOPES
                )
                credentials = run_google_oauth(flow)
            with open(token_path, "w", encoding="utf-8") as token_file:
                token_file.write(credentials.to_json())

            service = build("calendar", "v3", credentials=credentials)
            while self.running:
                self.update_events(service)
                self.update_weather()
                for _ in range(300):
                    if not self.running:
                        return
                    threading.Event().wait(1)
        except Exception as error:
            self.status_changed.emit(f"CALENDAR: {error}")

    def update_events(self, service):
        now = datetime.now(timezone.utc).isoformat()
        result = service.events().list(
            calendarId="primary",
            timeMin=now,
            maxResults=3,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        if not events:
            self.events_ready.emit("NO UPCOMING EVENTS")
            return
        lines = []
        for event in events:
            start = event.get("start", {})
            event_time = start.get("dateTime", start.get("date", ""))
            title = event.get("summary", "UNTITLED EVENT")
            lines.append(f"{event_time[:16].replace('T', ' ')}  {title}")
        self.events_ready.emit("\n".join(lines))

    def update_weather(self):
        try:
            url = "https://wttr.in/Odesa?format=j1"
            with urllib.request.urlopen(url, timeout=10) as response:
                weather = json.loads(response.read().decode("utf-8"))
            current = weather["current_condition"][0]
            temperature = current["temp_C"]
            feels_like = current["FeelsLikeC"]
            description = current["weatherDesc"][0]["value"]
            self.weather_ready.emit(
                f"ODESA  {temperature} C\nFEELS  {feels_like} C\n{description}"
            )
        except Exception:
            self.weather_ready.emit("ODESA WEATHER: OFFLINE")


class TelegramWorker(QThread):
    messages_ready = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    code_requested = pyqtSignal()
    password_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.running = True
        self.code_event = threading.Event()
        self.password_event = threading.Event()
        self.auth_code = ""
        self.auth_password = ""

    def stop(self):
        self.running = False
        self.code_event.set()
        self.password_event.set()

    def set_code(self, code):
        self.auth_code = code
        self.code_event.set()

    def set_password(self, password):
        self.auth_password = password
        self.password_event.set()

    def run(self):
        asyncio.run(self.update_messages())

    async def update_messages(self):
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        phone = os.getenv("TELEGRAM_PHONE")
        if not api_id or not api_hash or not phone:
            self.status_changed.emit("TELEGRAM: SET API VARIABLES")
            return

        session_path = os.path.expanduser("~/.cyber_panel_telegram")
        client = TelegramClient(session_path, int(api_id), api_hash)

        try:
            self.status_changed.emit("TELEGRAM: CONNECTING...")
            await client.start(
                phone=phone,
                code_callback=self.get_code,
                password=self.get_password,
            )

            while self.running:
                messages = []
                async for dialog in client.iter_dialogs(limit=100):
                    async for message in client.iter_messages(dialog.entity, limit=5):
                        if message.message and not getattr(message, "out", False):
                            messages.append((message.date, dialog.name, message.message))

                messages.sort(key=lambda item: item[0], reverse=True)
                if messages:
                    output = "\n\n".join(
                        f"{name}: {text[:180]}" for _, name, text in messages[:5]
                    )
                    self.messages_ready.emit(output)
                else:
                    self.messages_ready.emit("NO INCOMING MESSAGES")

                await asyncio.sleep(5)
        except Exception as error:
            self.status_changed.emit(f"TELEGRAM: {error}")
        finally:
            await client.disconnect()

    def get_code(self):
        self.auth_code = ""
        self.code_event.clear()
        self.status_changed.emit("TELEGRAM: ENTER CODE")
        self.code_requested.emit()
        self.code_event.wait()
        return self.auth_code

    def get_password(self):
        self.auth_password = ""
        self.password_event.clear()
        self.status_changed.emit("TELEGRAM: ENTER 2FA PASSWORD")
        self.password_requested.emit()
        self.password_event.wait()
        return self.auth_password


class RadialIndicator(QWidget):
    """Radial financial indicator for BTC/USDT or USD/UAH"""
    def __init__(self, label, value, change=0, parent=None):
        super().__init__(parent)
        self.label = label
        self.value = value
        self.change = change
        self.setMinimumSize(180, 180)

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        center = self.rect().center()
        radius = 80
        
        # Outer circle
        painter.setPen(QPen(QColor("#173f42"), 2))
        painter.drawEllipse(center, radius, radius)
        
        # Inner arc (based on change %)
        change_normalized = min(max((self.change + 100) / 200 * 100, 0), 100)
        painter.setPen(QPen(QColor("#73f6de" if self.change >= 0 else "#ff4444"), 4))
        painter.drawArc(
            int(center.x() - radius), int(center.y() - radius),
            radius * 2, radius * 2, 0, int(change_normalized * 3.6 * 16)
        )
        
        # Label
        painter.setPen(QColor("#9effef"))
        painter.setFont(QFont("Cascadia Mono", 10, QFont.Weight.Bold))
        painter.drawText(self.rect().adjusted(0, 40, 0, 0), Qt.AlignmentFlag.AlignHCenter, self.label)
        
        # Value
        painter.setFont(QFont("Cascadia Mono", 14, QFont.Weight.Bold))
        painter.drawText(self.rect().adjusted(0, 75, 0, 0), Qt.AlignmentFlag.AlignHCenter, f"{self.value:g}")
        
        # Change percentage
        change_color = QColor("#73f6de") if self.change >= 0 else QColor("#ff4444")
        painter.setPen(change_color)
        painter.setFont(QFont("Cascadia Mono", 9))
        painter.drawText(
            self.rect().adjusted(0, 120, 0, 0),
            Qt.AlignmentFlag.AlignHCenter,
            f"{self.change:+.2f}%"
        )


class NewsCarousel(QWidget):
    """News ticker with scrolling text"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.news_items = []
        self.current_index = 0
        self.scroll_pos = 0
        self.setFixedHeight(40)
        self.setStyleSheet("background-color: #0d1719; border: 1px solid #286e6b; border-radius: 6px;")
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_scroll)
        self.timer.start(50)

    def set_news(self, items):
        self.news_items = items
        self.current_index = 0
        self.scroll_pos = self.width()

    def update_scroll(self):
        self.scroll_pos -= 2
        if self.scroll_pos < -1000:
            self.scroll_pos = self.width()
        self.update()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        if not self.news_items:
            painter.setPen(QColor("#73f6de"))
            painter.setFont(QFont("Cascadia Mono", 11))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter, "NO NEWS AVAILABLE")
            return
        
        # Create ticker text
        ticker_text = " | ".join([f"[{item[2]}] {item[0]}" for item in self.news_items])
        ticker_text += " | "
        
        painter.setPen(QColor("#73f6de"))
        painter.setFont(QFont("Cascadia Mono", 10))
        painter.drawText(int(self.scroll_pos), 0, 2000, self.height(), 
                        Qt.AlignmentFlag.AlignVCenter, ticker_text)


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


class HudGauge(QWidget):
    def __init__(self, title, value, suffix="%", parent=None):
        super().__init__(parent)
        self.title = title
        self.value = value
        self.suffix = suffix
        self.setMinimumSize(112, 112)

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        radius = min(self.width(), self.height()) / 2 - 10

        painter.setPen(QPen(QColor("#173f42"), 2))
        painter.drawEllipse(center, int(radius), int(radius))
        painter.setPen(QPen(QColor("#286e6b"), 5))
        painter.drawArc(
            int(center.x() - radius), int(center.y() - radius),
            int(radius * 2), int(radius * 2), 35 * 16, 285 * 16
        )
        painter.setPen(QPen(QColor("#73f6de"), 5))
        painter.drawArc(
            int(center.x() - radius), int(center.y() - radius),
            int(radius * 2), int(radius * 2), 35 * 16, int(-self.value * 2.85 * 16)
        )
        painter.setPen(QColor("#9effef"))
        painter.setFont(QFont("Cascadia Mono", 9, QFont.Weight.Bold))
        painter.drawText(self.rect().adjusted(8, 20, -8, -48), Qt.AlignmentFlag.AlignCenter, self.title)
        painter.setFont(QFont("Cascadia Mono", 16, QFont.Weight.Bold))
        painter.drawText(self.rect().adjusted(8, 40, -8, -20), Qt.AlignmentFlag.AlignCenter, f"{self.value:g}{self.suffix}")


class EmbeddedTerminal(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "color: #b7fff2; background-color: #101012; "
            "border: 1px solid #00aa88; padding: 7px; font-size: 13px;"
        )
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setPlainText("WSL TERMINAL\n")
        self.process = QProcess(self)
        self.process.setWorkingDirectory(os.path.expanduser("~"))
        self.process.setProgram("bash")
        self.process.setArguments(["--noprofile", "--norc", "-i"])
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PS1", ">>> ")
        environment.insert("PROMPT_COMMAND", "")
        self.process.setProcessEnvironment(environment)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_output)
        self.process.start()
        self.setFocus()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            command = self.document().lastBlock().text().strip()
            if command.startswith(">>> "):
                command = command[4:]
            self.insertPlainText("\n")
            if command:
                self.process.write((command + "\n").encode())
            self.ensureCursorVisible()
            return

        super().keyPressEvent(event)
        self.moveCursor(self.textCursor().MoveOperation.End)

    def mousePressEvent(self, event):
        self.moveCursor(self.textCursor().MoveOperation.End)
        super().mousePressEvent(event)
        self.moveCursor(self.textCursor().MoveOperation.End)

    def read_output(self):
        output = self.process.readAllStandardOutput().data().decode(errors="replace")
        error = self.process.readAllStandardError().data().decode(errors="replace")
        text = output + error
        if text:
            self.moveCursor(self.textCursor().MoveOperation.End)
            self.insertPlainText(text)
            self.ensureCursorVisible()

    def stop(self):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(1000):
                self.process.kill()


class ExtendedHUD(QWidget):
    """Extended HUD for second monitor with task management, financial data, and news"""

    sheet_save_result = pyqtSignal(object, bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Extended HUD")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnBottomHint)
        
        self.setStyleSheet("""
            QWidget {
                color: #d6fff7;
                background-color: #0b1113;
                font-family: 'Cascadia Mono', 'DejaVu Sans Mono', monospace;
            }
            QLabel#sectionTitle {
                color: #73f6de;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            }
            QPushButton {
                color: #bffef1;
                background-color: #142326;
                border: 1px solid #286e6b;
                border-radius: 6px;
                padding: 6px 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #73f6de;
                color: #071112;
            }
            QListWidget {
                background-color: #0d1719;
                border: 1px solid #286e6b;
                color: #b8eee4;
            }
            QLineEdit {
                background-color: #101f21;
                border: 1px solid #286e6b;
                color: #b8eee4;
                padding: 6px;
                border-radius: 4px;
            }
        """)
        
        # Setup for second monitor
        screens = QApplication.screens()
        if len(screens) > 1:
            screen_geometry = screens[1].geometry()
            self.setGeometry(screen_geometry)
        else:
            # Fallback: centered window on the primary screen
            primary_geometry = QApplication.primaryScreen().geometry()
            self.setGeometry(
                max(0, primary_geometry.center().x() - 700),
                max(0, primary_geometry.center().y() - 450),
                1400, 900,
            )
        
        # Main layout
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(16)
        
        # LEFT: Task Management
        left_panel = self.create_task_panel()
        main_layout.addWidget(left_panel, 1)
        
        # CENTER: News Hub
        center_panel = self.create_news_panel()
        main_layout.addWidget(center_panel, 2)
        
        # RIGHT: Financial Terminal & Media
        right_panel = self.create_financial_panel()
        main_layout.addWidget(right_panel, 1)
        
        self.setLayout(main_layout)

        self.sheet_save_result.connect(self.on_sheet_save_result)

        # Workers are started by the main application; ExtendedHUD will receive updates via connected signals.
        self.status_label = QLabel("EXTENDED HUD: IDLE")
        self.status_label.setStyleSheet("color: #b8eee4;")
        main_layout.addWidget(self.status_label, 0)

    def create_task_panel(self):
        panel = QWidget()
        panel.setStyleSheet("background-color: #0e191b; border: 1px solid #1f4f4e; border-radius: 12px; padding: 12px;")
        layout = QVBoxLayout(panel)
        
        title = QLabel("SPRINT ИМ")
        title.setObjectName("sectionTitle")
        title.setStyleSheet("color: #73f6de; font-size: 12px;")
        layout.addWidget(title)
        
        self.ext_sprint_list = QListWidget()
        self.ext_sprint_list.setMaximumHeight(200)
        layout.addWidget(self.ext_sprint_list)
        
        divider = QLabel("──────────────")
        divider.setStyleSheet("color: #286e6b;")
        layout.addWidget(divider)
        
        title2 = QLabel("ЗАДАЧИ ДЛЯ ЮРЫ")
        title2.setObjectName("sectionTitle")
        layout.addWidget(title2)
        
        self.ext_backlog_list = QListWidget()
        self.ext_backlog_list.setMaximumHeight(200)
        layout.addWidget(self.ext_backlog_list)
        
        layout.addSpacing(10)
        
        add_task_btn = QPushButton("+ ДОБАВИТЬ В БЭКЛОГ")
        add_task_btn.clicked.connect(self.show_ext_add_task_form)
        layout.addWidget(add_task_btn)
        
        self.ext_task_form = QWidget()
        task_form_layout = QVBoxLayout(self.ext_task_form)
        task_form_layout.setContentsMargins(0, 0, 0, 0)
        
        self.ext_task_name_input = QLineEdit()
        self.ext_task_name_input.setPlaceholderText("Название...")
        self.ext_task_name_input.setMaximumHeight(28)
        task_form_layout.addWidget(self.ext_task_name_input)
        
        self.ext_task_desc_input = QLineEdit()
        self.ext_task_desc_input.setPlaceholderText("Описание...")
        self.ext_task_desc_input.setMaximumHeight(28)
        task_form_layout.addWidget(self.ext_task_desc_input)
        
        form_buttons = QHBoxLayout()
        save_btn = QPushButton("СОХР")
        save_btn.clicked.connect(self.save_ext_task)
        cancel_btn = QPushButton("ОТМЕН")
        cancel_btn.clicked.connect(self.hide_ext_add_task_form)
        form_buttons.addWidget(save_btn)
        form_buttons.addWidget(cancel_btn)
        task_form_layout.addLayout(form_buttons)
        
        self.ext_task_form.hide()
        layout.addWidget(self.ext_task_form)
        
        layout.addStretch()
        return panel

    def create_financial_panel(self):
        panel = QWidget()
        panel.setStyleSheet("background-color: #0e191b; border: 1px solid #1f4f4e; border-radius: 12px; padding: 12px;")
        layout = QVBoxLayout(panel)
        
        title = QLabel("FINANCIAL TERMINAL")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        
        indicators_layout = QHBoxLayout()
        self.btc_indicator = RadialIndicator("BTC/USDT", 0, 0)
        self.uah_indicator = RadialIndicator("USD/UAH", 0, 0)
        indicators_layout.addWidget(self.btc_indicator)
        indicators_layout.addWidget(self.uah_indicator)
        layout.addLayout(indicators_layout)
        
        divider = QLabel("──────────────────────")
        divider.setStyleSheet("color: #286e6b;")
        layout.addWidget(divider)
        
        media_title = QLabel("MEDIA CAROUSEL")
        media_title.setObjectName("sectionTitle")
        layout.addWidget(media_title)
        
        self.ext_video_list = QListWidget()
        self.ext_video_list.setMaximumHeight(150)
        self.ext_video_list.itemDoubleClicked.connect(self.play_video)
        layout.addWidget(self.ext_video_list)

        layout.addStretch()
        return panel

    def create_news_panel(self):
        panel = QWidget()
        panel.setStyleSheet("background-color: #0e191b; border: 1px solid #1f4f4e; border-radius: 12px; padding: 12px;")
        layout = QVBoxLayout(panel)
        
        title = QLabel("NEWS HUB")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        
        self.ext_news_carousel = NewsCarousel()
        layout.addWidget(self.ext_news_carousel)
        
        layout.addSpacing(10)
        
        news_scroll = QScrollArea()
        news_scroll.setWidgetResizable(True)
        news_scroll.setStyleSheet("QScrollArea { background-color: #0d1719; border: 1px solid #286e6b; }")
        
        self.ext_news_list = QListWidget()
        self.ext_news_list.itemClicked.connect(self.open_news)
        news_scroll.setWidget(self.ext_news_list)
        layout.addWidget(news_scroll, 1)
        
        layout.addStretch()
        return panel

    def update_financial_data(self, data):
        self.btc_indicator.value = data.get("btc_price", 0)
        self.btc_indicator.change = data.get("btc_change", 0)
        self.btc_indicator.update()
        
        self.uah_indicator.value = data.get("uah_rate", 0)
        self.uah_indicator.update()

    def update_tasks(self, sprint_tasks, backlog_tasks):
        # Extended HUD lists
        try:
            self.ext_sprint_list.clear()
            for task in sprint_tasks[:10]:
                if len(task) >= 2:
                    item_text = f"{task[0]} ({task[1]})" if len(task) > 1 else task[0]
                    item = QListWidgetItem(item_text)
                    self.ext_sprint_list.addItem(item)
        except AttributeError:
            pass
        
        try:
            self.ext_backlog_list.clear()
            for task in backlog_tasks[:10]:
                if len(task) >= 1:
                    item_text = task[0]
                    item = QListWidgetItem(item_text)
                    self.ext_backlog_list.addItem(item)
        except AttributeError:
            pass

    def update_videos(self, videos):
        # Extended HUD video list
        try:
            self.ext_video_list.clear()
            if not videos:
                self.ext_video_list.addItem("NO VIDEOS — check YOUTUBE_API_KEY or network")
                return
            for title, video_id, thumbnail in videos:
                item = QListWidgetItem(title[:60])
                item.setData(Qt.ItemDataRole.UserRole, video_id)
                self.ext_video_list.addItem(item)
        except AttributeError:
            pass

    def play_video(self, item):
        video_id = item.data(Qt.ItemDataRole.UserRole)
        url = f"https://www.youtube.com/watch?v={video_id}"
        webbrowser.open(url)

    def update_news(self, news_items):
        try:
            self.ext_news_carousel.set_news(news_items)
            self.ext_news_list.clear()
            if not news_items:
                self.ext_news_list.addItem("NO NEWS AVAILABLE")
                return
            for title, link, source in news_items:
                item = QListWidgetItem(f"[{source}] {title}")
                item.setData(Qt.ItemDataRole.UserRole, link)
                self.ext_news_list.addItem(item)
        except AttributeError:
            pass

    def open_news(self, item):
        link = item.data(Qt.ItemDataRole.UserRole)
        webbrowser.open(link)

    def show_ext_add_task_form(self):
        try:
            self.ext_task_form.show()
            self.ext_task_name_input.setFocus()
        except AttributeError:
            pass

    def hide_ext_add_task_form(self):
        try:
            self.ext_task_form.hide()
            self.ext_task_name_input.clear()
            self.ext_task_desc_input.clear()
        except AttributeError:
            pass

    def save_ext_task(self):
        try:
            name = self.ext_task_name_input.text()
            desc = self.ext_task_desc_input.text()
            if name:
                item = QListWidgetItem(f"{name} - {desc}")
                self.ext_backlog_list.addItem(item)
                self.hide_ext_add_task_form()
                save_task_to_backlog_sheet(name, desc, self, item)
        except AttributeError:
            pass

    def on_sheet_save_result(self, item, ok, message):
        if ok:
            item.setText(f"{item.text()} ✓")
        else:
            item.setText(f"⚠ NOT SAVED ({message}): {item.text()}")

    def closeEvent(self, event):
        # Stop workers if they exist (ExtendedHUD may be used without creating internal workers)
        if hasattr(self, 'financial_worker'):
            try:
                self.financial_worker.stop()
                self.financial_worker.wait(1000)
            except Exception:
                pass
        if hasattr(self, 'tasks_worker'):
            try:
                self.tasks_worker.stop()
                self.tasks_worker.wait(1000)
            except Exception:
                pass
        if hasattr(self, 'youtube_worker'):
            try:
                self.youtube_worker.stop()
                self.youtube_worker.wait(1000)
            except Exception:
                pass
        if hasattr(self, 'news_worker'):
            try:
                self.news_worker.stop()
                self.news_worker.wait(1000)
            except Exception:
                pass
        event.accept()


class CyberPanel(QWidget):
    sheet_save_result = pyqtSignal(object, bool, str)

    def __init__(self):
        super().__init__()

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnBottomHint)
        available_geometry = QApplication.primaryScreen().availableGeometry()
        self.setGeometry(available_geometry)
        
        self.setStyleSheet(
            """
            QWidget {
                color: #d6fff7;
                background-color: #0b1113;
                font-family: 'Cascadia Mono', 'DejaVu Sans Mono', monospace;
            }
            QLabel#sectionTitle {
                color: #73f6de;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
                padding: 4px 0;
            }
            QLabel#panelTitle {
                color: #d8fff8;
                font-size: 18px;
                font-weight: bold;
                padding: 2px 0 8px;
            }
            QPushButton {
                color: #bffef1;
                background-color: #142326;
                border: 1px solid #286e6b;
                border-radius: 6px;
                padding: 7px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: #071112;
                background-color: #73f6de;
                border-color: #b8fff4;
            }
            QPushButton:pressed { background-color: #42b9ac; }
            QPlainTextEdit {
                color: #b8eee4;
                background-color: #0d1719;
                border: 1px solid #286e6b;
                border-radius: 8px;
                padding: 10px;
                selection-background-color: #286e6b;
            }
            """
        )

        layout = QHBoxLayout()
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(14)

        # Левая панель метрик
        metrics_panel = QWidget()
        metrics_panel.setFixedWidth(360)
        metrics_panel.setObjectName("metricsPanel")
        metrics_panel.setStyleSheet(
            "#metricsPanel { background-color: #0e191b; border: 1px solid #1f4f4e; border-radius: 12px; padding: 8px; }"
        )
        metrics_layout = QVBoxLayout(metrics_panel)
        metrics_layout.setContentsMargins(14, 12, 14, 12)
        metrics_layout.setSpacing(10)

        telemetry_title = QLabel("NODE TELEMETRY")
        telemetry_title.setObjectName("sectionTitle")
        metrics_layout.addWidget(telemetry_title)

        gauges_layout = QHBoxLayout()
        gauges_layout.setSpacing(4)
        self.cpu_gauge = HudGauge("CPU LOAD", 0)
        self.ram_gauge = HudGauge("RAM USAGE", 0)
        gauges_layout.addWidget(self.cpu_gauge)
        gauges_layout.addWidget(self.ram_gauge)
        metrics_layout.addLayout(gauges_layout)

        self.temp_label = QLabel("CALCULATING...")
        self.temp_label.setFixedWidth(340)
        self.temp_label.setStyleSheet(
            "color: #73f6de; background-color: #101f21; border: 1px solid #286e6b; "
            "border-radius: 8px; padding: 12px; font-size: 14px; font-weight: bold;"
        )
        self.temp_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        metrics_layout.addWidget(self.temp_label)

        self.telegram_label = ClickableLabel("TELEGRAM: STARTING...")
        self.telegram_label.setFixedSize(340, 260)
        self.telegram_label.setWordWrap(True)
        self.telegram_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.telegram_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.telegram_label.setStyleSheet(
            "color: #bffef1; background-color: #101f21; "
            "border: 1px solid #286e6b; border-radius: 8px; padding: 12px; font-size: 13px;"
        )
        self.telegram_label.clicked.connect(self.open_telegram)
        metrics_layout.addWidget(self.telegram_label)

        shortcuts_layout = QHBoxLayout()
        shortcuts_layout.setSpacing(8)

        computer_button = QPushButton()
        computer_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        computer_button.setToolTip("Открыть диск C:\\")
        computer_button.clicked.connect(self.open_c_drive)
        shortcuts_layout.addWidget(computer_button)

        chrome_button = QPushButton("🌐")
        chrome_button.setToolTip("Открыть Google Chrome")
        chrome_button.clicked.connect(self.open_chrome)
        shortcuts_layout.addWidget(chrome_button)

        rdp_button = QPushButton()
        logo_path = "/mnt/c/Users/Михаил/OneDrive/Pictures/kopiyka.png"
        logo = QPixmap(logo_path)
        if not logo.isNull():
            rdp_button.setIcon(QIcon(logo))
        else:
            rdp_button.setText("₴")
        rdp_button.setToolTip("Открыть 227.rdp")
        rdp_button.clicked.connect(self.open_rdp)
        shortcuts_layout.addWidget(rdp_button)

        for button in (computer_button, chrome_button, rdp_button):
            button.setFixedSize(106, 52)
            button.setIconSize(QSize(40, 40))
        metrics_layout.addLayout(shortcuts_layout)

        metrics_layout.addStretch()

        terminal_title = QLabel("EMBEDDED TERMINAL")
        terminal_title.setObjectName("sectionTitle")
        metrics_layout.addWidget(terminal_title)

        self.terminal = EmbeddedTerminal()
        self.terminal.setFixedSize(340, 300)
        metrics_layout.addWidget(self.terminal)

        terminal_buttons = QHBoxLayout()
        copy_button = QPushButton("COPY ALL")
        copy_button.clicked.connect(self.copy_terminal_all)
        terminal_buttons.addWidget(copy_button)

        paste_button = QPushButton("PASTE")
        paste_button.clicked.connect(self.paste_terminal)
        terminal_buttons.addWidget(paste_button)

        for button in (copy_button, paste_button):
            button.setFixedHeight(32)
        metrics_layout.addLayout(terminal_buttons)

        restart_button = QPushButton("RESTART")
        restart_button.setFixedWidth(340)
        restart_button.clicked.connect(self.restart_app)
        metrics_layout.addWidget(restart_button)

        layout.addWidget(metrics_panel)

        self.telegram_worker = TelegramWorker()
        self.telegram_worker.messages_ready.connect(self.telegram_label.setText)
        self.telegram_worker.status_changed.connect(self.telegram_label.setText)
        self.telegram_worker.code_requested.connect(self.ask_telegram_code)
        self.telegram_worker.password_requested.connect(self.ask_telegram_password)
        self.telegram_worker.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_hardware_metrics)
        self.timer.start(2000)

        # Create Extended HUD on the right side
        extended_hud_panel = self.create_extended_hud_panel()
        layout.addWidget(extended_hud_panel, 1)

        # Конфигурация персистентного профиля для сохранения сессии авторизации
        self.profile = QWebEngineProfile("CyberProfile")
        profile_path = os.path.expanduser("~/.cyber_panel_cache")
        self.profile.setPersistentStoragePath(profile_path)
        self.profile.setCachePath(os.path.join(profile_path, "cache"))
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)

        # Инициализация веб-движка с сохраненным профилем
        self.browser = QWebEngineView()
        self.browser.setStyleSheet("background-color: #101012; border: none;")
        self.page = QWebEnginePage(self.profile, self.browser)
        self.browser.setPage(self.page)
        self.page.setBackgroundColor(QColor("#101012"))

        self.page.loadFinished.connect(
            lambda ok: self.hide_superset_header(self.page)
            if ok and "superset" in self.browser.url().toString() else None
        )
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.ForceDarkMode, True)
        self.browser.setUrl(QUrl("https://gemini.google.com/"))
        layout.addWidget(self.browser, 1)

        right_panel = QWidget()
        right_panel.setFixedWidth(520)
        right_panel.setObjectName("rightPanel")
        right_panel.setStyleSheet(
            "#rightPanel { background-color: #0e191b; border: 1px solid #1f4f4e; border-radius: 12px; padding: 8px; }"
        )
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(14, 12, 14, 12)
        right_layout.setSpacing(10)

        self.clock_label = QLabel()
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clock_label.setStyleSheet(
            "color: #73f6de; background-color: #101f21; border: 1px solid #286e6b; "
            "border-radius: 8px; padding: 10px; font-size: 26px; font-weight: bold;"
        )
        right_layout.addWidget(self.clock_label)

        self.weather_label = QLabel("ODESA WEATHER: LOADING...")
        self.weather_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.weather_label.setStyleSheet(
            "color: #bffef1; background-color: #101f21; "
            "border: 1px solid #286e6b; border-radius: 8px; padding: 8px; font-size: 14px;"
        )
        right_layout.addWidget(self.weather_label)

        events_title = QLabel("UPCOMING GOOGLE CALENDAR EVENTS")
        events_title.setObjectName("sectionTitle")
        right_layout.addWidget(events_title)

        self.events_view = QPlainTextEdit()
        self.events_view.setReadOnly(True)
        self.events_view.setFixedHeight(112)
        self.events_view.setStyleSheet(
            "color: #b8eee4; background-color: #0d1719; "
            "border: 1px solid #286e6b; border-radius: 8px; padding: 8px; font-size: 12px;"
        )
        self.events_view.setPlainText("CALENDAR: CONNECTING...")
        right_layout.addWidget(self.events_view)

        superset_urls = (
            "https://sset.varit.xyz/superset/dashboard/kopiykaanaliticsm/",
            "https://sset.varit.xyz/superset/dashboard/44/?native_filters_key=3iFMfof6R_k",
        )
        self.open_superset_in_chrome(superset_urls[0])

        layout.addWidget(right_panel)
        self.setLayout(layout)

        self.calendar_worker = CalendarWorker()
        self.calendar_worker.events_ready.connect(self.events_view.setPlainText)
        self.calendar_worker.weather_ready.connect(self.weather_label.setText)
        self.calendar_worker.status_changed.connect(self.events_view.setPlainText)
        self.calendar_worker.start()

        self.sheet_save_result.connect(self.on_sheet_save_result)

    def update_hardware_metrics(self):
        try:
            self.clock_label.setText(datetime.now().strftime("%d.%m %H:%M"))
            # Чтение загрузки ЦП и оперативной памяти
            cpu_usage = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory()
            ram_used = ram.used / (1024 ** 3)
            ram_total = ram.total / (1024 ** 3)

            cpu_temp = "N/A"
            try:
                for entries in psutil.sensors_temperatures().values():
                    for entry in entries:
                        if entry.current:
                            cpu_temp = f"{entry.current:.0f} C"
                            break
                    if cpu_temp != "N/A":
                        break
            except (AttributeError, OSError):
                pass

            # Формирование Sci-Fi вывода
            telemetry = (
                f"NODE TELEMETRY\n"
                f"================\n"
                f"CPU LOAD : {cpu_usage:04.1f}%\n"
                f"CPU TEMP : {cpu_temp}\n"
                f"RAM ALLOC: {ram_used:.1f} / {ram_total:.1f} GB\n"
                f"RAM USAGE: {ram.percent:04.1f}%\n"
                f"================\n"
                f"STATE: OPTIMAL"
            )
            self.temp_label.setText(telemetry)
            self.cpu_gauge.value = cpu_usage
            self.cpu_gauge.update()
            self.ram_gauge.value = ram.percent
            self.ram_gauge.update()
        except Exception:
            self.temp_label.setText("TELEMETRY: OFFLINE")

    def restart_app(self):
        os.execl(sys.executable, sys.executable, *sys.argv)

    def copy_terminal_all(self):
        QApplication.clipboard().setText(self.terminal.toPlainText())

    def paste_terminal(self):
        self.terminal.insertPlainText(QApplication.clipboard().text())
        self.terminal.moveCursor(self.terminal.textCursor().MoveOperation.End)
        self.terminal.setFocus()

    def open_telegram(self):
        executable = os.getenv(
            "TELEGRAM_EXE_PATH",
            "C:\\Program Files\\WindowsApps\\"
            "TelegramMessengerLLP.TelegramDesktop_7.0.9.0_x64__t4vj0pshhgkwm\\"
            "Telegram.exe",
        )
        QProcess.startDetached("explorer.exe", [executable])

    def open_c_drive(self):
        QProcess.startDetached("explorer.exe", ["C:\\"])

    def open_chrome(self):
        executable = os.getenv(
            "CHROME_EXE_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        )
        QProcess.startDetached("explorer.exe", [executable])

    def open_superset_in_chrome(self, url):
        executable = os.getenv(
            "CHROME_EXE_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        )
        QProcess.startDetached("explorer.exe", [executable, url])

    def hide_superset_header(self, page):
        page.runJavaScript(
            """
            (() => {
                const style = document.createElement('style');
                style.textContent = `
                    [data-test="navbar"],
                    .navbar,
                    .ant-layout-header,
                    [role="banner"] {
                        display: none !important;
                    }
                `;
                document.head.appendChild(style);
            })();
            """
        )

    def open_rdp(self):
        rdp_file = os.getenv("RDP_FILE_PATH", r"C:\Users\Михаил\OneDrive\Desktop\227.rdp")
        QProcess.startDetached("mstsc.exe", [rdp_file])

    def ask_telegram_code(self):
        dialog = QInputDialog()
        dialog.setWindowTitle("Telegram authorization")
        dialog.setLabelText("Введите код из Telegram:")
        dialog.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        dialog.resize(420, 130)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        code = dialog.textValue()
        self.telegram_worker.set_code(code if accepted else "")

    def ask_telegram_password(self):
        dialog = QInputDialog()
        dialog.setWindowTitle("Telegram 2FA")
        dialog.setLabelText("Введите пароль двухфакторной защиты:")
        dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
        dialog.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        dialog.resize(420, 130)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        password = dialog.textValue()
        self.telegram_worker.set_password(password if accepted else "")

    def create_extended_hud_panel(self):
        """Create the Extended HUD panel with tasks, financial data, news, and media"""
        panel = QWidget()
        panel.setObjectName("extendedHudPanel")
        panel.setStyleSheet(
            "#extendedHudPanel { background-color: #0e191b; border: 1px solid #1f4f4e; border-radius: 12px; padding: 8px; }"
        )
        
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        
        # Title
        hud_title = QLabel("EXTENDED HUD")
        hud_title.setObjectName("sectionTitle")
        hud_title.setStyleSheet("color: #73f6de; font-size: 12px; font-weight: bold;")
        layout.addWidget(hud_title)
        
        # Task Management Section
        task_title = QLabel("SPRINT ИМ")
        task_title.setObjectName("sectionTitle")
        layout.addWidget(task_title)
        
        self.sprint_list = QListWidget()
        self.sprint_list.setMaximumHeight(120)
        self.sprint_list.setStyleSheet(
            "background-color: #0d1719; border: 1px solid #286e6b; color: #b8eee4; border-radius: 6px;"
        )
        layout.addWidget(self.sprint_list)
        
        backlog_title = QLabel("ЗАДАЧИ ДЛЯ ЮРЫ")
        backlog_title.setObjectName("sectionTitle")
        layout.addWidget(backlog_title)
        
        self.backlog_list = QListWidget()
        self.backlog_list.setMaximumHeight(100)
        self.backlog_list.setStyleSheet(
            "background-color: #0d1719; border: 1px solid #286e6b; color: #b8eee4; border-radius: 6px;"
        )
        layout.addWidget(self.backlog_list)
        
        add_task_btn = QPushButton("+ ДОБАВИТЬ")
        add_task_btn.setFixedHeight(28)
        add_task_btn.clicked.connect(self.show_add_task_form)
        layout.addWidget(add_task_btn)
        
        # Task Form
        self.task_form = QWidget()
        task_form_layout = QVBoxLayout(self.task_form)
        task_form_layout.setContentsMargins(0, 0, 0, 0)
        task_form_layout.setSpacing(4)
        
        self.task_name_input = QLineEdit()
        self.task_name_input.setPlaceholderText("Название...")
        self.task_name_input.setMaximumHeight(24)
        self.task_name_input.setStyleSheet(
            "background-color: #101f21; border: 1px solid #286e6b; color: #b8eee4; border-radius: 4px; padding: 4px;"
        )
        task_form_layout.addWidget(self.task_name_input)
        
        self.task_desc_input = QLineEdit()
        self.task_desc_input.setPlaceholderText("Описание...")
        self.task_desc_input.setMaximumHeight(24)
        self.task_desc_input.setStyleSheet(
            "background-color: #101f21; border: 1px solid #286e6b; color: #b8eee4; border-radius: 4px; padding: 4px;"
        )
        task_form_layout.addWidget(self.task_desc_input)
        
        form_buttons = QHBoxLayout()
        form_buttons.setSpacing(4)
        save_btn = QPushButton("СОХР")
        save_btn.setFixedHeight(24)
        save_btn.clicked.connect(self.save_task)
        cancel_btn = QPushButton("ОТМЕН")
        cancel_btn.setFixedHeight(24)
        cancel_btn.clicked.connect(self.hide_add_task_form)
        form_buttons.addWidget(save_btn)
        form_buttons.addWidget(cancel_btn)
        task_form_layout.addLayout(form_buttons)
        
        self.task_form.hide()
        layout.addWidget(self.task_form)
        
        # Financial Section
        divider1 = QLabel("─" * 35)
        divider1.setStyleSheet("color: #286e6b; font-size: 10px;")
        layout.addWidget(divider1)
        
        fin_title = QLabel("FINANCIAL")
        fin_title.setObjectName("sectionTitle")
        layout.addWidget(fin_title)
        
        fin_layout = QHBoxLayout()
        fin_layout.setSpacing(4)
        self.btc_indicator = RadialIndicator("BTC/USDT", 0, 0)
        self.btc_indicator.setFixedSize(140, 140)
        self.uah_indicator = RadialIndicator("USD/UAH", 0, 0)
        self.uah_indicator.setFixedSize(140, 140)
        fin_layout.addWidget(self.btc_indicator)
        fin_layout.addWidget(self.uah_indicator)
        fin_layout.addStretch()
        layout.addLayout(fin_layout)
        
        # Media Section
        divider2 = QLabel("─" * 35)
        divider2.setStyleSheet("color: #286e6b; font-size: 10px;")
        layout.addWidget(divider2)
        
        media_title = QLabel("MEDIA")
        media_title.setObjectName("sectionTitle")
        layout.addWidget(media_title)
        
        self.video_list = QListWidget()
        self.video_list.setMaximumHeight(80)
        self.video_list.setStyleSheet(
            "background-color: #0d1719; border: 1px solid #286e6b; color: #b8eee4; border-radius: 6px;"
        )
        self.video_list.itemDoubleClicked.connect(self.play_video)
        layout.addWidget(self.video_list)
        
        # News Section
        divider3 = QLabel("─" * 35)
        divider3.setStyleSheet("color: #286e6b; font-size: 10px;")
        layout.addWidget(divider3)
        
        news_title = QLabel("NEWS HUB")
        news_title.setObjectName("sectionTitle")
        layout.addWidget(news_title)
        
        self.news_carousel = NewsCarousel()
        self.news_carousel.setFixedHeight(32)
        layout.addWidget(self.news_carousel)
        
        self.news_list = QListWidget()
        self.news_list.setMaximumHeight(100)
        self.news_list.setStyleSheet(
            "background-color: #0d1719; border: 1px solid #286e6b; color: #b8eee4; border-radius: 6px;"
        )
        self.news_list.itemClicked.connect(self.open_news)
        layout.addWidget(self.news_list)
        
        layout.addStretch()
        
        # Start workers for data updates
        self.financial_worker = FinancialDataWorker()
        self.financial_worker.data_updated.connect(self.update_financial_data)
        # forward financial worker status to events view if available
        if hasattr(self, 'events_view'):
            try:
                self.financial_worker.status_changed.connect(lambda s: self.events_view.setPlainText(s))
            except Exception:
                pass
        self.financial_worker.start()
        
        self.tasks_worker = TasksWorker()
        self.tasks_worker.tasks_updated.connect(self.update_tasks)
        if hasattr(self, 'events_view'):
            try:
                self.tasks_worker.status_changed.connect(lambda s: self.events_view.setPlainText(s))
            except Exception:
                pass
        self.tasks_worker.start()
        
        self.youtube_worker = YouTubeWorker()
        self.youtube_worker.videos_updated.connect(self.update_videos)
        try:
            self.youtube_worker.status_changed.connect(lambda s: (self.video_list.clear(), self.video_list.addItem(f"ERROR: {s}")))
        except Exception:
            pass
        self.youtube_worker.start()
        
        self.news_worker = NewsWorker()
        self.news_worker.news_updated.connect(self.update_news)
        try:
            self.news_worker.status_changed.connect(lambda s: (self.news_list.clear(), self.news_list.addItem(f"ERROR: {s}")))
        except Exception:
            pass
        self.news_worker.start()
        
        return panel

    def update_financial_data(self, data):
        self.btc_indicator.value = data.get("btc_price", 0)
        self.btc_indicator.change = data.get("btc_change", 0)
        self.btc_indicator.update()
        
        self.uah_indicator.value = data.get("uah_rate", 0)
        self.uah_indicator.update()

    def update_tasks(self, sprint_tasks, backlog_tasks):
        self.sprint_list.clear()
        for task in sprint_tasks[:5]:
            if len(task) >= 2:
                item_text = f"{task[0]} ({task[1]})" if len(task) > 1 else task[0]
                item = QListWidgetItem(item_text[:50])
                self.sprint_list.addItem(item)
        
        self.backlog_list.clear()
        for task in backlog_tasks[:5]:
            if len(task) >= 1:
                item_text = task[0]
                item = QListWidgetItem(item_text[:50])
                self.backlog_list.addItem(item)

    def update_videos(self, videos):
        self.video_list.clear()
        if not videos:
            self.video_list.addItem("NO VIDEOS — check YOUTUBE_API_KEY or network")
            return
        for title, video_id, thumbnail in videos:
            item = QListWidgetItem(title[:40])
            item.setData(Qt.ItemDataRole.UserRole, video_id)
            self.video_list.addItem(item)

    def play_video(self, item):
        video_id = item.data(Qt.ItemDataRole.UserRole)
        url = f"https://www.youtube.com/watch?v={video_id}"
        webbrowser.open(url)

    def update_news(self, news_items):
        self.news_carousel.set_news(news_items)
        
        self.news_list.clear()
        if not news_items:
            self.news_list.addItem("NO NEWS AVAILABLE")
            return
        for title, link, source in news_items:
            item = QListWidgetItem(f"[{source}] {title[:35]}")
            item.setData(Qt.ItemDataRole.UserRole, link)
            self.news_list.addItem(item)

    def open_news(self, item):
        link = item.data(Qt.ItemDataRole.UserRole)
        webbrowser.open(link)

    def show_add_task_form(self):
        self.task_form.show()
        self.task_name_input.setFocus()

    def hide_add_task_form(self):
        self.task_form.hide()
        self.task_name_input.clear()
        self.task_desc_input.clear()

    def save_task(self):
        name = self.task_name_input.text()
        desc = self.task_desc_input.text()
        if name:
            item = QListWidgetItem(f"{name[:30]} - {desc[:15]}")
            self.backlog_list.addItem(item)
            self.hide_add_task_form()
            save_task_to_backlog_sheet(name, desc, self, item)

    def on_sheet_save_result(self, item, ok, message):
        if ok:
            item.setText(f"{item.text()} ✓")
        else:
            item.setText(f"⚠ NOT SAVED ({message}): {item.text()}")

    def closeEvent(self, event):
        self.telegram_worker.stop()
        self.telegram_worker.wait(3000)
        self.calendar_worker.stop()
        self.calendar_worker.wait(3000)
        self.terminal.stop()

        if hasattr(self, 'extended_hud_window'):
            self.extended_hud_window.close()
        
        # Stop Extended HUD workers
        if hasattr(self, 'financial_worker'):
            self.financial_worker.stop()
            self.financial_worker.wait(1000)
        if hasattr(self, 'tasks_worker'):
            self.tasks_worker.stop()
            self.tasks_worker.wait(1000)
        if hasattr(self, 'youtube_worker'):
            self.youtube_worker.stop()
            self.youtube_worker.wait(1000)
        if hasattr(self, 'news_worker'):
            self.news_worker.stop()
            self.news_worker.wait(1000)
        
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)

    # One-time setup: show the form while any required key is missing
    if missing_env_keys():
        setup_dialog = FirstRunSetupDialog()
        setup_dialog.exec()
        load_dotenv(ENV_PATH, override=True)

    startup = QWidget()
    startup.setWindowFlags(Qt.WindowType.SplashScreen | Qt.WindowType.WindowStaysOnTopHint)
    startup.setFixedSize(520, 220)
    startup.setStyleSheet(
        "background-color: #101012; color: #00ffcc; "
        "border: 2px solid #00aa88;"
    )
    startup_layout = QVBoxLayout(startup)
    startup_label = QLabel("J>A>R>V>I>S\n\nДобрый день, сэр!")
    startup_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    startup_label.setStyleSheet(
        "color: #00ffcc; font-size: 26px; font-weight: bold; "
        "border: none;"
    )
    startup_layout.addWidget(startup_label)
    startup.show()
    app.processEvents()

    panel = CyberPanel()
    panel.show()

    # Second-monitor HUD, fed by the same workers as the embedded panel
    extended_hud = ExtendedHUD()
    panel.financial_worker.data_updated.connect(extended_hud.update_financial_data)
    panel.tasks_worker.tasks_updated.connect(extended_hud.update_tasks)
    panel.youtube_worker.videos_updated.connect(extended_hud.update_videos)
    panel.news_worker.news_updated.connect(extended_hud.update_news)
    panel.extended_hud_window = extended_hud
    extended_hud.show()

    startup.raise_()
    QTimer.singleShot(1800, startup.close)
    sys.exit(app.exec())
