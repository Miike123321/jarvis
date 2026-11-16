import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# AppTest may run the app in an isolated script context.
os.environ["JARVIS_DISABLE_SUPERSET_MCP"] = "true"

import httpx
from fastapi import WebSocketDisconnect
from google import genai
from google.genai import types
from streamlit.runtime.secrets import Secrets
from streamlit.testing.v1 import AppTest

import config_manager as config_manager_module
from config_manager import (
    ConfigManager,
    DatabaseConfig,
    ModelConfig,
    PLACEHOLDER_API_KEY,
    SupersetMCPConfig,
    normalize_omniroute_base_url,
)
from jarvis_backend import (
    GeminiChat,
    MAX_GEMINI_TOOL_ROUNDS,
    MAX_QUERY_RESULT_CHARS,
    OmnirouteChat,
    create_chat,
    execute_ddl_statement,
    execute_read_only_query,
    is_approved_ddl,
    is_read_only_select,
    test_model_connection,
    config_manager as app_config_manager,
    websocket_endpoint,
)
from superset_mcp import SupersetMCPError


class GeminiChatTests(unittest.TestCase):
    def test_client_remains_open_across_messages_and_closes_with_chat(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "candidates": [{
                        "content": {"role": "model", "parts": [{"text": "OK"}]},
                        "finishReason": "STOP",
                    }]
                },
            )

        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                client_args={"transport": httpx.MockTransport(respond)}
            ),
        )
        http_client = client._api_client._httpx_client
        model = ModelConfig(
            provider="gemini", model_name="gemini-2.5-flash", api_key="test-key"
        )
        with patch("jarvis_backend.config_manager.get_model_config", return_value=model):
            with patch(
                "jarvis_backend.config_manager.get_superset_mcp_config",
                return_value=SupersetMCPConfig(),
            ), patch("jarvis_backend.genai.Client", return_value=client) as factory:
                chat = create_chat()
                factory.return_value = None
                del client

            self.assertIn("не более 5 пунктов", chat.system_instruction)
            self.assertEqual(chat.send_message("first").text, "OK")
            self.assertEqual(chat.send_message("second").text, "OK")
            self.assertEqual(len(requests), 2)
            for request in requests:
                body = json.loads(request.content)
                self.assertNotIn("maxOutputTokens", body.get("generationConfig", {}))
            self.assertEqual(len(json.loads(requests[1].content)["contents"]), 3)
            self.assertFalse(http_client.is_closed)
            chat.close()
            self.assertTrue(http_client.is_closed)

    def test_tool_limit_produces_partial_summary_instead_of_empty_answer(self):
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) <= MAX_GEMINI_TOOL_ROUNDS:
                part = {
                    "functionCall": {
                        "name": "execute_read_only_query",
                        "args": {"sql_query": "SELECT 1"},
                    }
                }
            else:
                part = {"text": "Получены частичные результаты."}
            return httpx.Response(200, json={
                "candidates": [{
                    "content": {"role": "model", "parts": [part]},
                    "finishReason": "STOP",
                }]
            })

        def query(sql_query: str) -> str:
            return '{"data": [{"value": 1}]}'

        query.__name__ = "execute_read_only_query"
        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                client_args={"transport": httpx.MockTransport(respond)}
            ),
        )
        with patch("jarvis_backend.genai.Client", return_value=client), \
             patch("jarvis_backend.execute_read_only_query", query):
            chat = GeminiChat("gemini-2.5-flash", "test-key")
            response = chat.send_message("Проверь даты в таблицах")
            follow_up = chat.send_message("Продолжи")

        self.assertIn("частич", response.text.lower())
        self.assertEqual(follow_up.text, "Получены частичные результаты.")
        self.assertEqual(len(requests), MAX_GEMINI_TOOL_ROUNDS + 2)
        for request in requests:
            self.assertNotIn("maxOutputTokens", request.get("generationConfig", {}))
        self.assertFalse(requests[-2].get("tools"))
        self.assertTrue(requests[-1].get("tools"))
        self.assertIn("data", requests[-2]["contents"][-1]["parts"][0]["text"])
        self.assertEqual(
            requests[-1]["contents"][-2]["parts"][0]["text"],
            "Получены частичные результаты.",
        )
        chat.close()

    def test_limit_summary_uses_plain_text_even_when_model_repeats_tool_calls(self):
        requests = []
        queries = []

        def query(sql_query: str) -> str:
            queries.append(sql_query)
            return '{"data": [{"value": 42}]}'

        query.__name__ = "execute_read_only_query"

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            has_tool_history = any(
                "functionCall" in part
                for content in body["contents"]
                for part in content.get("parts", [])
            )
            if body.get("tools") or has_tool_history:
                part = {
                    "functionCall": {
                        "name": "execute_read_only_query",
                        "args": {"sql_query": "SELECT 42"},
                    }
                }
            else:
                part = {"text": "Результат: 42."}
            return httpx.Response(200, json={
                "candidates": [{
                    "content": {"role": "model", "parts": [part]},
                    "finishReason": "STOP",
                }]
            })

        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                client_args={"transport": httpx.MockTransport(respond)}
            ),
        )
        with patch("jarvis_backend.genai.Client", return_value=client), \
             patch("jarvis_backend.execute_read_only_query", query):
            chat = GeminiChat("gemini-2.5-flash", "test-key", max_remote_calls=2)
            response = chat.send_message("Покажи результат")

        self.assertIn("Результат: 42.", response.text)
        self.assertIn("могут быть неполными", response.text)
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(requests), 3)
        self.assertFalse(requests[-1].get("tools"))
        summary_text = requests[-1]["contents"][-1]["parts"][0]["text"]
        self.assertIn("42", summary_text)
        self.assertIn("SELECT 42", summary_text)
        self.assertNotIn("functionCall", json.dumps(requests[-1]["contents"]))
        chat.close()

    def test_blocked_response_reports_reason(self):
        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(client_args={
                "transport": httpx.MockTransport(lambda request: httpx.Response(
                    200, json={"promptFeedback": {"blockReason": "SAFETY"}}
                ))
            }),
        )
        with patch("jarvis_backend.genai.Client", return_value=client):
            chat = GeminiChat("gemini-2.5-flash", "test-key")
            with self.assertRaisesRegex(RuntimeError, "SAFETY"):
                chat.send_message("Запрос")
        self.assertEqual(chat.chat.get_history(), [])
        chat.close()

    def test_max_tokens_error_reports_provider_limit(self):
        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                client_args={
                    "transport": httpx.MockTransport(
                        lambda request: httpx.Response(
                            200,
                            json={
                                "candidates": [
                                    {
                                        "content": {
                                            "role": "model",
                                            "parts": [],
                                        },
                                        "finishReason": "MAX_TOKENS",
                                    }
                                ]
                            },
                        )
                    )
                }
            ),
        )
        with patch("jarvis_backend.genai.Client", return_value=client):
            chat = GeminiChat("gemini-2.5-flash", "test-key")
            with self.assertRaisesRegex(
                RuntimeError,
                "исчерпан лимит генерации Gemini",
            ):
                chat.send_message("Ответь на вопрос.")
        chat.close()

    def test_partial_text_with_max_tokens_is_marked_incomplete(self):
        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                client_args={
                    "transport": httpx.MockTransport(
                        lambda request: httpx.Response(
                            200,
                            json={
                                "candidates": [
                                    {
                                        "content": {
                                            "role": "model",
                                            "parts": [{"text": "Частичный ответ."}],
                                        },
                                        "finishReason": "MAX_TOKENS",
                                    }
                                ]
                            },
                        )
                    )
                }
            ),
        )
        with patch("jarvis_backend.genai.Client", return_value=client):
            chat = GeminiChat("gemini-2.5-flash", "test-key")
            response = chat.send_message("Ответь на вопрос.")

        self.assertIn("Частичный ответ.", response.text)
        self.assertIn("может быть неполным", response.text)
        chat.close()

    def test_connection_test_reports_gemini_block_reason(self):
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(client_args={
                "transport": httpx.MockTransport(respond)
            }),
        )
        model = ModelConfig(
            provider="gemini", model_name="gemini-2.5-flash", api_key="test-key"
        )
        with patch("jarvis_backend.genai.Client", return_value=client), \
             patch("jarvis_backend.config_manager.get_model_config", return_value=model):
            result = test_model_connection()
        self.assertFalse(result["success"])
        self.assertIn("SAFETY", result["error"])
        self.assertEqual(len(requests), 1)
        self.assertNotIn("maxOutputTokens", requests[0].get("generationConfig", {}))
        client.close()


class WebSocketChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_closes_gemini_client(self):
        chat = GeminiChat.__new__(GeminiChat)
        chat.client = MagicMock()
        websocket = AsyncMock()
        websocket.receive_text.side_effect = WebSocketDisconnect()

        with patch("jarvis_backend.create_chat", return_value=chat):
            await websocket_endpoint(websocket)

        chat.client.close.assert_called_once_with()


class ReadOnlyQueryTests(unittest.TestCase):
    def test_accepts_select_and_read_only_cte(self):
        self.assertTrue(is_read_only_select("SELECT name FROM customers;"))
        self.assertTrue(
            is_read_only_select(
                "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent"
            )
        )
        self.assertTrue(is_read_only_select("SELECT 'DELETE; DROP' AS [into]"))

    def test_rejects_batch_writes_and_select_into(self):
        rejected_queries = (
            "SELECT 1; DELETE FROM customers",
            "SELECT 1; /* comment */ DROP TABLE customers",
            "SELECT id INTO copy_of_customers FROM customers",
            "WITH rows AS (SELECT id FROM customers) DELETE FROM customers",
        )
        for query in rejected_queries:
            with self.subTest(query=query):
                self.assertFalse(is_read_only_select(query))

    def test_normalizes_openai_compatible_base_urls(self):
        self.assertEqual(
            normalize_omniroute_base_url("http://localhost:20128"),
            "http://localhost:20128/v1",
        )
        self.assertEqual(
            normalize_omniroute_base_url("http://localhost:20128/v1/"),
            "http://localhost:20128/v1",
        )

    @patch("jarvis_backend.pymssql.connect")
    def test_rejected_query_does_not_connect(self, connect):
        result = execute_read_only_query("SELECT 1; DROP TABLE customers")

        self.assertIn("отклонён", result)
        connect.assert_not_called()

    @patch("jarvis_backend.database_config", return_value={})
    @patch("jarvis_backend.pymssql.connect")
    def test_query_result_is_capped_before_being_sent_to_the_model(
        self, connect, _database_config
    ):
        cursor = MagicMock()
        cursor.description = [("value",)]
        cursor.fetchmany.return_value = [("x" * (MAX_QUERY_RESULT_CHARS + 1),)]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection

        result = json.loads(execute_read_only_query("SELECT value FROM data"))

        self.assertTrue(result["truncated"])
        self.assertEqual(result["data"], [])
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False)), MAX_QUERY_RESULT_CHARS
        )
        _database_config.assert_called_once_with()


