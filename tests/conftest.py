import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own data dir; nothing touches the real one."""
    monkeypatch.setenv("STUDYSYSTEM_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"
