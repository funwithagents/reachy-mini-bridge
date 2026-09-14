"""Functional tests for the daemon lifecycle (specs/daemon.md) — no daemon, no mujoco.

`managed_daemon` resolves its process and probe seams (`_spawn`, `_ready`, `_sleep`,
`_port_open`) at call time, so these tests script them: a `_FakeProc` stands in for the
child process and a scripted readiness sequence drives the own-or-borrow decisions, the
readiness loop, the error paths, and the teardown.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator

import pytest

from reachy_mini_bridge import daemon
from reachy_mini_bridge.config import DaemonConfig
from reachy_mini_bridge.errors import DaemonError

_AUTO = DaemonConfig(spawn="auto")


class _FakeProc:
    def __init__(self, polls: list[int | None] | None = None, hang: bool = False):
        self.pid = 4242
        self._polls = iter(polls or [])
        self.hang = hang
        self.calls: list[str] = []

    def poll(self) -> int | None:
        return next(self._polls, None)

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return 0


class _Harness:
    """Installs scripted seams; records spawns and sleeps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.spawned: list[tuple[list[str], dict[str, str]]] = []
        self.sleeps: list[float] = []
        self.proc = _FakeProc()
        self.ready: Iterator[bool] = iter([])
        self.port_open = False
        monkeypatch.setattr(daemon, "_spawn", self._spawn)
        monkeypatch.setattr(
            daemon, "_ready", lambda host, port: next(self.ready, False)
        )
        monkeypatch.setattr(daemon, "_sleep", self.sleeps.append)
        monkeypatch.setattr(daemon, "_port_open", lambda host, port: self.port_open)
        monkeypatch.setattr(
            daemon.shutil, "which", lambda name: f"/bin/{name}"
        )  # launchers present

    def _spawn(self, cmd: list[str], env: dict[str, str]) -> _FakeProc:
        self.spawned.append((cmd, env))
        return self.proc


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


# --- pure pieces -------------------------------------------------------------------


def test_launch_command_headless_and_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon.shutil, "which", lambda name: f"/bin/{name}")
    assert daemon.launch_command(DaemonConfig()) == [
        "/bin/reachy-mini-daemon",
        "--sim",
        "--headless",
        "--no-preload-datasets",
    ]
    assert daemon.launch_command(DaemonConfig(preload_datasets=True)) == [
        "/bin/reachy-mini-daemon",
        "--sim",
        "--headless",
    ]
    assert daemon.launch_command(DaemonConfig(scene="minimal")) == [
        "/bin/reachy-mini-daemon",
        "--sim",
        "--headless",
        "--no-preload-datasets",
        "--scene",
        "minimal",
    ]
    assert daemon.launch_command(DaemonConfig(headless=False, scene="minimal")) == [
        "/bin/mjpython",
        "-m",
        "reachy_mini.daemon.app.main",
        "--sim",
        "--no-preload-datasets",
        "--scene",
        "minimal",
    ]


def test_launch_command_requires_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon.shutil, "which", lambda name: None)
    with pytest.raises(DaemonError, match=r"reachy-mini-bridge\[sim\]"):
        daemon.launch_command(DaemonConfig())
    with pytest.raises(DaemonError, match="mjpython"):
        daemon.launch_command(DaemonConfig(headless=False))


def test_scrubbed_env_drops_the_gstreamer_bundle_vars() -> None:
    base = {name: "x:x" for name in daemon._GST_BUNDLE_ENV}
    base["PATH"] = "/usr/bin"
    base["HOME"] = "/home/me"
    env = daemon.scrubbed_env(base)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/me"}
    assert base["GST_REGISTRY_1_0"] == "x:x"  # input untouched


def test_scrubbed_env_defaults_to_the_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GST_PLUGIN_PATH_1_0", "doubled:doubled")
    monkeypatch.setenv("RMB_KEEP_ME", "1")
    env = daemon.scrubbed_env()
    assert "GST_PLUGIN_PATH_1_0" not in env
    assert env["RMB_KEEP_ME"] == "1"


# --- own it or borrow it -----------------------------------------------------------


def test_auto_borrows_a_ready_daemon(harness: _Harness) -> None:
    harness.port_open = True
    harness.ready = iter([True])
    with daemon.managed_daemon(_AUTO, host="127.0.0.1", port=8000) as handle:
        assert handle == daemon.DaemonHandle("127.0.0.1", 8000, owned=False, pid=None)
    assert harness.spawned == []
    assert harness.proc.calls == []  # nothing to stop


