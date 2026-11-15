from pathlib import Path

import pytest

from clippy.editing.assets import Asset, AssetIndex, AssetKind
from clippy.editing.factory import timeline_from_clip
from clippy.editing.operations import EditContext, OperationCatalogue
from clippy.editing.timeref import Clock, TimeResolver

WORDS = [
    {"start": 0.0, "end": 0.4, "word": "Senki"}, {"start": 0.5, "end": 0.8, "word": "nem"},
    {"start": 0.9, "end": 1.3, "word": "tud"}, {"start": 1.4, "end": 2.2, "word": "megállítani."},
    {"start": 5.0, "end": 5.5, "word": "Pénzt"}, {"start": 5.6, "end": 6.0, "word": "keresni"},
    {"start": 6.1, "end": 6.5, "word": "könnyű."}, {"start": 10.0, "end": 10.6, "word": "pénz"},
]


def assets() -> AssetIndex:
    return AssetIndex([
        Asset(id="a1b2c3", kind=AssetKind.AUDIO, name="summer.mp3", path=Path("summer.mp3"), duration=161.0),
        Asset(id="d4e5f6", kind=AssetKind.IMAGE, name="logo.png", path=Path("logo.png"), width=512, height=512),
        Asset(id="aa11bb", kind=AssetKind.VIDEO, name="outro.mp4", path=Path("outro.mp4"), duration=4.0),
        Asset(id="cc22dd", kind=AssetKind.LUT, name="warm.cube", path=Path("warm.cube")),
    ])


@pytest.fixture
def timeline():
    clip = {"start": 100.0, "end": 120.0, "hook": "Elvesztettem magam – miért?"}
    return timeline_from_clip(clip, {"highlight": "green", "cta": "Kövess!", "layout": "face", "hook": True})


@pytest.fixture
def ctx():
    clock = Clock(((100.0, 108.0), (110.0, 122.0)))
    return EditContext(assets=assets(), resolver=TimeResolver(20.0, WORDS), clock=clock, source_duration=600.0)


@pytest.fixture
def words():
    return WORDS


@pytest.fixture
def asset_index():
    return assets()


@pytest.fixture(scope="session")
def catalogue():
    return OperationCatalogue()
