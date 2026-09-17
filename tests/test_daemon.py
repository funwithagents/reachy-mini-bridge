"""Functional tests for the daemon lifecycle (specs/daemon.md) — no daemon, no mujoco.

`managed_daemon` resolves its process and probe seams (`_spawn`, `_ready`, `_sleep`,
`_port_open`, `_placo_available`) at call time, so these tests script them: a `_FakeProc` stands in for the
child process and a scripted readiness sequence drives the own-or-borrow decisions, the
readiness loop, the error paths, and the teardown.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from reachy_mini_bridge import daemon
from reachy_mini_bridge.config import DaemonConfig, SimCameraSettings
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
        monkeypatch.setattr(daemon, "_placo_available", lambda: False)

    def _spawn(self, cmd: list[str], env: dict[str, str]) -> _FakeProc:
        self.spawned.append((cmd, env))
        return self.proc


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


# --- pure pieces -------------------------------------------------------------------


def test_launch_command_headless_and_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every sim daemon runs the bridge's sim daemon launcher (specs/sim_daemon.md) —
    this interpreter headless, mjpython for the viewer."""
    monkeypatch.setattr(daemon.shutil, "which", lambda name: f"/bin/{name}")
    launcher = [sys.executable, "-m", "reachy_mini_bridge.sim_daemon"]
    # Preloading is the default and always explicit: the daemon's own default is off.
    assert daemon.launch_command(DaemonConfig()) == [
        *launcher,
        "--headless",
        "--preload-datasets",
    ]
    assert daemon.launch_command(DaemonConfig(preload_datasets=False)) == [
        *launcher,
        "--headless",
        "--no-preload-datasets",
    ]
    assert daemon.launch_command(DaemonConfig(scene="minimal")) == [
        *launcher,
        "--scene",
        "minimal",
        "--headless",
        "--preload-datasets",
    ]
    assert daemon.launch_command(DaemonConfig(headless=False, scene="minimal")) == [
        "/bin/mjpython",
        "-m",
        "reachy_mini_bridge.sim_daemon",
        "--scene",
        "minimal",
        "--preload-datasets",
    ]


def test_launch_command_passes_the_webcam_camera_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`daemon.camera` becomes the launcher's camera flags — none for the rendered eye
    camera, on the plain and the scene-file recipes alike."""
    monkeypatch.setattr(daemon.shutil, "which", lambda name: f"/bin/{name}")
    webcam = SimCameraSettings(source="webcam")
    assert daemon.launch_command(DaemonConfig(headless=False, camera=webcam)) == [
        "/bin/mjpython",
        "-m",
        "reachy_mini_bridge.sim_daemon",
        "--preload-datasets",
        "--camera",
        "webcam",
        "--webcam-hfov",
        "70",
    ]
    chosen = SimCameraSettings(source="webcam", device=1, hfov_deg=62.5)
    assert daemon.launch_command(DaemonConfig(camera=chosen))[-7:] == [
        "--preload-datasets",
        "--camera",
        "webcam",
        "--webcam-device",
        "1",
        "--webcam-hfov",
        "62.5",
    ]
    scene = str(tmp_path / "scene.xml")
    assert daemon.launch_command(DaemonConfig(scene=scene, camera=webcam))[-5:] == [
        "--preload-datasets",
        "--camera",
        "webcam",
        "--webcam-hfov",
        "70",
    ]
    # device and field of view play no part for the rendered camera
    rendered = SimCameraSettings(source="sim", device=1, hfov_deg=62.5)
    assert "--camera" not in daemon.launch_command(DaemonConfig(camera=rendered))


def test_launch_command_runs_a_scene_file_through_the_bridge_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `scene` ending in `.xml` is a scene *file* (specs/sim_scene.md): the test scene's
    launcher module runs the sim daemon on it — under mjpython for the viewer, this
    interpreter headless — with the path made absolute."""
    monkeypatch.setattr(daemon.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.chdir(tmp_path)
    scene = tmp_path / "scene.xml"
    assert daemon.launch_command(DaemonConfig(scene="scene.xml")) == [
        sys.executable,
        "-m",
        "reachy_mini_bridge.testing.sim_scene",
        "--scene-path",
        str(scene),
        "--headless",
        "--preload-datasets",
    ]
    assert daemon.launch_command(
        DaemonConfig(headless=False, scene=str(scene), preload_datasets=False)
    ) == [
        "/bin/mjpython",
        "-m",
        "reachy_mini_bridge.testing.sim_scene",
        "--scene-path",
        str(scene),
        "--no-preload-datasets",
    ]
    # a real daemon ignores the sim knobs, scene file and camera included
    monkeypatch.setattr(daemon, "_placo_available", lambda: False)
    config = DaemonConfig(scene="scene.xml", camera=SimCameraSettings(source="webcam"))
    assert daemon.launch_command(config, backend="real") == [
        "/bin/reachy-mini-daemon",
        "--preload-datasets",
    ]


def test_launch_command_scene_file_still_needs_the_sim_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.shutil, "which", lambda name: None)
    with pytest.raises(DaemonError, match=r"reachy-mini-bridge\[sim\]"):
        daemon.launch_command(DaemonConfig(scene="/tmp/scene.xml"))
    monkeypatch.setattr(
        daemon.shutil, "which", lambda name: "/bin/x" if name != "mjpython" else None
    )
    with pytest.raises(DaemonError, match="mjpython"):
        daemon.launch_command(DaemonConfig(headless=False, scene="/tmp/scene.xml"))


