"""Credential and endpoint validation for the SuperToken video skill."""

import os
import re
import tempfile
import urllib.parse
from pathlib import Path


DEFAULT_BASE_URL = "https://api.supertoken.cc"
MODEL_KEY_ENV = "SUPERTOKEN_API_KEY"
RESOURCE_KEY_ENV = "SUPERTOKEN_RESOURCE_API_KEY"
CONFIG_DIR_ENV = "SUPERTOKEN_VIDEO_CONFIG_DIR"


class ConfigError(RuntimeError):
    """Raised for invalid local video-skill configuration."""


_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _invalid_base_url():
    return ConfigError("base_url must be a clean absolute HTTPS URL")


def normalize_base_url(value: str) -> str:
    """Return a canonical HTTPS API base without query, fragment, or slash suffix."""
    if not isinstance(value, str):
        raise _invalid_base_url()
    value = value.strip(" \t\r\n\v\f")
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise _invalid_base_url()
    if "\\" in value:
        raise _invalid_base_url()
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise _invalid_base_url() from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _invalid_base_url()
    if host.endswith(".") or len(host) > 253:
        raise _invalid_base_url()
    try:
        display_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise _invalid_base_url() from exc
    if ":" not in display_host and any(
        not _HOST_LABEL.fullmatch(label) for label in display_host.split(".")
    ):
        raise _invalid_base_url()
    path = parsed.path.rstrip("/")
    if (
        "//" in path
        or re.search(r"%(?:2f|5c)", path, re.IGNORECASE)
        or re.search(r"%(?![0-9A-Fa-f]{2})", path)
    ):
        raise _invalid_base_url()
    try:
        decoded_path = urllib.parse.unquote_to_bytes(path).decode("ascii")
    except UnicodeError as exc:
        raise _invalid_base_url() from exc
    if any(part in {".", ".."} for part in decoded_path.split("/")):
        raise _invalid_base_url()
    netloc = display_host if port in (None, 443) else f"{display_host}:{port}"
    if ":" in display_host and not display_host.startswith("["):
        netloc = f"[{display_host}]" if port in (None, 443) else f"[{display_host}]:{port}"
    return urllib.parse.urlunsplit(("https", netloc, path, "", ""))


def _config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "supertoken" / "video-generation"


def _credentials_path() -> Path:
    return _config_dir() / "credentials"


def _stored_key(environment_name: str) -> str | None:
    path = _credentials_path()
    try:
        values = dict(
            line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError("stored credentials could not be read") from exc
    return values.get(environment_name)


def _get_key(explicit: str | None, environment_name: str, allowed_prefixes, key_label: str) -> str:
    value = explicit if explicit is not None else os.environ.get(environment_name) or _stored_key(environment_name)
    if not isinstance(value, str):
        raise ConfigError(f"{environment_name} must be set to a non-empty API key")
    value = value.strip()
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ConfigError(f"{environment_name} must be a non-empty API key without whitespace")
    if "://" in value:
        raise ConfigError(f"{environment_name} must be an API key, not a URL")
    known_prefixes = ("sk_", "sk-", "ak_", "ak-", "wk_", "wk-")
    if value.startswith(known_prefixes) and not value.startswith(allowed_prefixes):
        raise ConfigError(f"{environment_name} must be a {key_label} key")
    return value


def get_model_key(explicit: str | None = None) -> str:
    """Get a model/create API key, rejecting resource and webhook key shapes."""
    return _get_key(explicit, MODEL_KEY_ENV, ("sk_", "sk-"), "model")


def get_resource_key(explicit: str | None = None) -> str:
    """Get a resource API key, rejecting model and webhook key shapes."""
    return _get_key(explicit, RESOURCE_KEY_ENV, ("ak_", "ak-"), "resource")


def save_key(value: str, environment_name: str) -> None:
    """Persist one validated credential in a user-only local configuration file."""
    if environment_name == MODEL_KEY_ENV:
        value = get_model_key(value)
    elif environment_name == RESOURCE_KEY_ENV:
        value = get_resource_key(value)
    else:
        raise ConfigError("unsupported credential type")

    path = _credentials_path()
    current = {}
    for name in (MODEL_KEY_ENV, RESOURCE_KEY_ENV):
        stored = _stored_key(name)
        if stored is not None:
            current[name] = stored
    current[environment_name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for name in (MODEL_KEY_ENV, RESOURCE_KEY_ENV):
                if name in current:
                    handle.write(f"{name}={current[name]}\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
