"""One getUpdates consumer per bot token, including shared multi-client bots."""
import hashlib
import time

from pulse_replay import write_state
from telegram_notify import bot_call, NotificationError


def run_router(configs, dispatch, path, stop_event=None):
    import json
    offsets = json.loads(path.read_text()) if path.exists() else {}
    while stop_event is None or not stop_event.is_set():
        groups = {}
        try:
            for target, config in configs():
                if config.get("token") and config.get("chat_id"):
                    groups.setdefault(config["token"], []).append((target, config))
            for token, clients in groups.items():
                key = hashlib.sha256(token.encode()).hexdigest()
                try:
                    updates = bot_call(token, "getUpdates", {"offset": offsets.get(key, 0), "timeout": 0,
                                                            "allowed_updates": ["callback_query"]})
                    if not isinstance(updates, list):
                        continue
                    for update in updates:
                        if not isinstance(update, dict):
                            continue
                        if type(update.get("update_id")) is not int:
                            continue
                        # Consume before execution; a crash never replays a trading callback.
                        offsets[key] = update["update_id"] + 1
                        write_state(path, offsets)
                        query = update.get("callback_query", {})
                        if not isinstance(query, dict):
                            continue
                        text = "Кнопка недоступна или относится к другому кабинету"
                        for target, config in clients:
                            if str(query.get("from", {}).get("id")) != config["chat_id"]:
                                continue
                            result = dispatch(target, query)
                            if result.get("matched"):
                                text = result["message"]
                                break
                        if query.get("id"):
                            try:
                                bot_call(token, "answerCallbackQuery", {"callback_query_id": query["id"],
                                                                       "text": text[:180], "show_alert": True})
                            except NotificationError:
                                pass
                except NotificationError:
                    continue
        except Exception:
            pass
        if stop_event is None:
            time.sleep(2)
        else:
            stop_event.wait(2)
