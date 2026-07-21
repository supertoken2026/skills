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
from pathlib import Path


APP_NAME = "gpt-image-2"
BRAND = "supertoken"
DEFAULT_BASE_URL = "https://api.supertoken.cc/image-wrapper/v1"
DEFAULT_MODEL = "gpt-image-2-count"
SERVICE_NAME = "supertoken-gpt-image-2"
ACCOUNT_NAME = "default"
API_KEY_ENV = "SUPERTOKEN_API_KEY"
CONFIG_DIR_ENV = "SUPERTOKEN_GPT_IMAGE_2_CONFIG_DIR"
DISABLE_SECURE_STORE_ENV = "SUPERTOKEN_GPT_IMAGE_2_DISABLE_SECURE_STORE"


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


def windows_dpapi_path():
    return config_dir() / "credentials.dpapi"


def load_config():
    path = config_path()
    if not path.exists():
        return {}
    return _read_json_object(path, "配置文件")


def save_config(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_config(base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL):
    return {
        "base_url": base_url.rstrip("/"),
        "model": model,
    }


def get_api_key():
    env_key = os.environ.get(API_KEY_ENV)
    if env_key:
        return env_key

    system = platform.system()
    if system == "Darwin":
        value = _macos_read_key()
        if value:
            return value
    elif system == "Windows":
        value = _windows_read_key()
        if value:
            return value
    else:
        value = _linux_read_key()
        if value:
            return value

    return _plaintext_read_key()


def save_api_key(api_key, allow_plaintext=False):
    if not api_key:
        raise ConfigError("需要 SuperToken API Key。")

    system = platform.system()
    if not os.environ.get(DISABLE_SECURE_STORE_ENV):
        if system == "Darwin":
            if _macos_write_key(api_key):
                return "macos-keychain"
        elif system == "Windows":
            if _windows_write_key(api_key):
                return "windows-dpapi"
        else:
            if _linux_write_key(api_key):
                return "linux-secret-service"

    if allow_plaintext:
        _plaintext_write_key(api_key)
        return "plaintext-fallback"

    raise ConfigError(
        "未找到可用的系统安全存储。请设置 SUPERTOKEN_API_KEY，或明确启用受权限保护的明文存储。"
    )


def _macos_read_key():
    if not shutil.which("security"):
        return None
    result = subprocess.run(
        ["security", "find-generic-password", "-a", ACCOUNT_NAME, "-s", SERVICE_NAME, "-w"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _macos_write_key(api_key):
    if not shutil.which("security"):
        return False
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-a",
            ACCOUNT_NAME,
            "-s",
            SERVICE_NAME,
            "-w",
            api_key,
            "-U",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _linux_read_key():
    if not shutil.which("secret-tool"):
        return None
    result = subprocess.run(
        ["secret-tool", "lookup", "service", SERVICE_NAME, "account", ACCOUNT_NAME],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _linux_write_key(api_key):
    if not shutil.which("secret-tool"):
        return False
    result = subprocess.run(
        [
            "secret-tool",
            "store",
            "--label",
            "SuperToken GPT Image 2 API Key",
            "service",
            SERVICE_NAME,
            "account",
            ACCOUNT_NAME,
        ],
        input=api_key,
        text=True,
        capture_output=True,
        check=False,
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


def _windows_read_key():
    path = windows_dpapi_path()
    if not path.exists():
        return None
    try:
        encrypted = base64.b64decode(path.read_bytes(), validate=True)
    except OSError as exc:
        raise ConfigError(f"无法读取 DPAPI 凭据文件：{path}。") from exc
    except (binascii.Error, ValueError) as exc:
        raise ConfigError(f"DPAPI 凭据文件格式无效：{path}。请删除该文件后重新配置。") from exc
    try:
        plain = _windows_unprotect(encrypted)
    except OSError as exc:
        raise ConfigError(f"无法解密 DPAPI 凭据文件：{path}。请删除该文件后重新配置。") from exc
    if not plain:
        return None
    try:
        return plain.decode("utf-8")
    except UnicodeError as exc:
        raise ConfigError(f"DPAPI 凭据文件格式无效：{path}。请删除该文件后重新配置。") from exc


def _windows_write_key(api_key):
    encrypted = _windows_protect(api_key.encode("utf-8"))
    if not encrypted:
        return False
    path = windows_dpapi_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64encode(encrypted))
    return True


def _plaintext_read_key():
    path = plaintext_credentials_path()
    if not path.exists():
        return None
    data = _read_json_object(path, "凭据文件")
    return data.get("api_key")


def _plaintext_write_key(api_key):
    path = plaintext_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_key": api_key}, indent=2) + "\n", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
