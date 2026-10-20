"""The Nextcloud/ownCloud plugin against the NxtSound plugin protocol and its own logic (the cloud is simulated)."""
import catalog
import dav
import settings
from tests.conftest import BODY, PASSWORD, TREE, USER
from tests.vendor import plugincheck, pluginproto


def ids(items):
    return [i["id"] for i in items]


def titles(client, q, kind="songs"):
    return [i.get("title") or i.get("name") for i in client.get("/search", params={"q": q, "kind": kind}).json()["items"]]


# ---------- Artist, album and title from the path ----------

def test_names_are_guessed_from_folders_and_file_names():
    g = catalog.guess
    assert g("Artist A/Album X/01 - Alpha.mp3") == {"title": "Alpha", "artist": "Artist A", "album": "Album X", "track_no": 1}
    assert g("Artist B/Single Song.m4a") == {"title": "Single Song", "artist": "", "album": "Artist B", "track_no": 0}  # one folder, no artist in the name: the folder is the album
    assert g("Loose Artist - Loose Title.mp3") == {"title": "Loose Title", "artist": "Loose Artist", "album": "", "track_no": 0}
    assert g("Sub/Deep/Artist C/2020 - Album Z/CD1/03. Gamma.ogg") == {"title": "Gamma", "artist": "Artist C", "album": "Album Z", "track_no": 3}  # year and disc folders
    assert g("A/B/1-05 Song.flac")["track_no"] == 5 and g("A/B/Album (2019)/Song.mp3")["album"] == "Album"
    assert g("Band/Band - Song.mp3") == {"title": "Song", "artist": "Band", "album": "", "track_no": 0}  # the folder is the artist, not an album
    assert g("Artist/Album/Artist - Song.mp3")["artist"] == "Artist" and g("Artist/Album/Artist - Song.mp3")["title"] == "Song"
    assert g("Artist/Album/99 Luftballons.mp3")["title"] == "99 Luftballons"  # a title that starts with a number but has no separator
    assert g("just_a_file_name.mp3")["title"] == "just a file name"
    assert g("Various Artists/Best Of 2020/Queen - Bohemian Rhapsody.mp3")["artist"] == "Queen"  # compilations: the artist is in the file name
    assert g("Various/Best Of/Song Without Artist.mp3")["artist"] == "Various"  # nothing better to say


# ---------- Scanning and searching ----------

def test_the_scan_reads_music_and_ignores_the_rest(indexed):
    assert catalog.count() == 6  # the cover is not music
    assert catalog.STATE["error"] == "" and catalog.STATE["files"] == 6 and catalog.last_scan() > 0


def test_songs_artists_and_albums_are_found(client):
    assert titles(client, "alpha") == ["Alpha"]
    assert sorted(titles(client, "album x")) == ["Alpha", "Beta"]  # a word of the album finds its songs
    assert titles(client, "album x", "albums") == ["Album X"] and titles(client, "artist", "artists") == ["Artist A", "Artist C", "Artist D", "Loose Artist"]
    assert titles(client, "Alpha Artist A Album") == ["Alpha"]  # all words must match, in any field
    assert titles(client, "alp") == ["Alpha"] and titles(client, "pha") == []  # words match from their start, not from the middle
    assert titles(client, "nothing here") == []


def test_accents_and_case_do_not_matter(client):
    assert titles(client, "UNICODE") == ["Ünïcode"] and titles(client, "cafe muller", "albums") == ["Café Müller"] and titles(client, "ÜNÏ") == ["Ünïcode"]


def test_search_values_are_safe(client):
    for q in ("%", "_", "'; DROP TABLE files; --", "\\", "a%b"):
        assert client.get("/search", params={"q": q}).status_code == 200
    assert titles(client, "alpha") == ["Alpha"]  # the index is intact
    assert client.get("/search", params={"q": "x", "kind": "videos"}).status_code == 400


def test_track_cards_follow_the_protocol(client):
    t = client.get("/search", params={"q": "alpha"}).json()["items"][0]
    assert set(t) == {"id", "title", "artist", "artist_id", "album", "album_id", "duration", "thumb"}
    assert (t["artist"], t["album"]) == ("Artist A", "Album X") and t["artist_id"] and t["album_id"] and t["thumb"] == ""


