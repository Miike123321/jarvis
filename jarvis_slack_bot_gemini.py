"""Always-on Slack bot: reads the channel every 5 seconds and replies with
Google Gemini as the "JARVIS" agent persona. Run it alongside the HUD app:

    python jarvis_slack_bot_gemini.py

Reads from .env: SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, GEMINI_API_KEY.
Optional: SLACK_POLL_SECONDS (default 5), GEMINI_MODEL (default gemini-2.0-flash).
"""
import os
import time
import json
import urllib.request
import urllib.error

from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.env"))
load_dotenv(".env")

SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
CHANNEL = os.getenv("SLACK_CHANNEL_ID", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
POLL = int(os.getenv("SLACK_POLL_SECONDS", "5"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

SLACK_API = "https://slack.com/api"


def _req(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return {"ok": False, "error": f"http {e.code}: {e.read().decode()[:200]}"}
        except Exception:
            return {"ok": False, "error": f"http {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def slack_headers():
    return {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}


def slack_post(method, payload):
    body = json.dumps(payload).encode("utf-8")
    return _req(f"{SLACK_API}/{method}", headers=slack_headers(), data=body, method="POST")


def get_my_user_id():
    return slack_post("auth.test", {}).get("user_id")


def fetch_new_messages(last_ts):
    params = f"channel={CHANNEL}&limit=20"
    if last_ts:
        params += f"&oldest={last_ts}"
    data = _req(f"{SLACK_API}/conversations.history?{params}", headers=slack_headers())
    if not data.get("ok"):
        return last_ts, []
    messages = data.get("messages", [])
    if messages:
        last_ts = messages[0].get("ts", last_ts)
    return last_ts, list(reversed(messages))


def ask_gemini(user_text):
    payload = {
        "system_instruction": {
            "parts": [{"text": (
                "You are JARVIS, a concise Russian-speaking desktop HUD assistant. "
                "Reply briefly in Russian. If the message is a task, acknowledge it, "
                "give a rough time estimate, and a short status. Be helpful and direct."
            )}]
        },
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
    }
    body = json.dumps(payload).encode("utf-8")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent?key={GEMINI_KEY}"
    )
    data = _req(url, headers={"content-type": "application/json"}, data=body, method="POST")
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        err = data.get("error")
        if isinstance(err, dict):
            err = err.get("message", "unknown")
        return f"(gemini error: {err})"


def main():
    if not (SLACK_TOKEN and CHANNEL and GEMINI_KEY):
        print("Set SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, GEMINI_API_KEY in .env")
        return
    my_id = get_my_user_id()
    print(f"JARVIS Gemini bot online as {my_id}, channel {CHANNEL}, poll {POLL}s")
    # prime last_ts so we don't reply to old history
    last_ts, _ = fetch_new_messages(None)
    while True:
        try:
            last_ts, messages = fetch_new_messages(last_ts)
            for msg in messages:
                if msg.get("subtype"):
                    continue
                if msg.get("user") == my_id:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                print(f"< {text}")
                reply = ask_gemini(text)
                slack_post("chat.postMessage", {"channel": CHANNEL, "text": reply})
                print(f"> {reply}")
        except Exception as e:
            print("loop error:", e)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
