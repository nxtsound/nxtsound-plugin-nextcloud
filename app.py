"""NxtSound plugin: Nextcloud / ownCloud. Speaks the NxtSound plugin protocol v1 (see the README and the server's PLUGINS.md).

The music folder in the cloud is read once into a local index (catalog.py), so searching is instant; streaming passes the file through from the cloud
(WebDAV, with Range requests so seeking works).
"""
import asyncio
import hmac
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import catalog
import dav
import settings

TOKEN = os.getenv("PLUGIN_TOKEN", "")
ALLOW_NO_TOKEN = os.getenv("PLUGIN_ALLOW_NO_TOKEN", "") == "1"
ID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")
CONNECTION_KEYS = {"url", "username", "password", "folder"}  # a change of one of these means: a different music folder, start over

# plugin.json is the single source for who the plugin is (id, name, version, description).
SPEC = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))

ACTIONS = [
    {"id": "scan", "label": {"en": "Scan the music folder now", "de": "Musikordner jetzt einlesen"},
     "help": {"en": "Reads the music folder in the background. Only changed folders are read again, unless you ask for everything.",
              "de": "Liest den Musikordner im Hintergrund ein. Nur geänderte Ordner werden erneut gelesen, außer du verlangst alles."},
     "fields": [{"key": "full", "type": "bool", "label": {"en": "Read everything again", "de": "Alles neu einlesen"}, "default": False}]},
    {"id": "test", "label": {"en": "Test the connection", "de": "Verbindung testen"}, "fields": [],
     "help": {"en": "Checks address, login and music folder.", "de": "Prüft Adresse, Anmeldung und Musikordner."}},
]


def _say(en: str, de: str = "") -> dict:
    return {"en": en, "de": de or en}


# ---------- Looking for new music ----------

async def scan_loop() -> None:
    """Scans when nothing was scanned yet and, if the admin wants it, every `rescan_hours` hours. After a failure it waits ten minutes."""
    while True:
        try:
            if not settings.config_problem() and not catalog.STATE["running"] and time.time() - catalog.STATE["finished"] > 600:
                last, hours = await asyncio.to_thread(catalog.last_scan), settings.values["rescan_hours"]
                if last == 0 or (hours > 0 and time.time() - last > hours * 3600):
                    catalog.start_scan()
        except Exception:  # noqa: BLE001 (the loop must never die)
            pass
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(_app):
    if not TOKEN and not ALLOW_NO_TOKEN:
        raise RuntimeError("PLUGIN_TOKEN is missing. Without a token anyone on the network can fetch the music (for testing only: PLUGIN_ALLOW_NO_TOKEN=1).")
    task = asyncio.create_task(scan_loop()) if os.getenv("NO_SCAN_LOOP") != "1" else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="NxtSound-Plugin Nextcloud", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def auth(request: Request) -> None:
    if not TOKEN:
        return
    if not hmac.compare_digest(request.headers.get("authorization", "").encode(), f"Bearer {TOKEN}".encode()):
        raise HTTPException(401, "Token missing or wrong")


router = APIRouter(dependencies=[Depends(auth)])


@app.get("/health")
def health():
    return {"ok": True}


# ---------- Protocol ----------

@router.get("/manifest")
def manifest():
    return {"id": SPEC["id"], "name": SPEC["name"], "version": SPEC["version"], "api_version": 1, "description": SPEC["description"],
            "features": ["search", "stream", "suggest", "artist", "album"], "search_kinds": ["songs", "artists", "albums"],
            "settings": settings.SCHEMA, "actions": ACTIONS}


@router.get("/search")
def search(q: str = Query(min_length=1, max_length=200), kind: str = "songs", limit: int = Query(25, ge=1, le=50)):
    fn = {"songs": catalog.songs, "artists": catalog.artists, "albums": catalog.albums}.get(kind)
    if not fn:
        raise HTTPException(400, "unknown kind")
    return {"items": fn(q, limit)}