def test_suggestions(client):
    s = client.get("/suggest", params={"q": "art"}).json()["suggestions"]
    assert "Artist A" in s and len(s) == len({x.lower() for x in s})  # artists, titles and albums that start with it, without duplicates
    assert client.get("/suggest", params={"q": "alp"}).json() == {"suggestions": ["Alpha"]}


def test_artist_and_album_pages(client):
    a = client.get("/search", params={"q": "artist a", "kind": "artists"}).json()["items"][0]
    page = client.get("/artist/" + a["id"]).json()
    assert page["name"] == "Artist A" and [s["title"] for s in page["songs"]] == ["Alpha", "Beta"] and page["albums"][0]["title"] == "Album X"
    al = client.get("/search", params={"q": "album x", "kind": "albums"}).json()["items"][0]
    album = client.get("/album/" + al["id"]).json()
    assert album["title"] == "Album X" and album["artist"] == "Artist A" and [t["title"] for t in album["tracks"]] == ["Alpha", "Beta"]  # in track order
    assert client.get("/artist/nope").status_code == 404 and client.get("/album/" + "z" * 20).status_code == 404 and client.get("/album/bad id!").status_code == 404


# ---------- Rescans ----------

def propfinds(cloud):
    return [r for m, r in cloud.requests if m == "PROPFIND"]


def test_a_rescan_without_changes_reads_only_the_top_folder(indexed):
    first = len(propfinds(indexed))
    assert first > 6
    indexed.requests.clear()
    catalog.scan()
    assert propfinds(indexed) == [""]  # every subfolder has the same ETag: nothing to read


def test_only_changed_folders_are_read_again(indexed):
    indexed.files["Sub/Deep/Artist C/2020 - Album Z/CD1/04. Delta.ogg"] = b"delta"
    indexed.requests.clear()
    catalog.scan()
    read = propfinds(indexed)
    assert "" in read and "Sub/Deep/Artist C/2020 - Album Z/CD1" in read  # the chain down to the new file
    assert "Artist A" not in read and "Artist A/Album X" not in read and "Artist B" not in read  # untouched folders are skipped
    assert catalog.count() == 7


def test_removed_files_and_folders_leave_the_index(indexed):
    del indexed.files["Artist A/Album X/02 - Beta.flac"]
    catalog.scan()
    assert catalog.count() == 5
    for p in [p for p in indexed.files if p.startswith("Sub/")]:
        del indexed.files[p]
    catalog.scan()
    assert catalog.count() == 4
    with catalog.db() as cx:
        assert not [r for r in cx.execute("SELECT path FROM folders WHERE path LIKE 'Sub%'")]  # the folders are forgotten too


def test_a_full_scan_reads_everything_again(indexed):
    indexed.requests.clear()
    catalog.scan(full=True)
    assert len(propfinds(indexed)) == len(indexed.folders())


def test_a_failed_scan_keeps_the_index_and_reports_why(indexed):
    indexed.password = "changed"
    catalog.scan()
    assert catalog.STATE["error"].startswith("Login failed") and catalog.count() == 6 and catalog.STATE["running"] is False


# ---------- Streaming ----------

def a_track(client, q="alpha"):
    return client.get("/search", params={"q": q}).json()["items"][0]["id"]


