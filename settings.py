"""The plugin's settings, set by the admin in the NxtSound server (gear button on the plugin's card).

The server hands them over at start (environment variable PLUGIN_SETTINGS) and after every change (PUT /settings).
"""
import json
import os
import re

SCHEMA = [
    {"key": "url", "type": "text", "label": {"en": "Nextcloud / ownCloud address", "de": "Adresse von Nextcloud / ownCloud"}, "max_length": 300, "default": "",
     "placeholder": "https://cloud.example.com",
     "help": {"en": "The address you open in the browser, without a path. A full WebDAV address (containing /remote.php/) is used as it is.",
              "de": "Die Adresse, die du im Browser öffnest, ohne Pfad. Eine vollständige WebDAV-Adresse (mit /remote.php/) wird unverändert benutzt."}},
    {"key": "username", "type": "text", "label": {"en": "User name", "de": "Benutzername"}, "max_length": 100, "default": ""},
    {"key": "password", "type": "text", "secret": True, "label": {"en": "App password", "de": "App-Passwort"}, "max_length": 200, "default": "",
     "help": {"en": "Create one in Nextcloud/ownCloud under Settings → Security → Devices & sessions; do not use your main password.",
              "de": "Erzeuge eines in Nextcloud/ownCloud unter Einstellungen → Sicherheit → Geräte & Sitzungen; nimm nicht dein Hauptpasswort."}},
    {"key": "folder", "type": "text", "label": {"en": "Music folder", "de": "Musikordner"}, "max_length": 300, "default": "/Music", "placeholder": "/Music",
     "help": {"en": "The folder in your cloud that holds the music (all subfolders are included). Best organized as Artist/Album/01 - Title.mp3.",
              "de": "Der Ordner in deiner Cloud mit der Musik (alle Unterordner zählen mit). Am besten als Künstler/Album/01 - Titel.mp3 geordnet."}},
    {"key": "rescan_hours", "type": "int", "label": {"en": "Look for new music every", "de": "Nach neuer Musik suchen alle"}, "min": 0, "max": 168, "default": 24,
     "unit": {"en": "hours", "de": "Stunden"},
     "help": {"en": "How often the music folder is read again. 0 = only when you start it with the button \"Scan the music folder now\".",
              "de": "Wie oft der Musikordner neu eingelesen wird. 0 = nur, wenn du ihn mit dem Knopf „Musikordner jetzt einlesen“ startest."}},
]
FIELDS = {e["key"]: e for e in SCHEMA}
values = {e["key"]: e["default"] for e in SCHEMA}


def _valid(entry: dict, v) -> bool:
    if entry["type"] == "int":
        return isinstance(v, int) and not isinstance(v, bool) and entry["min"] <= v <= entry["max"]
    return isinstance(v, str) and len(v) <= entry["max_length"]


def normalize_url(raw: str) -> str:
    """What the admin typed as the address: "cloud.example.com" means https://cloud.example.com (like the app does); trailing slashes go."""
    raw = raw.strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    return raw.rstrip("/")


def apply(raw) -> set[str]:
    """Takes over valid values; returns the keys that changed."""
    changed = set()
    for key, v in (raw if isinstance(raw, dict) else {}).items():
        if key in FIELDS and _valid(FIELDS[key], v):
            v = normalize_url(v) if key == "url" else v.strip() if isinstance(v, str) else v
            if values[key] != v:
                values[key] = v
                changed.add(key)
    return changed


try:
    apply(json.loads(os.getenv("PLUGIN_SETTINGS", "{}")))
except ValueError:
    pass


def problem_texts() -> tuple[str, str]:
    """Why the connection cannot be tried yet, in English and German (empty texts if everything needed is filled in)."""
    if not values["url"] or not values["username"] or not values["password"]:
        return "Address, user name and app password are needed.", "Adresse, Benutzername und App-Passwort werden gebraucht."
    if not re.match(r"^https?://[^\s/@]+(/\S*)?$", values["url"]):
        return ("The address is not valid: use https://cloud.example.com (no user name in it).",
                "Die Adresse ist ungültig: nimm https://cloud.example.com (ohne Benutzernamen darin).")
    return "", ""


def config_problem() -> str:
    """The English text of problem_texts() ('' if everything needed is filled in)."""
    return problem_texts()[0]
