import os
import json
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

AMO_TOKEN = os.environ["AMO_TOKEN"]
AMO_DOMAIN = os.environ["AMO_DOMAIN"]
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
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


def get_lead(lead_id):
    return amo_get(f"/api/v4/leads/{lead_id}", {"with": "contacts"})


def get_user_name(user_id):
    if not user_id:
        return "Не назначен"
    data = amo_get(f"/api/v4/users/{user_id}")
    return data.get("name", f"Менеджер #{user_id}")


def check_lead_processed(lead_id, lead_created_at):
    """
    Returns (is_processed: bool, reason: str | None)

    Processed means ANY of:
    - Lead stage changed from NEW_LEAD_STATUS_ID
    - Outgoing call note exists (call_out)
    - Outgoing Wazzup/S3 message note exists (extended_service_message with direction=out)
    - Any note written by a real manager (common note, created_by != 0)
    """
    lead = get_lead(lead_id)
    if not lead:
        return True, "лид не найден"

    if lead.get("status_id") != NEW_LEAD_STATUS_ID:
        return True, "этап изменён"

    notes_data = amo_get(f"/api/v4/leads/{lead_id}/notes", {"limit": 50})
    notes = notes_data.get("_embedded", {}).get("notes", [])

    for note in notes:
        note_ts = note.get("created_at", 0)
        if note_ts < lead_created_at:
            continue

        note_type = note.get("note_type", "")
        created_by = note.get("created_by", 0)
        params = note.get("params", {}) or {}

        # Outgoing call (S3 or any telephony)
        if note_type == "call_out":
            return True, "исходящий звонок"

        # Wazzup / S3 service messages — check direction
        if note_type in ("extended_service_message", "service_message"):
            direction = params.get("direction") or params.get("type") or ""
            # direction "out" or int 2 means outgoing
            if str(direction).lower() in ("out", "2", "outgoing"):
                return True, "исходящее сообщение менеджера"

        # Note manually written by a real manager (not system/bot)
        if note_type == "common" and created_by and created_by != 0:
            return True, "примечание от менеджера"

    return False, None


def send_telegram_alert(lead_id, lead_name, responsible_name, created_at):
    time_str = datetime.fromtimestamp(created_at).strftime("%d.%m %H:%M")
    url = f"https://{AMO_DOMAIN}/leads/detail/{lead_id}"
    text = (
        f"🔴 <b>Заявка не отработана за 5 минут!</b>\n\n"
        f"📋 <a href='{url}'>{lead_name or 'Без имени'} #{lead_id}</a>\n"
        f"👤 Ответственный: <b>{responsible_name}</b>\n"
        f"🕐 Создана: {time_str}\n\n"
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
    # Try JSON formats (AMO salesbot sends JSON)
    if "application/json" in (content_type or ""):
        try:
            data = json.loads(body_bytes)
            lead_id = data.get("lead_id") or data.get("id")
            if lead_id:
                return int(lead_id)
            # Format: {"leads": {"add": [{"id": 123}]}}
            leads_block = data.get("leads", {})
            if isinstance(leads_block, dict):
                for key in ("add", "status", "update"):
                    items = leads_block.get(key, [])
                    if items:
                        return int(items[0].get("id", 0)) or None
        except Exception:
            pass

    # Try form-encoded (legacy AMO webhooks)
    try:
        params = parse_qs(body_bytes.decode("utf-8"))
        for key in params:
            if "[id]" in key and ("add" in key or "status" in key or "update" in key):
                val = params[key][0]
                if val.isdigit():
                    return int(val)
    except Exception:
        pass

    return None


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        content_type = self.headers.get("Content-Type", "")

        # Respond immediately (AMO expects fast 200)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

        lead_id = extract_lead_id(body, content_type)
        if not lead_id:
            return

        try:
            lead = get_lead(lead_id)
            if not lead:
                return

            # Only act on leads still in target stage
            if lead.get("status_id") != NEW_LEAD_STATUS_ID:
                return

            created_at = lead.get("created_at", 0)
            lead_name = lead.get("name", "")
            responsible_id = lead.get("responsible_user_id")
            responsible_name = get_user_name(responsible_id)

            processed, _ = check_lead_processed(lead_id, created_at)
            if not processed:
                send_telegram_alert(lead_id, lead_name, responsible_name, created_at)

        except Exception:
            pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"AMO Lead Monitor v1.0 — OK")

    def log_message(self, format, *args):
        pass
