"""Shared pytest fixtures: API token auth and isolated per-test config paths."""
import pytest


@pytest.fixture(autouse=True)
def isolate_config_dir(tmp_path, monkeypatch):
    """Keep tests from reading or writing the user's real ~/.arena-personal."""
    cfg_dir = tmp_path / ".arena-personal"
    projects_dir = tmp_path / "ArenaProjects"

    from arena import components, config, connectors, crypto, deep_links, models, workspace
    from arena.web import app as web

    monkeypatch.setattr(crypto, "LINUX_KEYRING_ENABLED", False)
    monkeypatch.setattr(crypto, "_LINUX_KEY_CACHE", None)
    monkeypatch.setattr(crypto, "_LINUX_KEYRING_CHECKED", False)

    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_dir / "config.json")
    monkeypatch.setattr(config, "COOKIE_FILE", cfg_dir / "cookies.txt")
    monkeypatch.setattr(config, "HIST_FILE", cfg_dir / "history.txt")
    monkeypatch.setattr(config, "PID_FILE", cfg_dir / "arena.pid")
    monkeypatch.setattr(config, "CHATS_DIR", cfg_dir / "chats")

    monkeypatch.setattr(models, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(models, "CACHE_FILE", cfg_dir / "models_cache.json")
    monkeypatch.setattr(connectors, "MCP_CONFIG_FILE", cfg_dir / "mcp.json")
    monkeypatch.setattr(deep_links, "_INSTANCE_FILE", cfg_dir / "instance.json")
    monkeypatch.setattr(components, "BIN_DIR", cfg_dir / "bin")
    monkeypatch.setattr(workspace, "PROJECTS_DIR", projects_dir)

    monkeypatch.setattr(web, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(web, "COOKIE_FILE", cfg_dir / "cookies.txt")
    monkeypatch.setattr(web, "CHATS_DIR", cfg_dir / "chats")
    monkeypatch.setattr(web, "GLOBAL_MEMORY_FILE", cfg_dir / "memory.md")
    monkeypatch.setattr(web, "ACCOUNTS_FILE", cfg_dir / "accounts.json")
    monkeypatch.setattr(web, "USAGE_FILE", cfg_dir / "usage.json")
    monkeypatch.setattr(web, "_USAGE", None)


@pytest.fixture(autouse=True)
def authenticated_test_client(monkeypatch):
    from arena.web import app as web
    original = web.app.test_client

    def create(*args, **kwargs):
        client = original(*args, **kwargs)
        client.environ_base['HTTP_X_ARENA_TOKEN'] = web._API_TOKEN
        return client

    monkeypatch.setattr(web.app, 'test_client', create)
