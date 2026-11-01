"""App icons: favicon, manifest and the icon files are served without logging in."""
from fastapi.testclient import TestClient


def test_icons_and_manifest_are_public(client):
    anon = TestClient(client.app)
    assert anon.get("/favicon.ico").status_code == 200
    m = anon.get("/manifest.json")
    assert m.status_code == 200 and m.json()["name"] == "IACF Finance"
    for i in m.json()["icons"]:
        assert anon.get(i["src"]).status_code == 200, i["src"]
    for p in ("/static/icons/apple-touch-icon.png", "/static/icons/favicon-32.png", "/static/icons/iacf-mark.svg"):
        assert anon.get(p).status_code == 200, p
