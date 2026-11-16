from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.security import SecretVault


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADMIN_HTML = (PROJECT_ROOT / "admin.html").read_text(encoding="utf-8")


class AdminUiAcceptanceTests(unittest.TestCase):
    def test_stylesheet_source_and_runtime_artifact_exist(self) -> None:
        self.assertTrue(
            (PROJECT_ROOT / "scss" / "admin.scss").is_file(),
            "SPEC requires the Sass entry point scss/admin.scss.",
        )
        self.assertTrue(
            (PROJECT_ROOT / "app" / "static" / "admin.css").is_file(),
            "SPEC requires the compiled runtime asset app/static/admin.css.",
        )

    def test_admin_document_links_the_public_stylesheet(self) -> None:
        self.assertIsNotNone(
            re.search(r'<link\b[^>]*href=["\']/static/admin\.css["\']', ADMIN_HTML),
            "admin.html must link /static/admin.css.",
        )

    def test_admin_document_has_no_embedded_css_block(self) -> None:
        self.assertIsNone(
            re.search(r"<style\b", ADMIN_HTML),
            "CSS must be externalized from admin.html.",
        )

    def test_admin_document_has_no_static_or_generated_style_attributes(self) -> None:
        self.assertIsNone(
            re.search(r"\bstyle\s*=", ADMIN_HTML),
            "Inline style attributes, including generated markup, are disallowed.",
        )

    def test_prompt_drafts_are_saved_and_restored_separately_from_token(self) -> None:
        prompt_draft_storage = re.compile(
            r"sessionStorage\.(?:getItem|setItem|removeItem)"
            r"\([^)]*(?:prompt|draft)",
            re.IGNORECASE,
        )
        self.assertIsNotNone(
            prompt_draft_storage.search(ADMIN_HTML),
            "Prompt drafts need per-prompt sessionStorage persistence.",
        )

    def test_async_regions_expose_busy_state(self) -> None:
        self.assertTrue(
            "aria-busy" in ADMIN_HTML,
            "Asynchronous regions must expose aria-busy.",
        )

    def test_async_status_is_announced_to_assistive_technology(self) -> None:
        self.assertIsNotNone(
            re.search(r'aria-live=["\']polite["\']', ADMIN_HTML),
            "Async status messages need aria-live=polite.",
        )

    def test_trace_drawer_has_dialog_semantics(self) -> None:
        drawer = re.search(
            r'<aside\b[^>]*id=["\']trace-drawer["\'][^>]*>',
            ADMIN_HTML,
        )
        self.assertIsNotNone(drawer, "Expected the existing trace drawer.")
        self.assertIsNotNone(
            re.search(r'role=["\']dialog["\']', drawer.group(0)),
            "The overlay trace drawer needs dialog semantics.",
        )

    def test_trace_drawer_closes_on_escape(self) -> None:
        self.assertIsNotNone(
            re.search(
                r"(?is)(?:keydown|key\s*===?\s*['\"]Escape['\"]|"
                r"['\"]Escape['\"]\s*===?\s*\w+)",
                ADMIN_HTML,
            ),
            "The trace drawer must close on Escape.",
        )

    def test_trace_drawer_moves_keyboard_focus_on_open(self) -> None:
        show_trace = re.search(
            r"function showTrace\(.*?\n\s*\}",
            ADMIN_HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(show_trace)
        self.assertIsNotNone(
            re.search(r"\.focus\(", show_trace.group(0)),
            "Opening the trace drawer must move focus into it.",
        )

    def test_trace_drawer_restores_keyboard_focus_on_close(self) -> None:
        close_trace = re.search(
            r"function closeTrace\(.*?\n\s*\}",
            ADMIN_HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(close_trace)
        self.assertIsNotNone(
            re.search(r"\.focus\(", close_trace.group(0)),
            "Closing the trace drawer must restore focus to its trigger.",
        )


class AdminServingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        temporary_path = Path(self.temporary_directory.name)
        self.css_path = temporary_path / "admin.css"
        self.css_path.write_text("body { color: #123; }\n", encoding="utf-8")
        key = Fernet.generate_key().decode("ascii")
        database_path = temporary_path / "test.sqlite3"
        self.settings = Settings(
            telegram_token="test-token",
            admin_token="test-admin-token",
            data_encryption_key=key,
            database_path=database_path,
            default_delivery_time="09:00",
            timezone="UTC",
            daily_budget_usd=1,
            max_tokens=1000,
        )
        self.database = Database(database_path)
        self.database.initialize()
        with patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(database_path),
                "TELEGRAM_TOKEN": "test-token",
                "ADMIN_TOKEN": "test-admin-token",
                "DATA_ENCRYPTION_KEY": key,
            },
        ):
            from app.admin import create_app

        self.application = create_app(
            settings=self.settings,
            database=self.database,
            vault=SecretVault(key),
            ai=object(),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_fastapi_starts_and_serves_page_and_css_without_node(self) -> None:
        from app.admin import ADMIN_CSS

        with patch("app.admin.ADMIN_CSS", self.css_path):
            with TestClient(self.application) as client:
                health = client.get("/health")
                page = client.get("/admin")
                stylesheet = client.get("/static/admin.css")

        self.assertEqual(health.status_code, 200, health.text)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(stylesheet.status_code, 200, stylesheet.text)
        self.assertIn("text/css", stylesheet.headers["content-type"])
        self.assertEqual(stylesheet.text, "body { color: #123; }\n")
        self.assertTrue(ADMIN_CSS.is_absolute())

    def test_shipped_css_endpoint_returns_the_project_asset(self) -> None:
        with TestClient(self.application) as client:
            response = client.get("/static/admin.css")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/css", response.headers["content-type"])
