"""The index of the music in the cloud: scanned from the WebDAV folder, searched locally (so searching is fast and never waits for the cloud).

Artist, album and title come from the folders and file names: `Artist/Album/01 - Title.mp3`, `Artist - Title.mp3`, `2020 - Album`, `CD1`.
A rescan only reads folders whose ETag changed (Nextcloud and ownCloud pass a change on to every parent folder).
"""
import contextlib
import hashlib
import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path, PurePosixPath

import dav

DB_PATH = Path(os.getenv("PLUGIN_DATA_DIR", Path(__file__).parent / "data")) / "index.db"
AUDIO = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
         ".opus": "audio/ogg", ".wav": "audio/wav", ".wma": "audio/x-ms-wma", ".aif": "audio/aiff", ".aiff": "audio/aiff"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, dir TEXT NOT NULL, title TEXT, artist TEXT, album TEXT, track_no INTEGER, ext TEXT, search TEXT);
CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY, parent TEXT NOT NULL, etag TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS files_dir ON files(dir);
CREATE INDEX IF NOT EXISTS files_artist ON files(artist);
"""
_lock = threading.Lock()  # one writer at a time
STATE = {"running": False, "folders": 0, "files": 0, "started": 0.0, "finished": 0.0, "error": "", "error_de": ""}


@contextlib.contextmanager
def db():
    """A connection that is committed and closed afterwards."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def fold(s: str) -> str:
    """Lower case without accents, for searching."""
    return "".join(ch for ch in unicodedata.normalize("NFD", s or "") if not unicodedata.combining(ch)).casefold()


def words(text: str) -> list[str]:
    return re.findall(r"\w+", fold(text))


def haystack(*parts: str) -> str:
    """The searchable text of a track: its words, each between spaces (so a word start can be found with ' word%')."""
    return " " + " ".join(words(" ".join(parts))) + " "


def word_match(q: str, text: str) -> bool:
    """Every word of the search starts a word of the text."""
    have = words(text)
    return bool(words(q)) and all(any(h.startswith(w) for h in have) for w in words(q))


