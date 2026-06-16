"""
`pat observe daemon` -- continuous observation scheduling (v0.9.0).

Wraps the single-shot ``pat observe`` in a platform-native scheduler so users get
continuous collection without hand-writing a cron / launchd / systemd unit. This
does **not** turn ``observe`` into a long-running process -- decision D2 stands:
the scheduler fires ``pat observe`` single-shot every ``--interval`` seconds.
Daemon mode is an opt-in convenience layered on top of cron/launchd, not a
replacement for the single-shot model.

Platforms (detected via ``sys.platform``):
  - ``darwin`` -> a launchd LaunchAgent plist in ``~/Library/LaunchAgents``
  - ``linux``  -> a systemd ``--user`` ``.service`` + ``.timer`` in
    ``~/.config/systemd/user``
  - anything else -> :class:`DaemonError`

No new dependencies: only standard-library modules. The command the scheduler
runs is the resolved ``pat`` console script (or ``python -m
presidio_arch_translucency`` as a fallback), so the agent records exactly what
``pat observe`` would record by hand.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 -- used only with fixed argv lists, never shell=True
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Identifiers & well-known paths
# ---------------------------------------------------------------------------

#: launchd job label (reverse-DNS, Presidio domain). Also the plist file stem.
LAUNCHD_LABEL = "eu.presidio-group.pat.observe"

#: systemd --user unit base name (".service" / ".timer" are appended).
SYSTEMD_UNIT = "pat-observe"

#: Default scrape cadence in seconds.
DEFAULT_INTERVAL_S = 60

_VALID_LAYERS = {"container", "pod", "deployment", "node"}


class DaemonError(RuntimeError):
    """Raised when the daemon cannot be managed (unsupported platform, etc.)."""


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def current_platform() -> str:
    """Return ``'darwin'`` or ``'linux'``; raise :class:`DaemonError` otherwise."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise DaemonError(
        f"Unsupported platform {sys.platform!r}: `pat observe daemon` supports "
        "macOS (launchd) and Linux (systemd --user) only. Schedule "
        "`pat observe` with your platform's native scheduler instead."
    )


def launchd_plist_path(home: Path | None = None) -> Path:
    """Path to the launchd LaunchAgent plist (macOS)."""
    return _home(home) / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_dir(home: Path | None = None) -> Path:
    """Directory holding the systemd ``--user`` units (Linux)."""
    return _home(home) / ".config" / "systemd" / "user"


def systemd_service_path(home: Path | None = None) -> Path:
    return systemd_unit_dir(home) / f"{SYSTEMD_UNIT}.service"


def systemd_timer_path(home: Path | None = None) -> Path:
    return systemd_unit_dir(home) / f"{SYSTEMD_UNIT}.timer"


# ---------------------------------------------------------------------------
# Command construction / validation
# ---------------------------------------------------------------------------


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _reject_control_chars(value: str, field: str) -> None:
    if _has_control_chars(value):
        raise DaemonError(f"{field} must not contain control characters")


def _validate_prometheus_url(prometheus: str | None) -> str | None:
    if prometheus is None:
        return None
    value = prometheus.strip()
    if not value:
        raise DaemonError("--prometheus must not be empty")
    _reject_control_chars(value, "--prometheus")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DaemonError("--prometheus must be an http(s) URL with a host")
    return value


def _validate_layer(layer: str | None) -> str | None:
    if layer is None:
        return None
    value = layer.strip().lower()
    if not value:
        raise DaemonError("--layer must not be empty")
    _reject_control_chars(value, "--layer")
    if value not in _VALID_LAYERS:
        valid = ", ".join(sorted(_VALID_LAYERS))
        raise DaemonError(f"--layer must be one of: {valid}")
    return value


def validate_schedule_inputs(
    prometheus: str | None, layer: str | None
) -> tuple[str | None, str | None]:
    """Validate daemon inputs before they are rendered into scheduler files."""
    return _validate_prometheus_url(prometheus), _validate_layer(layer)


def resolve_pat_command() -> list[str]:
    """
    Return the argv prefix that runs the ``pat`` CLI.

    Prefers the installed ``pat`` console script (absolute path, so the unit does
    not depend on the user's ``PATH``); falls back to
    ``python -m presidio_arch_translucency`` when the script is not found.
    """
    exe = shutil.which("pat")
    if exe:
        return [exe]
    return [sys.executable, "-m", "presidio_arch_translucency"]


def observe_argv(
    *, prometheus: str | None = None, layer: str | None = None
) -> list[str]:
    """Build the ``observe`` sub-argv the scheduler should run."""
    argv = ["observe"]
    if prometheus:
        argv += ["--prometheus", prometheus]
    if layer:
        argv += ["--layer", layer]
    return argv


def _full_command(prometheus: str | None, layer: str | None) -> list[str]:
    prometheus, layer = validate_schedule_inputs(prometheus, layer)
    return resolve_pat_command() + observe_argv(prometheus=prometheus, layer=layer)


# ---------------------------------------------------------------------------
# Unit-file rendering (pure -- no filesystem access, for easy testing)
# ---------------------------------------------------------------------------


