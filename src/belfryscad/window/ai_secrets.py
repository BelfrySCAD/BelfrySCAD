"""API key storage for the AI chat pane, via the OS keychain (keyring
package) rather than QSettings -- these are secrets, not preferences."""
import keyring
import keyring.errors

_SERVICE = "BelfrySCAD-AI"


def get_api_key(provider: str) -> str | None:
    try:
        return keyring.get_password(_SERVICE, provider)
    except keyring.errors.KeyringError:
        return None


def set_api_key(provider: str, key: str) -> None:
    try:
        keyring.set_password(_SERVICE, provider, key)
    except keyring.errors.KeyringError:
        pass


def delete_api_key(provider: str) -> None:
    try:
        keyring.delete_password(_SERVICE, provider)
    except keyring.errors.KeyringError:
        pass