def ident(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha1("|".join(p.casefold() for p in parts).encode()).hexdigest()[:24]


# ---------- Artist, album and title from the path ----------

TRACK_PREFIX = re.compile(r"^(?:(\d{1,2})[-.](\d{1,3})|(\d{1,3}))[\s._-]+(?=\S)")  # "01 - ", "1-05 ", "07. "
YEAR_DIR = re.compile(r"^(?:\[?(\d{4})\]?\s*[-–]\s*(.+)|(.+?)\s*[(\[](\d{4})[)\]])$")  # "2020 - Album", "Album (2020)"
DISC_DIR = re.compile(r"^(?:cd|disc|disk)\s*\d+$", re.I)
COMPILATION = re.compile(r"^(?:various(?: artists)?|va|verschiedene(?: interpreten)?|compilation|sampler)$", re.I)  # a folder that is no artist


def guess(rel: str) -> dict:
    """title, artist, album and track number from a path relative to the music folder (empty where unknown)."""
    parts = PurePosixPath(rel).parts
    dirs, name = list(parts[:-1]), parts[-1]
    stem = PurePosixPath(name).stem
    if " " not in stem and "_" in stem:
        stem = stem.replace("_", " ")
    track = 0
    m = TRACK_PREFIX.match(stem)
    # "99 Luftballons" is a title: a bare number is only a track number with a separator ("07. ", "07 - ") or a leading zero ("07 ")
    if m and (m.group(2) or re.search(r"[-._]", m.group(0)) or m.group(3).startswith("0")):
        track, stem = int(m.group(3) or m.group(2)), stem[m.end():]
    pieces = [p.strip() for p in re.split(r"\s+[-–]\s+", stem) if p.strip()]
    file_artist, title = (pieces[0], " - ".join(pieces[1:])) if len(pieces) >= 2 else ("", stem.strip())
    if dirs and DISC_DIR.match(dirs[-1]) and len(dirs) >= 2:
        dirs = dirs[:-1]  # Artist/Album/CD1/01 - Title: the disc folder is not the album
    album = dirs[-1] if dirs else ""
    folder_artist = dirs[-2] if len(dirs) >= 2 else ""
    if (y := YEAR_DIR.match(album)):
        album = (y.group(2) or y.group(3)).strip()
    if len(dirs) == 1 and file_artist and fold(album) == fold(file_artist):
        album = ""  # Artist/Artist - Title: the folder is the artist, not an album
    artist = file_artist if file_artist and (not folder_artist or COMPILATION.match(folder_artist)) else folder_artist or file_artist  # "Various/Album/Queen - Song": the song's artist
    return {"title": title or stem or name, "artist": artist, "album": album, "track_no": track}


# ---------- Scanning ----------

def _store_folder(cx: sqlite3.Connection, rel: str, entries: list[dav.Entry]) -> list[dav.Entry]:
    """Replaces what the index knows about the files directly in `rel`; returns the subfolders found."""
    files = {e.path: e for e in entries if not e.is_dir and PurePosixPath(e.path).suffix.lower() in AUDIO}
    cx.execute("DELETE FROM files WHERE dir = ? AND path NOT IN (%s)" % ",".join("?" * len(files)), [rel, *files])
    for path in files:
        g = guess(path)
        cx.execute("INSERT OR REPLACE INTO files(id, path, dir, title, artist, album, track_no, ext, search) VALUES(?,?,?,?,?,?,?,?,?)",
                   (ident("t", path), path, rel, g["title"], g["artist"], g["album"], g["track_no"], PurePosixPath(path).suffix.lower(),
                    haystack(g["title"], g["artist"], g["album"])))
    subs = [e for e in entries if e.is_dir]
    gone = [r["path"] for r in cx.execute("SELECT path FROM folders WHERE parent = ?", (rel,)) if r["path"] not in {s.path for s in subs}]
    for g in gone:  # folders that no longer exist take their files and subfolders with them
        cx.execute("DELETE FROM files WHERE dir = ? OR substr(dir, 1, ?) = ?", (g, len(g) + 1, g + "/"))
        cx.execute("DELETE FROM folders WHERE path = ? OR substr(path, 1, ?) = ?", (g, len(g) + 1, g + "/"))
    return subs


def scan(full: bool = False) -> None:
    """Reads the music folder into the index (folders whose ETag is unchanged are skipped unless `full`). Meant to run in a background thread."""
    if not _lock.acquire(blocking=False):
        return
    STATE.update(running=True, folders=0, files=0, started=time.time(), error="", error_de="")
    try:
        with db() as cx:
            todo: list[tuple[str, str | None]] = [("", None)]  # (folder, ETag it had in its parent's listing)
            while todo:
                rel, etag = todo.pop()
                entries = dav.list_folder(rel)
                subs = _store_folder(cx, rel, entries)
                known = {r["path"]: r["etag"] for r in cx.execute("SELECT path, etag FROM folders WHERE parent = ?", (rel,))}
                for s in subs:
                    if not full and s.etag and known.get(s.path) == s.etag:
                        continue  # nothing changed in there
                    cx.execute("INSERT OR REPLACE INTO folders(path, parent, etag) VALUES(?,?,NULL)", (s.path, rel))  # the ETag is stored when the folder is done
                    todo.append((s.path, s.etag))
                if etag is not None:
                    cx.execute("UPDATE folders SET etag = ? WHERE path = ?", (etag, rel))
                cx.commit()
                STATE["folders"] += 1
            STATE["files"] = cx.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            cx.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('scanned', ?)", (str(time.time()),))
    except dav.DavError as e:
        STATE["error"], STATE["error_de"] = e.en, e.de
    except Exception as e:  # noqa: BLE001 (a scan must never take the plugin down)
        STATE["error"] = STATE["error_de"] = f"{e.__class__.__name__}: {e}"
    finally:
        STATE.update(running=False, finished=time.time())
        _lock.release()


def start_scan(full: bool = False) -> bool:
    """Starts a scan in the background; False if one is already running."""
    if STATE["running"]:
        return False
    threading.Thread(target=scan, args=(full,), daemon=True, name="scan").start()
    return True


def reset() -> None:
    """Forgets the index (the cloud, user or folder changed)."""
    with _lock, db() as cx:
        cx.executescript("DELETE FROM files; DELETE FROM folders; DELETE FROM meta;")


def last_scan() -> float:
    with db() as cx:
        r = cx.execute("SELECT value FROM meta WHERE key = 'scanned'").fetchone()
    return float(r["value"]) if r else 0.0


def count() -> int:
    with db() as cx:
        return cx.execute("SELECT COUNT(*) FROM files").fetchone()[0]


# ---------- Queries ----------

def _where(q: str, column: str) -> tuple[str, list[str]]:
    """SQL for: every word of the search starts a word of the column (the words themselves are parameters)."""
    ws = words(q)
    return " AND ".join(f"{column} LIKE ? ESCAPE '\\'" for _ in ws) or "0", [f"% {w.replace(chr(92), chr(92) * 2).replace('%', chr(92) + '%').replace('_', chr(92) + '_')}%" for w in ws]


def _track(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "title": r["title"], "artist": r["artist"], "artist_id": ident("a", r["artist"]) if r["artist"] else "",
            "album": r["album"], "album_id": ident("b", r["artist"], r["album"]) if r["album"] else "", "duration": "", "thumb": ""}


def songs(q: str, limit: int) -> list[dict]:
    where, args = _where(q, "search")
    with db() as cx:
        return [_track(r) for r in cx.execute(f"SELECT * FROM files WHERE {where} ORDER BY artist, album, track_no, title LIMIT ?", [*args, limit])]


def artists(q: str, limit: int) -> list[dict]:
    where, args = _where(q, "search")
    with db() as cx:
        rows = cx.execute(f"SELECT DISTINCT artist FROM files WHERE artist != '' AND {where} ORDER BY artist LIMIT ?", [*args, limit * 5]).fetchall()
    names = [r["artist"] for r in rows if word_match(q, r["artist"])]  # the name itself must match, not just one of its songs
    return [{"type": "artist", "id": ident("a", n), "name": n, "thumb": ""} for n in names[:limit]]


def albums(q: str, limit: int) -> list[dict]:
    where, args = _where(q, "search")
    with db() as cx:
        rows = cx.execute(f"SELECT DISTINCT artist, album FROM files WHERE album != '' AND {where} ORDER BY album LIMIT ?", [*args, limit * 5]).fetchall()
    hits = [r for r in rows if word_match(q, f"{r['album']} {r['artist']}")]
    return [_card(r["artist"], r["album"]) for r in hits[:limit]]


def _card(artist: str, album: str) -> dict:
    return {"type": "album", "id": ident("b", artist, album), "title": album, "artist": artist, "year": "", "kind": "Album", "thumb": ""}


def suggestions(q: str, limit: int = 8) -> list[str]:
    where, args = _where(q, "search")
    with db() as cx:
        rows = cx.execute(f"SELECT title, artist, album FROM files WHERE {where} ORDER BY artist, album, title LIMIT 60", args).fetchall()
    out, seen = [], set()
    for r in rows:
        for text in (r["artist"], r["title"], r["album"]):
            if text and fold(text).startswith(fold(q)) and fold(text) not in seen:
                seen.add(fold(text))
                out.append(text)
    return out[:limit]


def artist_page(artist_id: str) -> dict | None:
    with db() as cx:
        names = [r["artist"] for r in cx.execute("SELECT DISTINCT artist FROM files WHERE artist != ''") if ident("a", r["artist"]) == artist_id]
        if not names:
            return None
        name = names[0]
        tracks = [_track(r) for r in cx.execute("SELECT * FROM files WHERE artist = ? ORDER BY album, track_no, title LIMIT 100", (name,))]
        albums_ = [r["album"] for r in cx.execute("SELECT DISTINCT album FROM files WHERE artist = ? AND album != '' ORDER BY album", (name,))]
    return {"id": artist_id, "name": name, "description": "", "subscribers": "", "thumb": "", "songs": tracks,
            "albums": [{k: v for k, v in _card(name, a).items() if k != "type"} for a in albums_], "singles": []}


def album_page(album_id: str) -> dict | None:
    with db() as cx:
        pairs = [(r["artist"], r["album"]) for r in cx.execute("SELECT DISTINCT artist, album FROM files WHERE album != ''") if ident("b", r["artist"], r["album"]) == album_id]
        if not pairs:
            return None
        artist, album = pairs[0]
        tracks = [_track(r) for r in cx.execute("SELECT * FROM files WHERE artist = ? AND album = ? ORDER BY track_no, title", (artist, album))]
    return {"id": album_id, "title": album, "kind": "Album", "year": "", "duration": "", "thumb": "", "artist": artist,
            "artist_id": ident("a", artist) if artist else "", "tracks": tracks}


def path_of(track_id: str) -> tuple[str, str] | None:
    """(path, media type) of a track, None if unknown."""
    with db() as cx:
        r = cx.execute("SELECT path, ext FROM files WHERE id = ?", (track_id,)).fetchone()
    return (r["path"], AUDIO.get(r["ext"], "audio/mpeg")) if r else None
