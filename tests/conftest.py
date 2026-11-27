import os

import pytest

os.environ["CLIPPY_LANG"] = "en"


@pytest.fixture(autouse=True)
def no_sound_event_model(monkeypatch):
    from clippy import audio_events

    monkeypatch.setattr(audio_events, "_ready", False)
