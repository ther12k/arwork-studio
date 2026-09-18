"""Task 41 — Artwork Content Release Pipeline tests.

The pipeline's ONE unbreakable rule: assets are staged and SHA-256-verified
BEFORE the catalog entry is published — a catalog entry pointing at absent
or corrupted bytes is the failure mode this exists to prevent. The tests pin
the ordering by forcing verification failures and asserting the catalog
stayed untouched, plus the version-bumping and immutability contracts.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release_artwork_content as release  # noqa: E402


def write_pack(root: Path, artwork_id: str, regions: bytes, version: str = "1.0.0") -> Path:
    pack = root / artwork_id
    pack.mkdir(parents=True)
    (pack / "artwork.json").write_text(json.dumps({
        "schemaVersion": 1,
        "id": artwork_id,
        "version": version,
        "title": artwork_id.replace("-", " ").title(),
        "format": "color-duel-detailed-vector-1",
        "regionCount": 2,
        "category": "Cozy",
        "difficultyLabel": "Easy",
    }))
    (pack / "regions.json").write_bytes(regions)
    (pack / "palette.json").write_text(json.dumps([{"id": 1, "hex": "#3366CC", "name": "Blue"}]))
    return pack


@pytest.fixture()
def env(tmp_path: Path):
    """A pack source, a release out-area, a game catalog, and a state file."""

    class Env:
        def __init__(self):
            self.packs = tmp_path / "packs"
            self.out = tmp_path / "release"
            self.catalog = tmp_path / "game" / "public" / "artworks" / "catalog.json"
            self.state = tmp_path / "release-state.json"
            self.catalog.parent.mkdir(parents=True)
            self.catalog.write_text(json.dumps({"artworks": [
                {"id": "preexisting", "title": "Preexisting", "manifest": "artworks/preexisting/artwork.json"},
            ]}))

        def run(self, *packs: Path) -> int:
            return release.main([
                "--packs", *[str(p) for p in packs],
                "--out", str(self.out),
                "--catalog", str(self.catalog),
                "--state", str(self.state),
            ])

        def catalog_ids(self) -> list[str]:
            return [a["id"] for a in json.loads(self.catalog.read_text())["artworks"]]

        def entry(self, artwork_id: str) -> dict:
            return next(a for a in json.loads(self.catalog.read_text())["artworks"] if a["id"] == artwork_id)

        def manifest(self) -> dict:
            return json.loads((self.out / "release-manifest.json").read_text())

    return Env()


def test_release_stages_assets_then_publishes_catalog_entry(env):
    pack = write_pack(env.packs, "mosslight-cottage", b'{"regions": []}')

    assert env.run(pack) == 0

    # Assets first: every pack file staged under the VERSIONED directory…
    staged = env.out / "artworks" / "mosslight-cottage" / "1.0.0"
    assert (staged / "artwork.json").is_file()
    assert (staged / "regions.json").read_bytes() == b'{"regions": []}'
    # …the release manifest carries id/version/contentHash/per-file sha256…
    entry = env.manifest()["mosslight-cottage"]
    assert entry["version"] == "1.0.0"
    assert entry["files"]["regions.json"] == hashlib.sha256(b'{"regions": []}').hexdigest()
    assert entry["contentHash"] == release.content_hash_of(entry["files"])
    # …and the catalog entry was published LAST, pointing INTO the versioned
    # directory (the game loader resolves sibling assets relative to it).
    assert env.entry("mosslight-cottage")["manifest"] == "artworks/mosslight-cottage/1.0.0/artwork.json"
    # pre-existing catalog entries survive the upsert.
    assert "preexisting" in env.catalog_ids()


def test_unchanged_content_is_idempotent_and_changed_content_bumps_patch(env):
    pack = write_pack(env.packs, "lantern-cove", b'{"v": 1}')
    assert env.run(pack) == 0
    assert env.entry("lantern-cove")["manifest"] == "artworks/lantern-cove/1.0.0/artwork.json"

    # Same bytes again: same version, no new directory, catalog still points there.
    assert env.run(pack) == 0
    assert env.entry("lantern-cove")["manifest"] == "artworks/lantern-cove/1.0.0/artwork.json"
    assert not (env.out / "artworks" / "lantern-cove" / "1.0.1").exists()

    # Content changes (even with the SAME declared version): the release bumps
    # the previous RELEASE version — releases never repeat a version.
    (pack / "regions.json").write_bytes(b'{"v": 2}')
    assert env.run(pack) == 0
    assert env.entry("lantern-cove")["manifest"] == "artworks/lantern-cove/1.0.1/artwork.json"
    assert (env.out / "artworks" / "lantern-cove" / "1.0.0" / "regions.json").read_bytes() == b'{"v": 1}'
    assert (env.out / "artworks" / "lantern-cove" / "1.0.1" / "regions.json").read_bytes() == b'{"v": 2}'


def test_verification_failure_aborts_before_catalog_write(env, monkeypatch):
    """THE ordering rule: a corrupted stage must never reach the catalog."""
    pack = write_pack(env.packs, "juniper-window", b'{"regions": []}')
    before = env.catalog.read_text()

    # The staged bytes rot in transit: hashing a file under the RELEASE OUT
    # area yields a different digest, while the SOURCE hashes (which formed
    # the expected manifest) stay real.
    real_sha = release.sha256_file

    def rotten(path: Path) -> str:
        digest = real_sha(path)
        if str(path).startswith(str(env.out)) and path.name == "regions.json":
            return "0" * 64
        return digest

    monkeypatch.setattr(release, "sha256_file", rotten)
    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        env.run(pack)

    # The catalog was NOT touched — no entry may point at unverified bytes.
    assert env.catalog.read_text() == before
    assert "juniper-window" not in env.catalog_ids()
    # The state file did not record the broken release either (it was never
    # written — the run aborted before ANY visibility write).
    state = json.loads(env.state.read_text())["packs"] if env.state.is_file() else {}
    assert "juniper-window" not in state


def test_existing_version_directory_must_hold_identical_bytes(env):
    """Releases are immutable: a pre-existing version dir with different bytes
    (hash collision sanity / partial manual tampering) refuses to publish."""
    pack = write_pack(env.packs, "petal-compass", b'{"v": 1}')
    assert env.run(pack) == 0

    # A file silently changes INSIDE the published version dir…
    staged_regions = env.out / "artworks" / "petal-compass" / "1.0.0" / "regions.json"
    staged_regions.write_bytes(b'{"v": TAMPERED}')

    # …releasing the same content must fail the immutable-dir verification
    # instead of silently coexisting with corrupted bytes.
    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        env.run(pack)


def test_multi_pack_release_publishes_in_one_catalog_write(env):
    a = write_pack(env.packs, "pack-a", b'{"a": 1}')
    b = write_pack(env.packs, "pack-b", b'{"b": 2}')
    assert env.run(a, b) == 0
    assert set(env.catalog_ids()) >= {"pack-a", "pack-b", "preexisting"}
    assert set(env.manifest().keys()) >= {"pack-a", "pack-b"}
