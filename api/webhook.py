import os
import json
import requests
from datetime import datetime
from flask import Flask, request

app = Flask(__name__)

AMO_TOKEN = os.environ.get("AMO_TOKEN", "")
AMO_DOMAIN = os.environ.get("AMO_DOMAIN", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
NEW_LEAD_STATUS_ID = int(os.environ.get("NEW_LEAD_STATUS_ID", "81146518"))

AMO_HEADERS = {
    "Authorization": f"Bearer {AMO_TOKEN}",
    "Content-Type": "application/json",
}
AMO_BASE = f"https://{AMO_DOMAIN}"


def amo_get(path, params=None):
    try:
        r = requests.get(
            f"{AMO_BASE}{path}", headers=AMO_HEADERS, params=params, timeout=8
        )
        return r.json() if r.ok else {}
    except Exception:
        return {}


def get_user_name(user_id):
    if not user_id:
        return "Не назначен"
    data = amo_get(f"/api/v4/users/{user_id}")
    return data.get("name", f"Менеджер #{user_id}")


def check_lead_processed(lead_id, lead_created_at):
    lead = amo_get(f"/api/v4/leads/{lead_id}")
    if not lead:
        return True

    if lead.get("status_id") != NEW_LEAD_STATUS_ID:
        return True

    notes_data = amo_get(f"/api/v4/leads/{lead_id}/notes", {"limit": 50})
    notes = notes_data.get("_embedded", {}).get("notes", [])

    for note in notes:
        if note.get("created_at", 0) < lead_created_at:
            continue

        note_type = note.get("note_type", "")
        created_by = note.get("created_by", 0)
        params = note.get("params", {}) or {}

        # Outgoing call (S3 or any telephony)
        if note_type == "call_out":
            return True

        # Wazzup / S3 outgoing messages
        if note_type in ("extended_service_message", "service_message"):
            direction = params.get("direction") or params.get("type") or ""
            if str(direction).lower() in ("out", "2", "outgoing"):
                return True

        # Note manually written by a real manager
        if note_type == "common" and created_by and created_by != 0:
            return True

    return False


def send_telegram_alert(lead_id, lead_name, responsible_name, created_at):
    time_str = datetime.fromtimestamp(created_at).strftime("%d.%m %H:%M")
    url = f"https://{AMO_DOMAIN}/leads/detail/{lead_id}"
    text = (
        f"\U0001f534 <b>Заявка не отработана за 5 минут!</b>\n\n"
        f"\U0001f4cb <a href='{url}'>{lead_name or 'Без имени'} #{lead_id}</a>\n"
        f"\U0001f464 Ответственный: <b>{responsible_name}</b>\n"
        f"\U0001f550 Создана: {time_str}\n\n"
        f"Нет исходящего сообщения, звонка или смены этапа!"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=5,
        )
    except Exception:
        pass


def extract_lead_id(body_bytes, content_type):
    if "application/json" in (content_type or ""):
        try:
            data = json.loads(body_bytes)
            lead_id = data.get("lead_id") or data.get("id")
            if lead_id:
                return int(lead_id)
            leads_block = data.get("leads", {})
            if isinstance(leads_block, dict):
                for key in ("add", "status", "update"):
                    items = leads_block.get(key, [])
                    if items:
                        return int(items[0].get("id", 0)) or None
        except Exception:
            pass

    # Form-encoded (legacy AMO webhooks)
    try:
        from urllib.parse import parse_qs
        params = parse_qs(body_bytes.decode("utf-8"))
        for key in params:
            if "[id]" in key and ("add" in key or "status" in key or "update" in key):
                val = params[key][0]
                if val.isdigit():
                    return int(val)
    except Exception:
        pass

    return None


@app.route("/api/webhook", methods=["GET"])
def health():
    return "AMO Lead Monitor v1.0 - OK", 200


@app.route("/api/webhook", methods=["POST"])
def webhook():
    body = request.get_data()
    content_type = request.content_type or ""

    lead_id = extract_lead_id(body, content_type)
    if not lead_id:
        return "No lead_id", 200

    try:
        lead = amo_get(f"/api/v4/leads/{lead_id}")
        if not lead:
            return "Lead not found", 200

        if lead.get("status_id") != NEW_LEAD_STATUS_ID:
            return "Lead already processed (stage changed)", 200

        created_at = lead.get("created_at", 0)
        lead_name = lead.get("name", "")
        responsible_id = lead.get("responsible_user_id")
        responsible_name = get_user_name(responsible_id)

        if not check_lead_processed(lead_id, created_at):
            send_telegram_alert(lead_id, lead_name, responsible_name, created_at)
            return "Alert sent", 200

        return "Lead is processed", 200

    except Exception as e:
        return f"Error: {e}", 200