def test_launch_command_requires_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sim extra (which ships `reachy-mini-daemon` and MuJoCo) is required for every
    sim recipe; the viewer also needs `mjpython`."""
    monkeypatch.setattr(daemon.shutil, "which", lambda name: None)
    with pytest.raises(DaemonError, match=r"reachy-mini-bridge\[sim\]"):
        daemon.launch_command(DaemonConfig())
    with pytest.raises(DaemonError, match=r"reachy-mini-bridge\[sim\]"):
        daemon.launch_command(DaemonConfig(headless=False))
    monkeypatch.setattr(
        daemon.shutil, "which", lambda name: "/bin/x" if name != "mjpython" else None
    )
    with pytest.raises(DaemonError, match="mjpython"):
        daemon.launch_command(DaemonConfig(headless=False))


def test_launch_command_real_robot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(daemon, "_placo_available", lambda: False)
    # No --sim; the sim-only knobs (headless, scene) play no part.
    assert daemon.launch_command(
        DaemonConfig(headless=False, scene="minimal"), backend="real"
    ) == ["/bin/reachy-mini-daemon", "--preload-datasets"]
    assert daemon.launch_command(
        DaemonConfig(preload_datasets=False), backend="real"
    ) == ["/bin/reachy-mini-daemon", "--no-preload-datasets"]


def test_launch_command_real_uses_placo_when_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(daemon, "_placo_available", lambda: True)
    assert daemon.launch_command(DaemonConfig(), backend="real") == [
        "/bin/reachy-mini-daemon",
        "--kinematics-engine",
        "Placo",
        "--preload-datasets",
    ]


def test_launch_command_real_requires_the_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.shutil, "which", lambda name: None)
    with pytest.raises(DaemonError, match="reachy-mini-daemon") as excinfo:
        daemon.launch_command(DaemonConfig(), backend="real")
    assert "[sim]" not in str(excinfo.value)  # the launcher ships with the base dep


def test_launch_command_rejects_an_unknown_backend() -> None:
    with pytest.raises(ValueError, match="fake"):
        daemon.launch_command(DaemonConfig(), backend="fake")


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


# --- the real spawn seam (specs/daemon.md "The child runs in its own session") -------

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions / process groups")
def test_spawn_puts_the_child_in_its_own_session() -> None:
    proc = daemon._spawn(_SLEEPER, daemon.scrubbed_env())
    try:
        assert os.getsid(proc.pid) != os.getsid(0)
        assert os.getpgid(proc.pid) != os.getpgid(0)
    finally:
        proc.terminate()
        proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions / process groups")
def test_terminal_sigint_does_not_reach_the_child() -> None:
    """The bug itself: a terminal's Ctrl+C is a SIGINT to the whole foreground process
    group. The harness below plays the bridge process: it spawns a child through the
    real seam, then interrupts its own process group as a terminal would — the child
    must survive it (only `_stop` ends a daemon, after the robot session closed)."""
    harness = textwrap.dedent(
        f"""
        import os, signal, sys, time
        from reachy_mini_bridge import daemon
        # A handler, not SIG_IGN: an ignored disposition is inherited across exec,
        # which would shield the child whatever the seam does.
        signal.signal(signal.SIGINT, lambda *_: None)
        child = daemon._spawn({_SLEEPER!r}, daemon.scrubbed_env())
        time.sleep(0.2)
        os.killpg(os.getpgrp(), signal.SIGINT)  # what the terminal does on Ctrl+C
        time.sleep(0.5)
        print(child.poll())
        child.terminate()
        child.wait()
        """
    )
    # In its own session, so the killpg cannot reach this pytest process.
    result = subprocess.run(
        [sys.executable, "-c", harness],
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None", (
        f"the child died of the group SIGINT (poll={result.stdout.strip()})"
    )


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
    assert cmd[1:3] == ["-m", "reachy_mini_bridge.sim_daemon"] and "--scene" in cmd
    assert not any(k in env for k in daemon._GST_BUNDLE_ENV)
    assert harness.proc.calls == ["terminate", "wait"]


def test_auto_spawns_the_real_recipe_for_a_real_backend(harness: _Harness) -> None:
    harness.ready = iter([False, True])
    with daemon.managed_daemon(_AUTO, backend="real") as handle:
        assert handle.owned is True
    (cmd, env), *_ = harness.spawned
    assert cmd == ["/bin/reachy-mini-daemon", "--preload-datasets"]
    assert not any(k in env for k in daemon._GST_BUNDLE_ENV)
    assert harness.proc.calls == ["terminate", "wait"]


def test_real_startup_errors_name_the_real_daemon(harness: _Harness) -> None:
    harness.proc = _FakeProc(polls=[1])
    with (
        pytest.raises(
            DaemonError, match=r"^real daemon exited during startup \(exit 1\)"
        ),
        daemon.managed_daemon(
            DaemonConfig(spawn="auto", headless=False), backend="real"
        ),
    ):
        pass
    clock = iter([0.0, 5.0])
    harness.proc = _FakeProc()
    with (
        pytest.MonkeyPatch.context() as mp,
        pytest.raises(
            DaemonError, match="spawned real daemon did not become ready"
        ) as excinfo,
    ):
        mp.setattr(daemon.time, "monotonic", lambda: next(clock))
        with daemon.managed_daemon(
            DaemonConfig(spawn="auto", headless=False, startup_timeout=1.0),
            backend="real",
        ):
            pass
    assert "GUI session" not in str(excinfo.value)  # the viewer hint is sim-only


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
        pytest.raises(DaemonError, match=r"exit 139.*reachy_mini_bridge.sim_daemon"),
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
