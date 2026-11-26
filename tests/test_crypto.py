import json

from arena import config, crypto
from arena.web import app as web


def _fake_linux_keyring(monkeypatch):
    store = {}
    monkeypatch.setattr(crypto, "LINUX_KEYRING_ENABLED", True)
    monkeypatch.setattr(crypto, "_LINUX_KEY_CACHE", None)
    monkeypatch.setattr(crypto, "_LINUX_KEYRING_CHECKED", False)
    monkeypatch.setattr(crypto, "_secret_tool_lookup", lambda: store.get("key"))

    def fake_store(key: bytes) -> bool:
        store["key"] = key
        return True

    monkeypatch.setattr(crypto, "_secret_tool_store", fake_store)
    return store


def test_legacy_cookie_is_never_base64_mangled():
    legacy = "arena-auth-prod-v1.0=abc%3D%3D; refresh=x+y/z==; theme=dark"
    assert crypto.decode_cookie(legacy) == legacy
    assert crypto.unprotect_text(legacy) == legacy
    assert crypto.is_protected(legacy) is False


def test_linux_secret_roundtrip_uses_versioned_prefix(monkeypatch):
    _fake_linux_keyring(monkeypatch)
    secret = "arena-auth-prod-v1.0=secret-token-value"
    stored = crypto.protect_text(secret)
    assert stored.startswith(crypto.LINUX_PREFIX)
    assert "secret-token-value" not in stored
    assert crypto.unprotect_text(stored) == secret
    assert crypto.protect_text(stored) == stored


def test_bad_or_unavailable_secret_value_fails_closed(monkeypatch):
    assert crypto.decode_cookie(crypto.LINUX_PREFIX + "not valid base64!") == ""
    assert crypto.decode_cookie(crypto.PREFIX + "dGVzdA==") == ""
    store = _fake_linux_keyring(monkeypatch)
    stored = crypto.protect_text("cookie=value")
    store.clear()
    monkeypatch.setattr(crypto, "_LINUX_KEY_CACHE", None)
    assert crypto.decode_cookie(stored) == ""


def test_config_cookie_file_migrates_legacy_plaintext(tmp_path, monkeypatch):
    _fake_linux_keyring(monkeypatch)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    cookie_file = tmp_path / "cookies.txt"
    monkeypatch.setattr(config, "COOKIE_FILE", cookie_file)
    cookie_file.write_text("arena-auth-prod-v1.0=legacy-value", encoding="utf-8")

    assert config.get_cookie() == "arena-auth-prod-v1.0=legacy-value"
    stored = cookie_file.read_text(encoding="utf-8")
    assert stored.startswith(crypto.LINUX_PREFIX)
    assert "legacy-value" not in stored

    config.save_cookie("arena-auth-prod-v1.0=new-value")
    assert crypto.is_protected(cookie_file.read_text(encoding="utf-8"))
    assert config.get_cookie() == "arena-auth-prod-v1.0=new-value"


def test_account_store_encrypts_at_rest_and_migrates(tmp_path, monkeypatch):
    _fake_linux_keyring(monkeypatch)
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(web, "ACCOUNTS_FILE", accounts_file)

    account_id = web._register_account(
        {"email": "alice@local.invalid"}, "alice-secret-cookie")
    raw = json.loads(accounts_file.read_text(encoding="utf-8"))
    assert raw["accounts"][0]["id"] == account_id
    assert crypto.is_protected(raw["accounts"][0]["cookie"])
    assert "alice-secret-cookie" not in accounts_file.read_text(encoding="utf-8")
    assert web._accounts_load()["accounts"][0]["cookie"] == "alice-secret-cookie"

    raw["accounts"][0]["cookie"] = "alice-legacy-cookie"
    accounts_file.write_text(json.dumps(raw), encoding="utf-8")
    assert web._accounts_load()["accounts"][0]["cookie"] == "alice-legacy-cookie"
    migrated = json.loads(accounts_file.read_text(encoding="utf-8"))
    assert crypto.is_protected(migrated["accounts"][0]["cookie"])
    assert "alice-legacy-cookie" not in accounts_file.read_text(encoding="utf-8")


def test_account_protect_failure_does_not_write_plaintext_or_destroy_old_store(tmp_path, monkeypatch):
    accounts = tmp_path / 'accounts.json'
    accounts.write_text('{"current": null, "accounts": []}', encoding='utf-8')
    monkeypatch.setattr(web, 'ACCOUNTS_FILE', accounts)
    _fake_linux_keyring(monkeypatch)
    monkeypatch.setattr(crypto, '_linux_protect', lambda data: (_ for _ in ()).throw(OSError('keyring failed')))
    assert web._register_account({'email': 'test@example.test'}, 'secret-cookie') is None
    assert 'secret-cookie' not in accounts.read_text(encoding='utf-8')
    assert json.loads(accounts.read_text(encoding='utf-8'))['accounts'] == []


def test_unreadable_protected_cookie_not_lost_when_other_record_saved(tmp_path, monkeypatch):
    import base64
    accounts = tmp_path / 'accounts.json'
    stored = crypto.LINUX_PREFIX + base64.b64encode(b'x' * 32).decode('ascii')
    accounts.write_text(json.dumps({'current': 'a', 'accounts': [{'id': 'a', 'cookie': stored}]}), encoding='utf-8')
    monkeypatch.setattr(web, 'ACCOUNTS_FILE', accounts)
    monkeypatch.setattr(crypto, '_linux_unprotect', lambda data: (_ for _ in ()).throw(OSError('keyring locked')))
    record = web._accounts_load()['accounts'][0]
    assert record['cookie'] == ''
    assert web._accounts_save({'current': 'a', 'accounts': [record]})
    saved = json.loads(accounts.read_text(encoding='utf-8'))['accounts'][0]
    assert saved['cookie'] == stored
    assert '_protected_cookie' not in saved


def test_headless_linux_without_keyring_falls_back_to_plaintext(monkeypatch):
    monkeypatch.setattr(crypto, "LINUX_KEYRING_ENABLED", True)
    monkeypatch.setattr(crypto, "_LINUX_KEY_CACHE", None)
    monkeypatch.setattr(crypto, "_LINUX_KEYRING_CHECKED", False)
    monkeypatch.setattr(crypto, "_secret_tool_lookup", lambda: None)
    monkeypatch.setattr(crypto, "_secret_tool_store", lambda _k: False)
    assert crypto.protect_text("plain=ok") == "plain=ok"
