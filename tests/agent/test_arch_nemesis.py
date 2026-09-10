"""Tests for agent/arch_nemesis.py - the arch-nemesis config loader."""

from agent.arch_nemesis import load_arch_nemesis_name


def _write_config(hermes_home, content):
    config_dir = hermes_home / "memories" / "ops"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "arch_nemesis.md").write_text(content, encoding="utf-8")


class TestLoadArchNemesisName:
    def test_missing_file_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert load_arch_nemesis_name() is None

    def test_empty_file_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path, "")

        assert load_arch_nemesis_name() is None

    def test_file_with_no_name_line_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path, "# Arch Nemesis\n\nNo name configured here.\n")

        assert load_arch_nemesis_name() is None

    def test_blank_name_value_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path, "# Arch Nemesis\n\nname:\n")

        assert load_arch_nemesis_name() is None

    def test_valid_file_returns_configured_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path, "# Arch Nemesis\n\nname: Scarlet Mendoza\n")

        assert load_arch_nemesis_name() == "Scarlet Mendoza"

    def test_changing_name_is_reflected_on_next_load(self, monkeypatch, tmp_path):
        # Proves the config file is the single source of truth: the loader
        # reflects whatever is on disk right now, not a cached/hardcoded value.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path, "name: Test Target\n")
        assert load_arch_nemesis_name() == "Test Target"

        _write_config(tmp_path, "name: Scarlet Mendoza\n")
        assert load_arch_nemesis_name() == "Scarlet Mendoza"

    def test_directory_at_config_path_returns_none(self, monkeypatch, tmp_path):
        # read_text() on a directory raises IsADirectoryError - must not crash.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config_dir = tmp_path / "memories" / "ops" / "arch_nemesis.md"
        config_dir.mkdir(parents=True)

        assert load_arch_nemesis_name() is None
