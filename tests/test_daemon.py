"""
Tests for `pat observe daemon` -- launchd (macOS) / systemd (Linux) scheduling.

Platform is forced via ``monkeypatch.setattr(daemon.sys, "platform", ...)`` so
both the macOS and Linux code paths run regardless of the host OS. Subprocess
calls (``launchctl`` / ``systemctl``) are stubbed -- no real units are loaded.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency import daemon
from presidio_arch_translucency.cli import app

runner = CliRunner()


@dataclass
class _FakeProc:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "darwin")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "linux")


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


class TestPlatform:
    def test_darwin(self, mac):
        assert daemon.current_platform() == "darwin"

    def test_linux(self, linux):
        assert daemon.current_platform() == "linux"

    def test_linux2_prefix(self, monkeypatch):
        monkeypatch.setattr(daemon.sys, "platform", "linux2")
        assert daemon.current_platform() == "linux"

    def test_unsupported_raises(self, monkeypatch):
        monkeypatch.setattr(daemon.sys, "platform", "win32")
        with pytest.raises(daemon.DaemonError, match="Unsupported platform"):
            daemon.current_platform()


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


class TestCommand:
    def test_resolve_uses_pat_script(self, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/usr/local/bin/pat")
        assert daemon.resolve_pat_command() == ["/usr/local/bin/pat"]

    def test_resolve_falls_back_to_module(self, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: None)
        cmd = daemon.resolve_pat_command()
        assert cmd[0] == daemon.sys.executable
        assert cmd[1:] == ["-m", "presidio_arch_translucency"]

    def test_observe_argv_bare(self):
        assert daemon.observe_argv() == ["observe"]

    def test_observe_argv_prometheus_and_layer(self):
        argv = daemon.observe_argv(prometheus="http://p:9090", layer="pod")
        assert argv == ["observe", "--prometheus", "http://p:9090", "--layer", "pod"]

    def test_observe_argv_layer_only(self):
        assert daemon.observe_argv(layer="node") == ["observe", "--layer", "node"]

    def test_validate_normalizes_layer(self):
        prometheus, layer = daemon.validate_schedule_inputs("https://p:9090", "Pod")
        assert prometheus == "https://p:9090"
        assert layer == "pod"

    @pytest.mark.parametrize("url", ["file:///tmp/x", "prom:9090", ""])
    def test_validate_rejects_invalid_prometheus_url(self, url):
        with pytest.raises(daemon.DaemonError, match="prometheus"):
            daemon.validate_schedule_inputs(url, "pod")

    @pytest.mark.parametrize("layer", ["", "api", "pod\nEnvironment=X"])
    def test_validate_rejects_invalid_layer(self, layer):
        with pytest.raises(daemon.DaemonError, match="layer"):
            daemon.validate_schedule_inputs("https://p:9090", layer)


# ---------------------------------------------------------------------------
# Unit-file rendering (pure)
# ---------------------------------------------------------------------------


class TestRenderLaunchd:
    def test_structure(self):
        plist = daemon.render_launchd_plist(["/bin/pat", "observe"], 45)
        assert plist.startswith('<?xml version="1.0"')
        assert "<key>Label</key>" in plist
        assert f"<string>{daemon.LAUNCHD_LABEL}</string>" in plist
        assert "<key>StartInterval</key>" in plist
        assert "<integer>45</integer>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<string>/bin/pat</string>" in plist
        assert "<string>observe</string>" in plist

    def test_xml_escaping(self):
        plist = daemon.render_launchd_plist(
            ["/bin/pat", "observe", "--prometheus", "http://h?a=1&b=2<x>"], 60
        )
        assert "&amp;" in plist
        assert "&lt;x&gt;" in plist
        assert "<x>" not in plist.split("ProgramArguments")[1]

    def test_rejects_control_chars(self):
        with pytest.raises(daemon.DaemonError, match="control"):
            daemon.render_launchd_plist(["/bin/pat", "observe\nmalicious"], 60)


class TestRenderSystemd:
    def test_service(self):
        svc = daemon.render_systemd_service(["/bin/pat", "observe", "--layer", "pod"])
        assert "Type=oneshot" in svc
        assert "ExecStart=/bin/pat observe --layer pod" in svc

    def test_service_quotes_spaces_and_escapes_percent(self):
        svc = daemon.render_systemd_service(
            [
                "/bin/pat",
                "observe",
                "--prometheus",
                "https://prom.example/query path?x=100%25",
            ]
        )
        assert '"https://prom.example/query path?x=100%%25"' in svc

    def test_service_rejects_control_chars(self):
        with pytest.raises(daemon.DaemonError, match="control"):
            daemon.render_systemd_service(["/bin/pat", "observe\nEnvironment=X"])

    def test_timer(self):
        timer = daemon.render_systemd_timer(90)
        assert "OnCalendar=*:*:00/90" in timer
        assert f"Unit={daemon.SYSTEMD_UNIT}.service" in timer
        assert "WantedBy=timers.target" in timer


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


class TestInstall:
    def test_macos_writes_plist(self, mac, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        res = daemon.install(
            prometheus="http://p:9090", layer="pod", interval=30, home=tmp_path
        )
        assert res.platform == "darwin"
        path = daemon.launchd_plist_path(tmp_path)
        assert res.paths == [path]
        assert path.exists()
        content = path.read_text()
        assert "<integer>30</integer>" in content
        assert "<string>http://p:9090</string>" in content
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "bootstrap" in res.reload_hint

    def test_linux_writes_service_and_timer(self, linux, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        res = daemon.install(interval=120, home=tmp_path)
        assert res.platform == "linux"
        svc = daemon.systemd_service_path(tmp_path)
        timer = daemon.systemd_timer_path(tmp_path)
        assert set(res.paths) == {svc, timer}
        assert svc.exists() and timer.exists()
        assert "ExecStart=/opt/pat observe" in svc.read_text()
        assert "OnCalendar=*:*:00/120" in timer.read_text()
        assert stat.S_IMODE(svc.stat().st_mode) == 0o600
        assert stat.S_IMODE(timer.stat().st_mode) == 0o600
        assert "daemon-reload" in res.reload_hint
        assert "enable --now" in res.reload_hint

    def test_linux_normalizes_layer(self, linux, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        daemon.install(prometheus="https://p:9090", layer="Pod", home=tmp_path)
        svc = daemon.systemd_service_path(tmp_path).read_text()
        assert "--layer pod" in svc

    def test_rejects_control_chars_before_write(self, linux, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        with pytest.raises(daemon.DaemonError, match="control"):
            daemon.install(
                prometheus="https://p:9090\nEnvironment=X=Y",
                layer="pod",
                home=tmp_path,
            )
        assert not daemon.systemd_service_path(tmp_path).exists()

    def test_rejects_invalid_layer_before_write(self, linux, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        with pytest.raises(daemon.DaemonError, match="layer"):
            daemon.install(prometheus="https://p:9090", layer="api", home=tmp_path)
        assert not daemon.systemd_service_path(tmp_path).exists()

    def test_unsupported_platform_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon.sys, "platform", "win32")
        with pytest.raises(daemon.DaemonError):
            daemon.install(home=tmp_path)


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_macos_removes_and_boots_out(self, mac, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            daemon.subprocess,
            "run",
            lambda *a, **k: calls.append(a[0]) or _FakeProc(),
        )
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        daemon.install(interval=60, home=tmp_path)
        removed = daemon.uninstall(home=tmp_path)
        assert removed == [daemon.launchd_plist_path(tmp_path)]
        assert not daemon.launchd_plist_path(tmp_path).exists()
        assert any("bootout" in c for c in calls)

    def test_macos_missing_file_is_noop(self, mac, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: _FakeProc())
        assert daemon.uninstall(home=tmp_path) == []

    def test_macos_bootout_error_swallowed(self, mac, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("launchctl missing")

        monkeypatch.setattr(daemon.subprocess, "run", boom)
        assert daemon.uninstall(home=tmp_path) == []

    def test_linux_removes_both(self, linux, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        daemon.install(interval=60, home=tmp_path)
        removed = daemon.uninstall(home=tmp_path)
        assert set(removed) == {
            daemon.systemd_service_path(tmp_path),
            daemon.systemd_timer_path(tmp_path),
        }

    def test_linux_missing_is_noop(self, linux, tmp_path):
        assert daemon.uninstall(home=tmp_path) == []


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_macos_not_installed(self, mac, tmp_path):
        res = daemon.status(home=tmp_path)
        assert res.installed is False
        assert res.loaded is False
        assert res.detail == "not installed"

    def test_macos_loaded(self, mac, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        daemon.install(interval=60, home=tmp_path)
        monkeypatch.setattr(
            daemon.subprocess,
            "run",
            lambda *a, **k: _FakeProc(stdout=f"123\t0\t{daemon.LAUNCHD_LABEL}\n"),
        )
        res = daemon.status(home=tmp_path)
        assert res.installed and res.loaded
        assert res.detail == "loaded"

    def test_macos_installed_not_loaded(self, mac, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        daemon.install(interval=60, home=tmp_path)
        monkeypatch.setattr(
            daemon.subprocess, "run", lambda *a, **k: _FakeProc(stdout="other\n")
        )
        res = daemon.status(home=tmp_path)
        assert res.installed and not res.loaded
        assert "not loaded" in res.detail

    def test_linux_not_installed(self, linux, tmp_path):
        res = daemon.status(home=tmp_path)
        assert res.installed is False

    def test_linux_active(self, linux, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        daemon.install(interval=60, home=tmp_path)
        monkeypatch.setattr(
            daemon.subprocess, "run", lambda *a, **k: _FakeProc(stdout="active\n")
        )
        res = daemon.status(home=tmp_path)
        assert res.loaded is True
        assert res.detail == "active"

    def test_linux_inactive(self, linux, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        daemon.install(interval=60, home=tmp_path)
        monkeypatch.setattr(
            daemon.subprocess, "run", lambda *a, **k: _FakeProc(stdout="inactive\n")
        )
        res = daemon.status(home=tmp_path)
        assert res.loaded is False
        assert "inactive" in res.detail

    def test_linux_empty_stdout_unknown(self, linux, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon.shutil, "which", lambda _: "/opt/pat")
        daemon.install(interval=60, home=tmp_path)
        monkeypatch.setattr(
            daemon.subprocess, "run", lambda *a, **k: _FakeProc(stdout="")
        )
        res = daemon.status(home=tmp_path)
        assert res.loaded is False
        assert "unknown" in res.detail


# ---------------------------------------------------------------------------
# CLI integration: pat observe daemon {install,uninstall,status}
# ---------------------------------------------------------------------------


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "observe", "daemon", *args])


class TestDaemonCLI:
    def test_install_success(self, monkeypatch, tmp_path):
        plist = tmp_path / "eu.presidio-group.pat.observe.plist"
        result = daemon.InstallResult(
            platform="darwin",
            paths=[plist],
            reload_hint="launchctl bootstrap ...",
        )
        monkeypatch.setattr(daemon, "install", lambda **k: result)
        out = _invoke("install", "--interval", "30")
        assert out.exit_code == 0
        assert "Installed" in out.output
        assert "every 30s" in out.output
        collapsed = "".join(out.output.split())
        assert plist.name in collapsed
        assert "bootstrap" in out.output

    def test_install_unsupported_platform_exit_2(self, monkeypatch):
        def boom(**k):
            raise daemon.DaemonError("nope")

        monkeypatch.setattr(daemon, "install", boom)
        out = _invoke("install")
        assert out.exit_code == 2
        assert "Cannot install daemon" in out.output

    def test_uninstall_removed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "uninstall", lambda: [tmp_path / "x.plist"])
        out = _invoke("uninstall")
        assert out.exit_code == 0
        assert "Removed" in out.output

    def test_uninstall_nothing(self, monkeypatch):
        monkeypatch.setattr(daemon, "uninstall", lambda: [])
        out = _invoke("uninstall")
        assert out.exit_code == 0
        assert "nothing to remove" in out.output

    def test_uninstall_error_exit_2(self, monkeypatch):
        def boom():
            raise daemon.DaemonError("nope")

        monkeypatch.setattr(daemon, "uninstall", boom)
        out = _invoke("uninstall")
        assert out.exit_code == 2

    def test_status_not_installed(self, monkeypatch):
        monkeypatch.setattr(
            daemon,
            "status",
            lambda: daemon.StatusResult("linux", False, False, "not installed"),
        )
        out = _invoke("status")
        assert out.exit_code == 0
        assert "not installed" in out.output

    def test_status_loaded(self, monkeypatch):
        monkeypatch.setattr(
            daemon,
            "status",
            lambda: daemon.StatusResult("darwin", True, True, "loaded"),
        )
        out = _invoke("status")
        assert out.exit_code == 0
        assert "loaded" in out.output

    def test_status_error_exit_2(self, monkeypatch):
        def boom():
            raise daemon.DaemonError("nope")

        monkeypatch.setattr(daemon, "status", boom)
        out = _invoke("status")
        assert out.exit_code == 2

    def test_bare_observe_still_records(self, tmp_path):
        """The group refactor must not break plain `pat observe` recording."""
        db = tmp_path / "obs.db"
        rec = runner.invoke(
            app,
            [
                "--skip-audit",
                "observe",
                "--layer",
                "container",
                "--rps",
                "500",
                "--avg-latency-ms",
                "80",
                "--p99-latency-ms",
                "140",
                "--throughput",
                "480",
                "--replicas",
                "6",
                "--db",
                str(db),
            ],
        )
        assert rec.exit_code == 0
        assert "Recorded" in rec.output
