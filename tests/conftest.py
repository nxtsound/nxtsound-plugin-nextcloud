"""Test setup: Nextcloud/ownCloud is replaced by a small WebDAV stand-in (no network)."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote

_tmp = tempfile.mkdtemp(prefix="plugin-nextcloud-test-")
os.environ["PLUGIN_TOKEN"] = "secret"
os.environ["PLUGIN_DATA_DIR"] = str(Path(_tmp) / "data")
os.environ["NO_SCAN_LOOP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import dav  # noqa: E402
import settings  # noqa: E402

USER, PASSWORD = "alice", "app-pass"
ROOT = "/remote.php/dav/files/alice/Music"
BODY = bytes(range(256)) * 16  # 4096 bytes of "music"

TREE = {
    "Artist A/Album X/01 - Alpha.mp3": b"alpha" * 100,
    "Artist A/Album X/02 - Beta.flac": BODY,
    "Artist A/Album X/cover.jpg": b"jpg",  # not music: ignored
    "Artist B/Single Song.m4a": b"single" * 50,
    "Loose Artist - Loose Title.mp3": b"loose" * 10,
    "Sub/Deep/Artist C/2020 - Album Z/CD1/03. Gamma.ogg": b"gamma" * 20,
    "Artist D/Café Müller/01 - Ünïcode.mp3": b"uni" * 10,
}


class Cloud:
    """A Nextcloud-like server: folders have an ETag that changes when anything below them changes."""

    def __init__(self):
        self.files = dict(TREE)
        self.requests: list[tuple[str, str]] = []  # (method, relative path), for the tests
        self.user, self.password = USER, PASSWORD
        self.root_missing = False
        self.garbage = False

    def folders(self) -> set[str]:
        out = {""}
        for p in self.files:
            parts = p.split("/")[:-1]
            out |= {"/".join(parts[:i + 1]) for i in range(len(parts))}
        return out

    def etag(self, folder: str) -> str:
        prefix = folder + "/" if folder else ""
        inside = sorted((p, hashlib.md5(b).hexdigest()) for p, b in self.files.items() if p.startswith(prefix))
        return hashlib.md5(repr(inside).encode()).hexdigest()[:12]

    def entry(self, rel: str, is_dir: bool) -> str:
        href = quote(ROOT + ("/" + rel if rel else "") + ("/" if is_dir else ""))
        if is_dir:
            prop = f'<d:resourcetype><d:collection/></d:resourcetype><d:getetag>"{self.etag(rel)}"</d:getetag>'
        else:
            prop = f'<d:resourcetype/><d:getcontentlength>{len(self.files[rel])}</d:getcontentlength><d:getetag>"f{len(self.files[rel])}"</d:getetag>'
        return f'<d:response><d:href>{href}</d:href><d:propstat><d:prop>{prop}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'

    def handler(self, req: httpx.Request) -> httpx.Response:
        import base64
        if req.headers.get("authorization") != "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode():
            return httpx.Response(401)
        path = unquote(req.url.path)
        if not path.startswith(ROOT) or self.root_missing:
            return httpx.Response(404)
        rel = path[len(ROOT):].strip("/")
        self.requests.append((req.method, rel))
        if req.method == "PROPFIND":
            if self.garbage:
                return httpx.Response(207, content=b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><x>&b;</x>')
            if rel not in self.folders():
                return httpx.Response(404)
            prefix = rel + "/" if rel else ""
            kids = {p[len(prefix):].split("/")[0] for p in self.files if p.startswith(prefix)}
            parts = [self.entry(rel, True)]
            for k in sorted(kids):
                child = prefix + k
                parts.append(self.entry(child, child in self.folders()))
            return httpx.Response(207, content=('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + "".join(parts) + "</d:multistatus>").encode())
        if req.method == "GET":
            if rel not in self.files:
                return httpx.Response(404)
            data, rng = self.files[rel], req.headers.get("range", "")
            if rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start, end = int(a or 0), int(b) if b else len(data) - 1
                return httpx.Response(206, content=data[start:end + 1], headers={"content-range": f"bytes {start}-{min(end, len(data) - 1)}/{len(data)}", "content-type": "application/octet-stream"})
            return httpx.Response(200, content=data, headers={"content-type": "application/octet-stream"})
        return httpx.Response(405)


@pytest.fixture()
def cloud(monkeypatch, tmp_path):
    c = Cloud()
    transport = httpx.MockTransport(c.handler)

    class Client(httpx.Client):
        def __init__(self, **kw):
            super().__init__(transport=transport, **kw)

    monkeypatch.setattr(dav, "httpx", SimpleNamespace(Client=Client, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout, Response=httpx.Response, InvalidURL=httpx.InvalidURL))
    monkeypatch.setattr(catalog, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(catalog, "STATE", {"running": False, "folders": 0, "files": 0, "started": 0.0, "finished": 0.0, "error": "", "error_de": ""})
    monkeypatch.setattr(settings, "values", {e["key"]: e["default"] for e in settings.SCHEMA})
    settings.apply({"url": "https://cloud.example.com", "username": USER, "password": PASSWORD, "folder": "/Music"})
    return c


@pytest.fixture()
def indexed(cloud):
    catalog.scan()
    assert catalog.STATE["error"] == "", catalog.STATE["error"]
    return cloud


@pytest.fixture()
def client(indexed):
    with TestClient(appmod.app, headers={"Authorization": "Bearer secret"}) as c:
        yield c


@pytest.fixture()
def anon(cloud):
    with TestClient(appmod.app) as c:
        yield c
