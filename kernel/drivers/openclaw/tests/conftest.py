"""Native protocol fixtures choose opt-in explicitly, never the user's setting."""
import pytest

@pytest.fixture(autouse=True)
def native_computer_policy_baseline(tmp_path, monkeypatch):
    from drivers.computer_use import settings
    monkeypatch.setenv("KAUS_COMPUTER_USE_HOME", str(tmp_path / "computer-use"))
    settings.save_enabled(True)