def _xml_escape(value: str) -> str:
    _reject_control_chars(value, "ProgramArguments value")
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def render_launchd_plist(command: list[str], interval: int) -> str:
    """Render a launchd LaunchAgent plist running *command* every *interval* s."""
    program_args = "\n".join(
        f"        <string>{_xml_escape(arg)}</string>" for arg in command
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{program_args}\n"
        "    </array>\n"
        "    <key>StartInterval</key>\n"
        f"    <integer>{interval}</integer>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>StandardOutPath</key>\n"
        "    <string>/tmp/pat-observe.log</string>\n"  # noqa: S108
        "    <key>StandardErrorPath</key>\n"
        "    <string>/tmp/pat-observe.err</string>\n"  # noqa: S108
        "</dict>\n"
        "</plist>\n"
    )


def _systemd_quote_arg(arg: str) -> str:
    _reject_control_chars(arg, "ExecStart argument")
    escaped = arg.replace("%", "%%")
    if escaped == "":
        return '""'
    if all(ch not in ' "\\' for ch in escaped):
        return escaped
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_systemd_service(command: list[str]) -> str:
    """Render the systemd ``oneshot`` service that runs ``pat observe`` once."""
    exec_start = " ".join(_systemd_quote_arg(arg) for arg in command)
    return (
        "[Unit]\n"
        "Description=Presidio pat observe -- single-shot workload measurement\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
    )


def render_systemd_timer(interval: int) -> str:
    """Render the systemd timer that fires the service every *interval* seconds."""
    return (
        "[Unit]\n"
        "Description=Schedule pat observe every "
        f"{interval}s\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*:*:00/{interval}\n"
        "Persistent=true\n"
        f"Unit={SYSTEMD_UNIT}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


# ---------------------------------------------------------------------------
# Install / uninstall / status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallResult:
    """Outcome of an install: the files written and a post-install reminder."""

    platform: str
    paths: list[Path]
    reload_hint: str | None


def _write_private_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def install(
    *,
    prometheus: str | None = None,
    layer: str | None = None,
    interval: int = DEFAULT_INTERVAL_S,
    home: Path | None = None,
) -> InstallResult:
    """Write the launchd plist (macOS) or systemd units (Linux)."""
    platform = current_platform()
    command = _full_command(prometheus, layer)

    if platform == "darwin":
        path = launchd_plist_path(home)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private_text(path, render_launchd_plist(command, interval))
        hint = f"launchctl bootstrap gui/$(id -u) {path}"
        return InstallResult(platform=platform, paths=[path], reload_hint=hint)

    # linux
    unit_dir = systemd_unit_dir(home)
    unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    service = systemd_service_path(home)
    timer = systemd_timer_path(home)
    _write_private_text(service, render_systemd_service(command))
    _write_private_text(timer, render_systemd_timer(interval))
    hint = (
        "systemctl --user daemon-reload && "
        f"systemctl --user enable --now {SYSTEMD_UNIT}.timer"
    )
    return InstallResult(platform=platform, paths=[service, timer], reload_hint=hint)


def uninstall(home: Path | None = None) -> list[Path]:
    """
    Remove the daemon's unit file(s). Returns the paths that existed and were
    removed. A missing file is a graceful no-op. On macOS, also attempts a
    ``launchctl bootout`` (errors swallowed -- the agent may not be loaded).
    """
    platform = current_platform()
    removed: list[Path] = []

    if platform == "darwin":
        path = launchd_plist_path(home)
        # Best-effort unload first; the agent may not be loaded (nonzero exit) and
        # launchctl may even be absent -- swallow both, removal is what matters.
        try:
            subprocess.run(  # noqa: S603 -- fixed argv, no shell
                ["launchctl", "bootout", f"gui/{_uid()}/{LAUNCHD_LABEL}"],  # noqa: S607
                capture_output=True,
                check=False,
            )
        except OSError:
            # launchctl missing or not executable -- nothing to unload; file
            # removal below is the operation that actually matters.
            pass
        if path.exists():
            path.unlink()
            removed.append(path)
        return removed

    # linux
    for path in (systemd_timer_path(home), systemd_service_path(home)):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def _uid() -> int:
    """Current user id (``os.getuid`` is POSIX-only; daemon is POSIX-only too)."""
    import os  # noqa: PLC0415 -- local import keeps module import-clean

    return os.getuid()


@dataclass(frozen=True)
class StatusResult:
    """Daemon status: whether it is installed and (if so) loaded/active."""

    platform: str
    installed: bool
    loaded: bool
    detail: str


def status(home: Path | None = None) -> StatusResult:
    """Report whether the daemon is installed and loaded/running."""
    platform = current_platform()

    if platform == "darwin":
        path = launchd_plist_path(home)
        if not path.exists():
            return StatusResult(platform, False, False, "not installed")
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["launchctl", "list"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        loaded = LAUNCHD_LABEL in (proc.stdout or "")
        detail = "loaded" if loaded else "installed but not loaded"
        return StatusResult(platform, True, loaded, detail)

    # linux
    timer = systemd_timer_path(home)
    if not timer.exists():
        return StatusResult(platform, False, False, "not installed")
    proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["systemctl", "--user", "is-active", f"{SYSTEMD_UNIT}.timer"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    state = (proc.stdout or "").strip() or "unknown"
    loaded = state == "active"
    detail = "active" if loaded else f"installed but not active ({state})"
    return StatusResult(platform, True, loaded, detail)