class OmnirouteToolTests(unittest.TestCase):
    @patch("jarvis_backend.execute_read_only_query", return_value='{"data":[{"year":2024}]}')
    @patch("jarvis_backend.requests.post")
    def test_runs_read_only_tool_and_returns_final_answer(self, post, execute_query):
        tool_response = MagicMock()
        tool_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "execute_read_only_query",
                                    "arguments": json.dumps(
                                        {"sql_query": "SELECT YEAR(date_col) AS year FROM dbo.orders"}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        }
        final_response = MagicMock()
        final_response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "В 2024 году есть записи."}}]
        }
        post.side_effect = [tool_response, final_response]
        chat = OmnirouteChat("test-model", "test-key", "http://localhost:20128/v1")

        answer = chat.send_message("Посчитай записи по годам")

        self.assertEqual(answer.text, "В 2024 году есть записи.")
        execute_query.assert_called_once_with(
            "SELECT YEAR(date_col) AS year FROM dbo.orders"
        )
        self.assertEqual(
            post.call_args_list[0].kwargs["json"]["tools"][0]["function"]["name"],
            "execute_read_only_query",
        )
        second_messages = post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual(second_messages[-1]["role"], "tool")
        self.assertEqual(second_messages[-1]["tool_call_id"], "call-1")


