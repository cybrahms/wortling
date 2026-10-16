import pytest

from wortling.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CAPTURE_TOKEN", "a" * 48)
    return Settings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "users": [
                {
                    "id": "test",
                    "display_name": "Test",
                    "capture_token_env": "TEST_CAPTURE_TOKEN",
                    "ankiweb_user_env": "TEST_ANKI_USER",
                    "ankiweb_pass_env": "TEST_ANKI_PASS",
                }
            ],
        }
    )