def test_auto_waits_for_a_booting_daemon(harness: _Harness) -> None:
    harness.port_open = True
    harness.ready = iter([False, False, True])
    with daemon.managed_daemon(_AUTO) as handle:
        assert handle.owned is False
    assert harness.spawned == []
    assert harness.sleeps == [daemon._POLL_INTERVAL_S] * 2


def test_auto_spawns_when_the_port_is_free(harness: _Harness) -> None:
    harness.ready = iter([False, True])
    with daemon.managed_daemon(DaemonConfig(spawn="auto", scene="minimal")) as handle:
        assert handle == daemon.DaemonHandle("127.0.0.1", 8000, owned=True, pid=4242)
        assert harness.proc.calls == []  # still running inside the block
    (cmd, env), *_ = harness.spawned
    assert cmd[0].endswith("reachy-mini-daemon") and "--scene" in cmd
    assert not any(k in env for k in daemon._GST_BUNDLE_ENV)
    assert harness.proc.calls == ["terminate", "wait"]


def test_always_spawns_on_a_free_port_and_errors_on_a_busy_one(
    harness: _Harness,
) -> None:
    harness.ready = iter([True])
    with daemon.managed_daemon(DaemonConfig(spawn="always")) as handle:
        assert handle.owned is True
    assert len(harness.spawned) == 1

    harness.port_open = True
    with (
        pytest.raises(DaemonError, match="already in use"),
        daemon.managed_daemon(DaemonConfig(spawn="always")),
    ):
        pass
    assert len(harness.spawned) == 1  # no second spawn


def test_never_is_not_a_managed_mode(harness: _Harness) -> None:
    with (
        pytest.raises(ValueError, match="never"),
        daemon.managed_daemon(DaemonConfig()),
    ):
        pass


# --- failure paths -----------------------------------------------------------------


def test_spawn_exit_during_startup_raises_with_exit_code(harness: _Harness) -> None:
    harness.proc = _FakeProc(polls=[None, 139])
    harness.ready = iter([False, False])
    with (
        pytest.raises(DaemonError, match=r"exit 139.*reachy-mini-daemon"),
        daemon.managed_daemon(_AUTO),
    ):
        pass
    assert harness.proc.calls == ["terminate", "wait"]


def test_spawn_timeout_stops_the_child(harness: _Harness) -> None:
    clock = iter([0.0, 0.0, 0.5, 1.0, 1.5])
    with (
        pytest.MonkeyPatch.context() as mp,
        pytest.raises(DaemonError, match="did not become ready"),
    ):
        mp.setattr(daemon.time, "monotonic", lambda: next(clock))
        with daemon.managed_daemon(DaemonConfig(spawn="auto", startup_timeout=1.0)):
            pass
    assert harness.proc.calls == ["terminate", "wait"]


def test_viewer_timeout_mentions_the_gui_session(harness: _Harness) -> None:
    clock = iter([0.0, 5.0])
    with (
        pytest.MonkeyPatch.context() as mp,
        pytest.raises(DaemonError, match="GUI session"),
    ):
        mp.setattr(daemon.time, "monotonic", lambda: next(clock))
        with daemon.managed_daemon(
            DaemonConfig(spawn="auto", headless=False, startup_timeout=1.0)
        ):
            pass


def test_booting_daemon_timeout_names_the_port(harness: _Harness) -> None:
    harness.port_open = True
    clock = iter([0.0, 5.0])
    with (
        pytest.MonkeyPatch.context() as mp,
        pytest.raises(DaemonError, match="port 8000"),
    ):
        mp.setattr(daemon.time, "monotonic", lambda: next(clock))
        with daemon.managed_daemon(DaemonConfig(spawn="auto", startup_timeout=1.0)):
            pass
    assert harness.spawned == []


def test_teardown_kills_after_terminate_timeout(harness: _Harness) -> None:
    harness.proc = _FakeProc(hang=True)
    harness.ready = iter([True])
    with daemon.managed_daemon(_AUTO):
        pass
    assert harness.proc.calls == ["terminate", "wait", "kill", "wait"]


def test_teardown_runs_when_the_body_raises(harness: _Harness) -> None:
    harness.ready = iter([True])
    with pytest.raises(RuntimeError, match="body"), daemon.managed_daemon(_AUTO):
        raise RuntimeError("body")
    assert harness.proc.calls == ["terminate", "wait"]
