"""Shared helpers for the off-server backup tools. Standard library only (they run under the system python3 from cron).

Nothing here prints or logs a secret. Configuration files are read by a strict parser (never sourced by a shell), must not be
readable by group or others, and may contain only the names listed for them.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time

CONFIG_DIR_DEFAULT = "/etc/mba-backup"
STATE_DIR_DEFAULT = "/var/lib/mba-backup"
BACKUP_DIR_DEFAULT = "/opt/mba-bot/backups"

B2_ENV_KEYS = ("RCLONE_CONFIG_B2_TYPE", "RCLONE_CONFIG_B2_ACCOUNT", "RCLONE_CONFIG_B2_KEY", "MBA_BACKUP_BUCKET")
ALERT_ENV_KEYS = ("OPS_BOT_TOKEN", "OPS_CHAT_ID", "DEADMAN_URL")
AGE_PUBLIC_KEY = re.compile(r"^age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}$")
BUCKET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{4,62}$")
ALERT_STEPS = (
    "start", "local_backup", "integrity_check", "marker", "local_prune", "offsite", "validate", "recheck", "compress", "encrypt",
    "manifest", "upload", "verify", "state", "config",
)
SECRET_KEY_NAMES = ("RCLONE_CONFIG_B2_ACCOUNT", "RCLONE_CONFIG_B2_KEY", "OPS_BOT_TOKEN", "DEADMAN_URL")


class ConfigError(Exception):
    """A configuration problem. The message is fixed text written here, never file content, so it is safe to show."""


def config_dir() -> str:
    return os.environ.get("MBA_BACKUP_CONFIG_DIR", CONFIG_DIR_DEFAULT)


def state_dir() -> str:
    return os.environ.get("MBA_BACKUP_STATE_DIR", STATE_DIR_DEFAULT)


def backup_dir() -> str:
    return os.environ.get("MBA_BACKUP_DIR", BACKUP_DIR_DEFAULT)


def parse_env_file(path: str, allowed: tuple[str, ...], *, required: tuple[str, ...] = ()) -> dict[str, str]:
    """Reads KEY=VALUE lines. Refuses a file that is a symlink, is readable or writable by group or others, or has an unknown
    name, a duplicate, or a value containing a newline or NUL. Surrounding single or double quotes are removed."""
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ConfigError(f"a configuration file is missing or unreadable ({os.path.basename(path)})") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{os.path.basename(path)} must be a regular file")
    if info.st_mode & 0o077:
        raise ConfigError(f"{os.path.basename(path)} must not be accessible to group or others (chmod 600)")
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle.read().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            name = name.strip()
            if not sep or name not in allowed or name in values:
                raise ConfigError(f"{os.path.basename(path)} has an unexpected, repeated or malformed line")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if "\x00" in value:
                raise ConfigError(f"{os.path.basename(path)} has an invalid value")
            values[name] = value
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise ConfigError(f"{os.path.basename(path)} is missing {len(missing)} required setting(s)")
    return values


def read_recipient(path: str) -> str:
    try:
        text = open(path, encoding="utf-8").read().strip()
    except OSError as exc:
        raise ConfigError("the age recipient file is missing or unreadable") from exc
    if not AGE_PUBLIC_KEY.match(text):
        raise ConfigError("the age recipient file does not hold a public key (it must be one line starting with age1; never a private key)")
    return text


def scrub(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    out = text
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "[redacted]")
    return out


def find_binary(name: str, env_override: str) -> str:
    """cron runs with a minimal PATH, so look in the usual folders as well. An override must be an absolute path to a file."""
    override = os.environ.get(env_override)
    if override:
        if os.path.isabs(override) and os.access(override, os.X_OK):
            return override
        raise ConfigError(f"{env_override} does not point to an executable file")
    for folder in os.environ.get("PATH", "").split(os.pathsep) + ["/usr/local/bin", "/usr/bin", "/bin"]:
        candidate = os.path.join(folder, name) if folder else ""
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise ConfigError(f"the {name} program was not found")


def write_atomic(path: str, text: str, mode: int = 0o600) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_epoch(path: str) -> int | None:
    try:
        return int(open(path, encoding="utf-8").read().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_text(path: str) -> str | None:
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return None


def utc_stamp(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def log(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def json_dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True)