@router.get("/suggest")
def suggest(q: str = Query(min_length=1, max_length=100)):
    return {"suggestions": catalog.suggestions(q)}


@router.get("/artist/{artist_id}")
def artist(artist_id: str):
    page = catalog.artist_page(artist_id) if ID_RE.match(artist_id) else None
    if not page:
        raise HTTPException(404, "unknown ID")
    return page


@router.get("/album/{album_id}")
def album(album_id: str):
    page = catalog.album_page(album_id) if ID_RE.match(album_id) else None
    if not page:
        raise HTTPException(404, "unknown ID")
    return page


@router.get("/stream/{track_id}")
def stream(track_id: str, request: Request):
    """Passes the file through from the cloud; Range requests are forwarded, so seeking and the iPhone work."""
    found = catalog.path_of(track_id) if ID_RE.match(track_id) else None
    if not found:
        raise HTTPException(404, "unknown ID")
    path, media_type = found
    try:
        client, resp = dav.open_file(path, request.headers.get("range", ""))
    except dav.DavError as e:
        raise HTTPException(502, str(e))
    headers = {k: resp.headers[k] for k in ("content-length", "content-range") if k in resp.headers}
    headers["accept-ranges"] = "bytes"

    def body():
        try:
            yield from resp.iter_bytes(65536)
        finally:
            resp.close()
            client.close()

    return StreamingResponse(body(), status_code=resp.status_code, media_type=media_type, headers=headers)


# ---------- Settings and actions ----------

def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _info() -> list[dict]:
    s = catalog.STATE
    out = [{"label": _say("Tracks in the index", "Titel im Index"), "value": str(catalog.count())},
           {"label": _say("Last scan", "Letztes Einlesen"), "value": _when(catalog.last_scan()) or "-"}]
    if s["running"]:
        out.append({"label": _say("Scan", "Einlesen"), "value": f"{s['folders']} folders read so far"})
    en, de = settings.problem_texts()
    if en or s["error"]:
        out.append({"label": _say("Problem", "Problem"), "value": _say(en, de) if en else _say(s["error"], s["error_de"])})
    return out


@router.get("/settings")
def get_settings():
    return {"info": _info()}


@router.put("/settings")
async def put_settings(request: Request):
    """The admin changed the settings: the complete set arrives as {"values": {...}} and takes effect at once."""
    try:
        values = (await request.json())["values"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, 'Expected {"values": {...}}')
    changed = settings.apply(values)
    if changed & CONNECTION_KEYS and not settings.config_problem():
        import threading
        threading.Thread(target=lambda: (catalog.reset(), catalog.start_scan(True)), daemon=True).start()  # another cloud or folder: start over
    return {"info": _info()}


@router.post("/actions/{action_id}")
async def run_action(action_id: str, request: Request):
    values = (await request.json()).get("values") or {}
    problem = settings.problem_texts()
    if action_id == "scan":
        if problem[0]:
            return {"ok": False, "message": _say(*problem)}
        if not catalog.start_scan(bool(values.get("full"))):
            return {"ok": False, "message": _say("A scan is already running.", "Es läuft bereits ein Einlesen.")}
        return {"ok": True, "message": _say("The scan has started. It runs in the background; see \"Tracks in the index\" in a while.",
                                            "Das Einlesen hat begonnen. Es läuft im Hintergrund; schau später bei „Titel im Index“.")}
    if action_id == "test":
        if problem[0]:
            return {"ok": False, "message": _say(*problem)}
        try:
            n = len(await asyncio.to_thread(dav.list_folder, ""))
        except dav.DavError as e:
            return {"ok": False, "message": _say(e.en, e.de)}
        return {"ok": True, "message": _say(f"Connected. The music folder has {n} entries.", f"Verbunden. Der Musikordner hat {n} Einträge.")}
    raise HTTPException(404, "unknown action")


app.include_router(router)