class DdlExecutionTests(unittest.TestCase):
    def test_allows_only_single_supported_create_statements(self):
        self.assertTrue(is_approved_ddl("CREATE TABLE reports (id int)"))
        self.assertTrue(is_approved_ddl("CREATE UNIQUE INDEX ix ON reports (id)"))
        self.assertTrue(
            is_approved_ddl(
                "CREATE TABLE reports (note varchar(20) DEFAULT 'a;b')"
            )
        )
        self.assertFalse(is_approved_ddl("CREATE OR REPLACE VIEW reports AS SELECT 1"))
        self.assertFalse(is_approved_ddl("CREATE TABLE reports (id int); DROP TABLE reports"))

    @patch("jarvis_backend.database_config", return_value={"server": "test"})
    @patch("jarvis_backend.pymssql.connect")
    def test_uses_pymssql_for_confirmed_ddl(self, connect, _database_config):
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection

        result = execute_ddl_statement("CREATE TABLE reports (id int)")

        self.assertEqual(result, "SQL выполнен успешно.")
        connect.assert_called_once_with(server="test")
        _database_config.assert_called_once()
        cursor.execute.assert_called_once_with("CREATE TABLE reports (id int)")
        connection.commit.assert_called_once()
        connection.close.assert_called_once()


class ConfigurationTests(unittest.TestCase):
    def test_streamlit_root_secrets_supply_managed_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            path.write_text(
                'JARVIS_MANAGED_CONFIG = "true"\n'
                'DB_HOST = "db.example"\n'
                'DB_NAME = "analytics"\n'
                'DB_USER = "reader"\n'
                'DB_PASSWORD = "test-password"\n'
                'GEMINI_API_KEY = "test-key"\n'
                'GEMINI_MODEL = "gemini-test"\n'
                'SUPERSET_MCP_ENABLED = "true"\n'
                'SUPERSET_MCP_URL = "https://superset.example/mcp"\n'
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "streamlit.runtime.secrets.config.get_option", return_value=[str(path)]
            ), patch.object(
                config_manager_module, "CONFIG_FILE", Path(directory) / "missing.json"
            ), patch.object(
                Secrets, "_maybe_install_file_watchers"
            ):
                secrets = Secrets()
                self.assertTrue(secrets.load_if_toml_exists())
                manager = ConfigManager()
                self.assertIsNotNone(manager.get_database_config())
                self.assertIsNotNone(manager.get_model_config())
                self.assertTrue(manager.get_superset_mcp_config().enabled)
                self.assertFalse(manager.is_ui_config_enabled())

    def test_managed_config_uses_shared_environment_not_saved_settings(self):
        environment = {
            "JARVIS_MANAGED_CONFIG": "true",
            "ALLOW_DB_CONFIG_UI": "true",
            "DB_HOST": "db.example",
            "DB_NAME": "analytics",
            "DB_USER": "reader",
            "DB_PASSWORD": "test-db-password",
            "GEMINI_API_KEY": "test-gemini-key",
            "GEMINI_MODEL": "gemini-test",
            "OMNIROUTE_API_KEY": "unused-omniroute-key",
            "SUPERSET_MCP_ENABLED": "true",
            "SUPERSET_MCP_URL": "https://superset.example/mcp",
            "SUPERSET_MCP_BEARER_TOKEN": "test-token",
        }
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.json"
            config_file.write_text(json.dumps({
                "database": {
                    "host": "old-db", "port": 1433, "name": "old",
                    "user": "old", "password": "old",
                },
                "model": {"provider": "gemini", "model_name": "old", "api_key": "old"},
                "superset_mcp": {
                    "enabled": False, "endpoint_url": "", "bearer_token": "",
                },
            }))
            with patch.object(config_manager_module, "CONFIG_FILE", config_file), \
                 patch.dict(os.environ, environment, clear=True):
                manager = ConfigManager()
                self.assertEqual(manager.get_database_config().host, "db.example")
                self.assertEqual(manager.get_model_config().provider, "gemini")
                self.assertEqual(manager.get_model_config().model_name, "gemini-test")
                self.assertTrue(manager.get_superset_mcp_config().enabled)
                self.assertEqual(manager.get_superset_mcp_config().bearer_token, "test-token")
                self.assertFalse(manager.is_ui_config_enabled())

    def test_database_config_uses_pymssql_connection_fields(self):
        connection_settings = DatabaseConfig(
            host="db.example",
            port=1433,
            name="analytics",
            user="reader",
            password="test-password",
        ).to_pymssql_dict()

        self.assertEqual(connection_settings["server"], "db.example")
        self.assertEqual(connection_settings["port"], "1433")
        self.assertEqual(connection_settings["database"], "analytics")

    def test_database_environment_defaults_to_sql_server_port(self):
        environment = {
            "DB_HOST": "db.example",
            "DB_NAME": "analytics",
            "DB_USER": "reader",
            "DB_PASSWORD": "test-password",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch("config_manager.CONFIG_FILE", Path(directory) / "missing.json"):
                with patch.dict(os.environ, environment, clear=True):
                    config = ConfigManager().get_database_config()

        self.assertIsNotNone(config)
        self.assertEqual(config.port, 1433)

    def test_environment_template_values_are_not_treated_as_configuration(self):
        environment = {
            "DB_HOST": "your_sql_server_host",
            "DB_NAME": "your_database_name",
            "DB_USER": "your_database_user",
            "DB_PASSWORD": "replace_with_a_new_password",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch("config_manager.CONFIG_FILE", Path(directory) / "missing.json"):
                with patch.dict(os.environ, environment, clear=True):
                    config = ConfigManager().get_database_config()

        self.assertIsNone(config)

    def test_saved_configuration_file_has_restricted_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / ".jarvis_config.json"
            with patch("config_manager.CONFIG_FILE", config_file):
                manager = ConfigManager()
                manager.set_database_config(
                    DatabaseConfig(
                        host="db.example",
                        port=1433,
                        name="analytics",
                        user="reader",
                        password="test-password",
                    )
                )

            self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)

    def test_placeholder_omniroute_key_does_not_override_gemini(self):
        environment = {
            "OMNIROUTE_API_KEY": PLACEHOLDER_API_KEY,
            "GEMINI_API_KEY": "configured-gemini-key",
            "GEMINI_MODEL": "gemini-2.5-flash",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch("config_manager.CONFIG_FILE", Path(directory) / "missing.json"):
                with patch.dict(os.environ, environment, clear=True):
                    model = ConfigManager().get_model_config()

        if model is None:
            self.fail("A valid Gemini configuration should be loaded")
        self.assertEqual(model.provider, "gemini")


class StreamlitGeminiKeyTests(unittest.TestCase):
    def test_password_form_saves_gemini_key(self):
        app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.json"
            with patch.object(config_manager_module, "CONFIG_FILE", config_file):
                with patch.dict(
                    os.environ,
                    {
                        "ALLOW_DB_CONFIG_UI": "true",
                        "JARVIS_DISABLE_SUPERSET_MCP": "true",
                    },
                    clear=True,
                ):
                    with patch.object(
                        app_config_manager, "get_database_config", return_value=None
                    ), \
                        patch.object(
                        app_config_manager, "get_model_config", return_value=None
                    ), \
                        patch.object(
                            app_config_manager,
                            "get_superset_mcp_config",
                            return_value=SupersetMCPConfig(),
                        ), \
                        patch.object(
                            app_config_manager, "set_model_config"
                        ) as save_model, patch(
                            "superset_mcp.superset_mcp_manager.get_toolset",
                            side_effect=SupersetMCPError(
                                "Superset test connection disabled"
                            ),
                        ):
                        app = AppTest.from_file(str(app_path)).run()
                        self.assertFalse(app.exception)

                        app.radio(key="model_provider_select").set_value("gemini").run()
                        api_key_input = app.text_input(key="gemini_api_key_input")
                        api_key_input.set_value("test-gemini-key")
                        save_button = next(
                            button
                            for button in app.button
                            if button.label == "Сохранить ключ Gemini"
                        )
                        save_button.click().run()

                        save_model.assert_called_once()
                        saved_config = save_model.call_args.args[0]
                        self.assertEqual(saved_config.provider, "gemini")
                        self.assertEqual(saved_config.api_key, "test-gemini-key")
                        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
