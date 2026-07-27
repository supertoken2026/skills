#!/usr/bin/env python3
import base64
import binascii
import ctypes
import ctypes.wintypes
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


CONFIG_VERSION = 2
APP_NAME = "gpt-image-2"
BRAND = "supertoken"
SERVICE_NAME = "supertoken-gpt-image-2"
DEFAULT_BASE_URL = "https://api.supertoken.cc"
LEGACY_BASE_URL = "https://api.supertoken.cc/image-wrapper/v1"
DEFAULT_MODEL = "gpt-image-2-count"
MODEL_KEY = "model"
RESOURCE_KEY = "resource"
API_KEY_ENV = "SUPERTOKEN_API_KEY"
RESOURCE_API_KEY_ENV = "SUPERTOKEN_RESOURCE_API_KEY"
CONFIG_DIR_ENV = "SUPERTOKEN_GPT_IMAGE_2_CONFIG_DIR"
DISABLE_SECURE_STORE_ENV = "SUPERTOKEN_GPT_IMAGE_2_DISABLE_SECURE_STORE"


@dataclass(frozen=True)
class CredentialSpec:
    kind: str
    env_name: str
    account_name: str
    plaintext_field: str
    dpapi_filename: str
    label: str


CREDENTIALS = {
    MODEL_KEY: CredentialSpec(
        MODEL_KEY, API_KEY_ENV, "default", "api_key", "credentials.dpapi",
        "SuperToken GPT Image 2 API Key",
    ),
    RESOURCE_KEY: CredentialSpec(
        RESOURCE_KEY, RESOURCE_API_KEY_ENV, "resource", "resource_api_key",
        "resource-credentials.dpapi", "SuperToken Resource API Key",
    ),
}


class ConfigError(RuntimeError):
    pass


def _read_json_object(path, label):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ConfigError(f"{label}格式无效：{path}。请删除该文件后重新配置。") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取{label}：{path}。") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label}格式无效：{path}。请删除该文件后重新配置。") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{label}格式无效：{path}。请删除该文件后重新配置。")
    return value


def config_dir():
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()

    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / BRAND / APP_NAME
    if system == "Windows":
        root = os.environ.get("APPDATA")
        if root:
            return Path(root) / BRAND / APP_NAME
        return Path.home() / "AppData" / "Roaming" / BRAND / APP_NAME

    root = os.environ.get("XDG_CONFIG_HOME")
    if root:
        return Path(root) / BRAND / APP_NAME
    return Path.home() / ".config" / BRAND / APP_NAME


def config_path():
    return config_dir() / "config.json"


def plaintext_credentials_path():
    return config_dir() / "credentials.json"


