"""Computer-use integration scenarios opt in inside disposable settings only."""
import pytest

@pytest.fixture(autouse=True)
def isolated_enabled_computer_use(tmp_path, monkeypatch):
    from drivers.computer_use import settings
    monkeypatch.setenv("KAUS_COMPUTER_USE_HOME", str(tmp_path / "computer-settings"))
    settings.save_enabled(True)
