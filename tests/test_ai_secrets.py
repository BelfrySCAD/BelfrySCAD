"""Tests for belfryscad.window.ai_secrets -- the keyring wrapper for AI
provider API keys.

Monkeypatches keyring's module-level functions rather than touching a real
OS keychain: CI runs on headless ubuntu-latest, where keyring's Linux
backend needs a D-Bus Secret Service session that isn't available, so a
real round-trip test would be flaky/CI-environment-dependent. The actual
macOS Keychain round-trip was verified manually against the real backend
during development.
"""
import keyring.errors

from belfryscad.window import ai_secrets


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, provider):
        return self.store.get((service, provider))

    def set_password(self, service, provider, key):
        self.store[(service, provider)] = key

    def delete_password(self, service, provider):
        if (service, provider) not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[(service, provider)]


def test_round_trip(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(ai_secrets.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(ai_secrets.keyring, "set_password", fake.set_password)
    ai_secrets.set_api_key("openai", "sk-test")
    assert ai_secrets.get_api_key("openai") == "sk-test"


def test_get_missing_key_returns_none(monkeypatch):
    monkeypatch.setattr(ai_secrets.keyring, "get_password", lambda s, p: None)
    assert ai_secrets.get_api_key("openai") is None


def test_get_swallows_keyring_error(monkeypatch):
    def raise_error(s, p):
        raise keyring.errors.NoKeyringError("no backend")
    monkeypatch.setattr(ai_secrets.keyring, "get_password", raise_error)
    assert ai_secrets.get_api_key("openai") is None


def test_set_swallows_keyring_error(monkeypatch):
    def raise_error(s, p, k):
        raise keyring.errors.PasswordSetError("can't set")
    monkeypatch.setattr(ai_secrets.keyring, "set_password", raise_error)
    ai_secrets.set_api_key("openai", "sk-test")  # must not raise


def test_delete_swallows_missing_key(monkeypatch):
    def raise_error(s, p):
        raise keyring.errors.PasswordDeleteError("not found")
    monkeypatch.setattr(ai_secrets.keyring, "delete_password", raise_error)
    ai_secrets.delete_api_key("openai")  # must not raise


def test_providers_are_independent(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(ai_secrets.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(ai_secrets.keyring, "set_password", fake.set_password)
    ai_secrets.set_api_key("openai", "sk-openai")
    ai_secrets.set_api_key("anthropic", "sk-anthropic")
    assert ai_secrets.get_api_key("openai") == "sk-openai"
    assert ai_secrets.get_api_key("anthropic") == "sk-anthropic"
