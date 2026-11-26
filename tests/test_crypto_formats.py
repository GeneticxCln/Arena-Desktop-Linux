import base64
from arena import crypto


def test_cookie_formats_and_fail_closed(monkeypatch):
    store = {}
    monkeypatch.setattr(crypto, "LINUX_KEYRING_ENABLED", True)
    monkeypatch.setattr(crypto, "_LINUX_KEY_CACHE", None)
    monkeypatch.setattr(crypto, "_LINUX_KEYRING_CHECKED", False)
    monkeypatch.setattr(crypto, "_secret_tool_lookup", lambda: store.get("key"))
    monkeypatch.setattr(crypto, "_secret_tool_store", lambda k: store.__setitem__("key", k) or True)

    value = "session=abc; text=normal"
    current = crypto.encrypt(value.encode())
    assert current.startswith(b"ARENA-SECRET-v1:")
    assert crypto.decrypt(current).decode() == value
    assert crypto.decrypt(value.encode()).decode() == value
    assert crypto.decrypt(b"dGVzdA==") == b"dGVzdA=="  # plaintext is not silently changed

    # Legacy Windows DPAPI formats always fail closed on Linux.
    legacy_v1 = crypto.PREFIX + base64.b64encode(b"ciphertext").decode("ascii")
    legacy_raw = base64.b64encode(b"DPAPI:ciphertext").decode("ascii")
    assert crypto.decode_cookie(legacy_v1) == ""
    assert crypto.decode_cookie(legacy_raw) == ""

    store.clear()
    monkeypatch.setattr(crypto, "_LINUX_KEY_CACHE", None)
    assert crypto.decode_cookie(current.decode()) == ""
