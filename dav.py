"""A minimal WebDAV client for Nextcloud and ownCloud: list a folder (PROPFIND) and open a file (GET with Range)."""
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

import httpx
from defusedxml import ElementTree as ET  # a server must not be able to blow up the parser

import settings

DAV = "{DAV:}"
PROPFIND = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getetag/></d:prop></d:propfind>')
HEADERS = {"User-Agent": "nxtsound-plugin-nextcloud/0.1"}


class DavError(Exception):
    """The cloud cannot be reached or refuses (the text is meant for the admin, in English and German)."""

    def __init__(self, en: str, de: str = ""):
        super().__init__(en)
        self.en, self.de = en, de or en


@dataclass(frozen=True)
class Entry:
    path: str  # relative to the music folder, no leading or trailing slash ("" is the music folder itself)
    is_dir: bool
    size: int
    etag: str


def root_url() -> str:
    """The WebDAV address of the music folder."""
    url, user, folder = settings.values["url"].rstrip("/"), settings.values["username"], settings.values["folder"].strip()
    base = url if "/remote.php/" in url else f"{url}/remote.php/dav/files/{quote(user, safe='')}"
    return base.rstrip("/") + "/" + quote(folder.strip("/"), safe="/") if folder.strip("/") else base


def http() -> httpx.Client:
    return httpx.Client(auth=(settings.values["username"], settings.values["password"]), headers=HEADERS, follow_redirects=True,
                        timeout=httpx.Timeout(15, read=30))


def _url(rel: str) -> str:
    return root_url() + ("/" + quote(rel, safe="/") if rel else "") + ("/" if not rel else "")


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code == 401:
        raise DavError("Login failed: check the user name and the app password.", "Anmeldung fehlgeschlagen: Prüfe Benutzername und App-Passwort.")
    if r.status_code == 404:
        raise DavError(f"{what} was not found in the cloud: check the music folder.", f"{'Der Ordner' if what == 'The folder' else 'Der Musikordner' if what == 'The music folder' else 'Die Datei'} wurde in der Cloud nicht gefunden: Prüfe den Musikordner.")
    if r.status_code >= 400:
        raise DavError(f"The cloud answered with HTTP {r.status_code}.", f"Die Cloud hat mit HTTP {r.status_code} geantwortet.")


def _ready() -> None:
    en, de = settings.problem_texts()
    if en:
        raise DavError(en, de)


def list_folder(rel: str) -> list[Entry]:
    """The direct children of a folder (the folder itself is left out)."""
    _ready()
    try:
        with http() as c:
            r = c.request("PROPFIND", _url(rel), headers={"Depth": "1", "Content-Type": "application/xml"}, content=PROPFIND)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise DavError(f"The cloud cannot be reached ({e.__class__.__name__}).", f"Die Cloud ist nicht erreichbar ({e.__class__.__name__}).") from e
    _check(r, "The folder" if rel else "The music folder")
    try:
        tree = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001 (any parser error: the answer is not WebDAV)
        raise DavError("The answer is not WebDAV: is this the address of Nextcloud/ownCloud?",
                       "Die Antwort ist kein WebDAV: Ist das die Adresse von Nextcloud/ownCloud?") from e
    base = unquote(urlparse(root_url()).path).rstrip("/")
    out: list[Entry] = []
    for resp in tree.iter(DAV + "response"):
        href = unquote(urlparse((resp.findtext(DAV + "href") or "").strip()).path)
        child = href[len(base):].strip("/") if href.startswith(base) else None
        if child is None or child == rel:
            continue  # outside the music folder, or the folder itself
        prop = resp.find(f"{DAV}propstat/{DAV}prop")
        if prop is None:
            continue
        is_dir = prop.find(f"{DAV}resourcetype/{DAV}collection") is not None
        size = prop.findtext(DAV + "getcontentlength") or "0"
        out.append(Entry(child, is_dir, int(size) if size.isdigit() else 0, (prop.findtext(DAV + "getetag") or "").strip('"')))
    return out


def open_file(rel: str, range_header: str = ""):
    """Opens a file for streaming. -> (client, response); the caller closes both."""
    _ready()
    client = http()
    headers = {"Range": range_header} if re.match(r"^bytes=\d*-\d*$", range_header or "") else {}
    try:
        resp = client.send(client.build_request("GET", _url(rel).rstrip("/"), headers=headers), stream=True)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        client.close()
        raise DavError(f"The cloud cannot be reached ({e.__class__.__name__}).", f"Die Cloud ist nicht erreichbar ({e.__class__.__name__}).") from e
    if resp.status_code not in (200, 206):
        resp.close()
        client.close()
        _check(resp, "The file")
        raise DavError(f"The cloud answered with HTTP {resp.status_code}.", f"Die Cloud hat mit HTTP {resp.status_code} geantwortet.")
    return client, resp