def _atomic_write_text(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    part = Path(f"{path}.part")
    part.unlink(missing_ok=True)
    try:
        part.write_text(text, encoding="utf-8")
        if mode is not None and os.name == "posix":
            part.chmod(mode)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def build_config(base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL):
    if not isinstance(base_url, str) or not base_url.strip():
        raise ConfigError("配置文件中的 base_url 必须是非空字符串。")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("配置文件中的 model 必须是非空字符串。")
    return {
        "version": CONFIG_VERSION,
        "base_url": base_url.rstrip("/"),
        "model": model,
    }


def save_config(value):
    _atomic_write_text(
        config_path(), json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    )


def load_config():
    path = config_path()
    if not path.exists():
        return {}
    value = _read_json_object(path, "配置文件")
    if value.get("version") == CONFIG_VERSION:
        return build_config(value.get("base_url"), value.get("model"))
    base_url = value.get("base_url") or DEFAULT_BASE_URL
    if isinstance(base_url, str) and base_url.rstrip("/") == LEGACY_BASE_URL:
        base_url = DEFAULT_BASE_URL
    migrated = build_config(base_url, value.get("model") or DEFAULT_MODEL)
    save_config(migrated)
    return migrated


def _macos_read_key(spec):
    if not shutil.which("security"):
        return None
    result = subprocess.run(
        ["security", "find-generic-password", "-a", spec.account_name,
         "-s", SERVICE_NAME, "-w"],
        text=True, capture_output=True, check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _macos_write_key(spec, value):
    if not shutil.which("security"):
        return False
    result = subprocess.run(
        ["security", "add-generic-password", "-a", spec.account_name,
         "-s", SERVICE_NAME, "-w", value, "-U"],
        text=True, capture_output=True, check=False,
    )
    return result.returncode == 0


def _linux_read_key(spec):
    if not shutil.which("secret-tool"):
        return None
    result = subprocess.run(
        ["secret-tool", "lookup", "service", SERVICE_NAME,
         "account", spec.account_name],
        text=True, capture_output=True, check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _linux_write_key(spec, value):
    if not shutil.which("secret-tool"):
        return False
    result = subprocess.run(
        ["secret-tool", "store", "--label", spec.label, "service", SERVICE_NAME,
         "account", spec.account_name],
        input=value, text=True, capture_output=True, check=False,
    )
    return result.returncode == 0


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _windows_protect(data):
    if platform.system() != "Windows":
        return None
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _windows_unprotect(data):
    if platform.system() != "Windows":
        return None
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def windows_dpapi_path(spec):
    return config_dir() / spec.dpapi_filename


def _windows_read_key(spec):
    path = windows_dpapi_path(spec)
    if not path.exists():
        return None
    try:
        encrypted = base64.b64decode(path.read_bytes(), validate=True)
        plain = _windows_unprotect(encrypted)
        return plain.decode("utf-8") if plain else None
    except OSError as exc:
        raise ConfigError(f"无法读取或解密 DPAPI 凭据文件：{path}。") from exc
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise ConfigError(f"DPAPI 凭据文件格式无效：{path}。请删除后重新配置。") from exc


def _windows_write_key(spec, value):
    encrypted = _windows_protect(value.encode("utf-8"))
    if not encrypted:
        return False
    path = windows_dpapi_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = Path(f"{path}.part")
    part.unlink(missing_ok=True)
    try:
        part.write_bytes(base64.b64encode(encrypted))
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return True


def _plaintext_read_key(spec):
    path = plaintext_credentials_path()
    if not path.exists():
        return None
    return _read_json_object(path, "凭据文件").get(spec.plaintext_field)


def _plaintext_write_key(spec, value):
    path = plaintext_credentials_path()
    stored = _read_json_object(path, "凭据文件") if path.exists() else {}
    stored[spec.plaintext_field] = value
    _atomic_write_text(path, json.dumps(stored, indent=2) + "\n", mode=0o600)


def _credential_spec(kind):
    try:
        return CREDENTIALS[kind]
    except KeyError as exc:
        raise ConfigError(f"未知的凭据类型：{kind}。") from exc


def _validate_key_kind(value, spec):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{spec.env_name} 必须是非空字符串。")
    expected_labels = {
        MODEL_KEY: "模型 API Token（sk-...）",
        RESOURCE_KEY: "资源 API Key（ak_...）",
    }
    mismatches = {
        MODEL_KEY: (("ak_", "资源 API Key"), ("wk-", "Webhook Key")),
        RESOURCE_KEY: (("sk-", "模型 API Token"), ("wk-", "Webhook Key")),
    }
    for prefix, actual_label in mismatches[spec.kind]:
        if value.startswith(prefix):
            raise ConfigError(
                f"{spec.env_name} 需要 {expected_labels[spec.kind]}，"
                f"当前值看起来是 {actual_label}。"
            )
    return value


def get_api_key(kind=MODEL_KEY):
    spec = _credential_spec(kind)
    env_key = os.environ.get(spec.env_name)
    if env_key:
        return _validate_key_kind(env_key, spec)
    system = platform.system()
    if system == "Darwin":
        value = _macos_read_key(spec)
    elif system == "Windows":
        value = _windows_read_key(spec)
    else:
        value = _linux_read_key(spec)
    value = value or _plaintext_read_key(spec)
    return _validate_key_kind(value, spec) if value else None


def save_api_key(api_key, allow_plaintext=False, kind=MODEL_KEY):
    spec = _credential_spec(kind)
    if not api_key:
        raise ConfigError(f"需要 {spec.env_name} 对应的 Key。")
    _validate_key_kind(api_key, spec)
    system = platform.system()
    if not os.environ.get(DISABLE_SECURE_STORE_ENV):
        writers = {
            "Darwin": _macos_write_key,
            "Windows": _windows_write_key,
        }
        writer = writers.get(system, _linux_write_key)
        if writer(spec, api_key):
            return {
                "Darwin": "macos-keychain",
                "Windows": "windows-dpapi",
            }.get(system, "linux-secret-service")
    if allow_plaintext:
        _plaintext_write_key(spec, api_key)
        return "plaintext-fallback"
    raise ConfigError(
        f"未找到可用的系统安全存储。请设置 {spec.env_name}，"
        "或明确启用受权限保护的明文存储。"
    )