def test_streaming_passes_the_file_through_with_ranges(client):
    tid = a_track(client, "beta")
    r = client.get("/stream/" + tid)
    assert r.status_code == 200 and r.content == BODY and r.headers["content-type"] == "audio/flac" and r.headers["accept-ranges"] == "bytes"  # the type comes from the extension
    part = client.get("/stream/" + tid, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == BODY[100:200] and part.headers["content-range"] == "bytes 100-199/4096"
    assert client.get("/stream/" + a_track(client)).headers["content-type"] == "audio/mpeg"


def test_streaming_errors(client, cloud):
    assert client.get("/stream/unknownid").status_code == 404 and client.get("/stream/bad id").status_code == 404
    tid = a_track(client)
    cloud.password = "changed"
    r = client.get("/stream/" + tid)
    assert r.status_code == 502 and "Login failed" in r.json()["detail"]
    cloud.password = PASSWORD
    del cloud.files["Artist A/Album X/01 - Alpha.mp3"]  # gone from the cloud, still in the index
    assert client.get("/stream/" + tid).status_code == 502


# ---------- Protocol, token, settings ----------

def test_speaks_the_plugin_protocol(client, monkeypatch):
    # the check puts the manifest's default settings into the plugin: for this test the defaults are the test cloud, so the stream check can still reach it
    mine = {"url": "https://cloud.example.com", "username": "alice", "password": PASSWORD, "folder": "/Music"}
    monkeypatch.setattr(settings, "SCHEMA", [{**e, "default": mine.get(e["key"], e["default"])} for e in settings.SCHEMA])
    assert plugincheck.check("http://plugin.test", "secret", "artist", transport=client._transport) == []


def test_without_settings_the_cloud_is_simply_not_reachable_not_a_crash(client):
    settings.apply({"url": "", "password": ""})
    assert client.get("/stream/" + a_track(client)).status_code == 502 and client.post("/actions/test", json={}).json()["ok"] is False
    assert catalog.songs("alpha", 5)  # what is already indexed stays searchable


def test_manifest_is_valid(client):
    m = pluginproto.validate_manifest(client.get("/manifest").json())
    assert m["id"] == "nextcloud" and m["name"] and m["description"] and m["version"]
    assert [s["key"] for s in m["settings"]] == ["url", "username", "password", "folder", "rescan_hours"]
    assert {s["key"] for s in m["settings"] if s.get("secret")} == {"password"}
    assert [a["id"] for a in m["actions"]] == ["scan", "test"] and set(m["search_kinds"]) == {"songs", "artists", "albums"}


def test_requires_token(anon):
    assert anon.get("/manifest").status_code == 401
    assert anon.get("/search", params={"q": "x"}, headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert anon.get("/stream/x").status_code == 401 and anon.post("/actions/test", json={}).status_code == 401
    assert anon.get("/health").json() == {"ok": True}


def test_settings_are_applied_and_validated(client):
    r = client.put("/settings", json={"values": {"folder": "/Other", "rescan_hours": 6}})
    assert r.status_code == 200 and settings.values["folder"] == "/Other" and settings.values["rescan_hours"] == 6
    client.put("/settings", json={"values": {"rescan_hours": 999, "folder": "x" * 301, "nope": 1}})
    assert settings.values["rescan_hours"] == 6 and settings.values["folder"] == "/Other"  # invalid values and unknown keys are ignored
    assert client.put("/settings", json={"nope": 1}).status_code == 400


def test_a_different_cloud_or_folder_starts_over(client, monkeypatch):
    started = []
    import threading
    class Immediate(threading.Thread):  # run the "start over" thread at once
        def start(self):
            self.run()
            started.append(1)
    monkeypatch.setattr("threading.Thread", Immediate)
    monkeypatch.setattr(catalog, "start_scan", lambda full=False: started.append(("scan", full)))
    client.put("/settings", json={"values": {"rescan_hours": 12}})
    assert started == []  # an unrelated setting changes nothing
    client.put("/settings", json={"values": {"folder": "/Else"}})
    assert ("scan", True) in started and catalog.count() == 0  # the old index is gone, a full scan starts


def test_info_reports_the_state_without_secrets(client):
    info = {i["label"]["en"]: i["value"] for i in client.get("/settings").json()["info"]}
    assert info["Tracks in the index"] == "6" and info["Last scan"] != "-" and "Problem" not in info
    assert PASSWORD not in client.get("/settings").text
    settings.apply({"password": ""})
    assert "Problem" in {i["label"]["en"] for i in client.get("/settings").json()["info"]}


# ---------- Actions ----------

def test_the_connection_test_says_what_is_wrong(client, cloud):
    ok = client.post("/actions/test", json={"values": {}}).json()
    assert ok["ok"] is True and "5 entries" in ok["message"]["en"] and "Verbunden" in ok["message"]["de"]
    cloud.password = "changed"
    assert "Login failed" in client.post("/actions/test", json={"values": {}}).json()["message"]["en"]
    cloud.password, cloud.root_missing = PASSWORD, True
    assert "music folder" in client.post("/actions/test", json={"values": {}}).json()["message"]["en"]
    cloud.root_missing, cloud.garbage = False, True
    assert "not WebDAV" in client.post("/actions/test", json={"values": {}}).json()["message"]["en"]  # also a hostile answer (entity bomb) is refused
    settings.apply({"url": "ftp://x"})
    assert client.post("/actions/test", json={"values": {}}).json()["ok"] is False
    assert client.post("/actions/nope", json={"values": {}}).status_code == 404


def test_the_scan_action_starts_a_scan(client, monkeypatch):
    calls = []
    monkeypatch.setattr(catalog, "start_scan", lambda full=False: calls.append(full) or True)
    assert client.post("/actions/scan", json={"values": {"full": True}}).json()["ok"] is True and calls == [True]
    monkeypatch.setattr(catalog, "start_scan", lambda full=False: False)
    assert client.post("/actions/scan", json={"values": {}}).json()["ok"] is False  # already running


# ---------- The WebDAV address ----------

def test_the_webdav_address_is_built_from_what_the_admin_enters(cloud):
    assert dav.root_url() == "https://cloud.example.com/remote.php/dav/files/alice/Music"
    settings.apply({"url": "https://cloud.example.com/", "folder": "Music/Rock & Roll/"})
    assert dav.root_url() == "https://cloud.example.com/remote.php/dav/files/alice/Music/Rock%20%26%20Roll"
    settings.apply({"url": "https://cloud.example.com/nextcloud", "username": "a b@c"})
    assert dav.root_url() == "https://cloud.example.com/nextcloud/remote.php/dav/files/a%20b%40c/Music/Rock%20%26%20Roll"  # a subfolder install, a user name with odd characters
    settings.apply({"url": "https://cloud.example.com/remote.php/webdav/", "folder": "/Music"})
    assert dav.root_url() == "https://cloud.example.com/remote.php/webdav/Music"  # a full WebDAV address is used as it is
    settings.apply({"folder": "/"})
    assert dav.root_url() == "https://cloud.example.com/remote.php/webdav"


def test_unconfigured_means_empty_not_an_error(anon, monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "DB_PATH", tmp_path / "empty.db")
    monkeypatch.setattr(settings, "values", {e["key"]: e["default"] for e in settings.SCHEMA})
    assert settings.config_problem()
    from fastapi.testclient import TestClient
    import app as appmod
    with TestClient(appmod.app, headers={"Authorization": "Bearer secret"}) as c:
        assert c.get("/search", params={"q": "x"}).json() == {"items": []} and c.get("/suggest", params={"q": "x"}).json() == {"suggestions": []}


# ---------- The address as typed ----------

def test_an_address_without_a_scheme_means_https(cloud):
    settings.apply({"url": "cloud.example.com"})
    assert settings.values["url"] == "https://cloud.example.com" and settings.config_problem() == ""
    assert dav.root_url() == "https://cloud.example.com/remote.php/dav/files/alice/Music"
    settings.apply({"url": "  cloud.example.com/nextcloud/  "})
    assert settings.values["url"] == "https://cloud.example.com/nextcloud"  # spaces and the trailing slash go
    settings.apply({"url": "http://192.168.1.5:8080/"})
    assert settings.values["url"] == "http://192.168.1.5:8080"  # an explicit http:// (a server in the home network) stays


def test_invalid_addresses_are_explained_in_both_languages(client):
    for bad in ("ftp://cloud.example.com", "https://user:secret@cloud.example.com", "https://", "a b"):
        settings.apply({"url": bad})
        en, de = settings.problem_texts()
        assert "not valid" in en and "ungültig" in de, bad
    settings.apply({"url": "cloud.example.com", "password": ""})
    assert settings.problem_texts() == ("Address, user name and app password are needed.", "Adresse, Benutzername und App-Passwort werden gebraucht.")


def test_messages_come_in_german_too(client, cloud):
    cloud.password = "changed"
    m = client.post("/actions/test", json={"values": {}}).json()["message"]
    assert m["en"].startswith("Login failed") and m["de"].startswith("Anmeldung fehlgeschlagen")
    cloud.password = PASSWORD
    cloud.root_missing = True
    assert "Musikordner wurde" in client.post("/actions/test", json={"values": {}}).json()["message"]["de"]
    settings.apply({"password": ""})
    info = {i["label"]["de"]: i["value"] for i in client.get("/settings").json()["info"]}
    assert info["Problem"]["de"].startswith("Adresse, Benutzername")


def test_a_scan_error_is_reported_in_german_too(indexed, client):
    indexed.password = "changed"
    catalog.scan()
    info = {i["label"]["de"]: i["value"] for i in client.get("/settings").json()["info"]}
    assert info["Problem"]["en"].startswith("Login failed") and info["Problem"]["de"].startswith("Anmeldung fehlgeschlagen")
