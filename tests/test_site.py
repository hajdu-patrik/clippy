import hashlib
import json
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
PAGES = ("index.html", "privacy.html", "terms.html")


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


@pytest.mark.parametrize("page", PAGES)
def test_pages_share_one_stylesheet_whose_version_matches_its_content(page):
    html = (DOCS / page).read_text(encoding="utf-8")
    assert "<style" not in html
    for name, version in re.findall(r"assets/(site\.(?:css|js))\?v=(\w+)", html):
        assert version == fingerprint(DOCS / "assets" / name), f"{page}: bump {name}?v= to its new fingerprint"
    assert "assets/site.css?v=" in html


@pytest.mark.parametrize("page", PAGES)
def test_every_local_asset_a_page_uses_exists(page):
    html = (DOCS / page).read_text(encoding="utf-8")
    used = set(re.findall(r'(?<![\w/:.])assets/[\w.-]+', html))
    assert used and all((DOCS / u).is_file() for u in used), sorted(u for u in used if not (DOCS / u).is_file())


def test_the_cache_rules_are_valid_and_keep_pages_fresh():
    rules = json.loads((DOCS / "vercel.json").read_text(encoding="utf-8"))["headers"]
    values = {r["source"]: r["headers"][0]["value"] for r in rules}
    assert "immutable" in values["/assets/site.(css|js)"]
    assert values["/"].startswith("public, max-age=0")


def test_every_page_is_served_with_security_headers():
    rules = json.loads((DOCS / "vercel.json").read_text(encoding="utf-8"))["headers"]
    sent = {h["key"]: h["value"] for r in rules if r["source"] == "/(.*)" for h in r["headers"]}
    assert "default-src 'self'" in sent["Content-Security-Policy"] and "unsafe" not in sent["Content-Security-Policy"]
    assert sent["X-Content-Type-Options"] == "nosniff" and sent["X-Frame-Options"] == "DENY"
    assert {"Referrer-Policy", "Permissions-Policy"} <= set(sent)
