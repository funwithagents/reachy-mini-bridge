"""The Gradio layer of the control panel (specs/examples/control_panel.md "The UI").

`build_page(model, session)` is the page: the Robot tab, wiring components to
`ControlPanelController` calls and adding nothing the controller cannot do, then
app-setup's Setup and Secrets panels over the bridge's setup contract. `main(argv)` is
the `python -m examples.control_panel` entry: the app's folder in, a web server out, the
session started at launch unless the launch choice says otherwise.

    uv run python -m examples.control_panel [--folder PATH] [--host ADDRESS] [--port N]
        [--start | --no-start]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import gradio as gr
import numpy as np
import numpy.typing as npt
from app_setup.app import App, AppModel, Section
from app_setup.autostart import add_start_arguments, starts_at_launch
from app_setup.folder import ConfigFolder, default_folder
from app_setup.ui import secrets_panel, setup_panel

import reachy_mini_bridge.setup
from examples.control_panel.controller import (
    ControlPanelController,
    FaceView,
    PanelState,
    UsersView,
    draw_faces,
)
from examples.control_panel.session import SECTION, RobotSession
from reachy_mini_bridge import BridgeError, ReachyMiniConfig
from reachy_mini_bridge.config import IDLE_MODES

log = logging.getLogger(__name__)

# The app's default config, which app-setup writes into an empty folder: the repo's
# full example config (every field, the headless sim with the webcam, a Phonon voice), as
# the panel used before, under the app's one section. On a fresh install the Setup tab
# shows what it needs (the sim extra, the voice's extra and model, the datasets).
DEFAULT_CONFIG_FILE = Path(__file__).parents[1] / "configs" / "full.json"
DEFAULT_CONFIG = {SECTION: json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8"))}

APP = App(
    name="reachy-mini-bridge-control-panel",
    title="Reachy Mini control panel",
    host="reachy-mini-bridge",
    sections=(Section(SECTION, reachy_mini_bridge.setup, title="Reachy Mini"),),
    default_config=DEFAULT_CONFIG,
    autostart=True,
)

REFRESH_S = 0.5
# The camera has its own, faster timer so the face markers follow a moving face.
CAMERA_REFRESH_S = 0.2
# Show the camera as a mirror (like a selfie view): someone stepping to their right moves
# right on screen. Display only — the faces line keeps the bridge's image coordinates.
MIRROR_CAMERA = True
LOG_LINES = 50
MOTOR_STATES = ("enabled", "disabled", "gravity_compensation")

# The log is a plain list of lines, newest first, trimmed to LOG_LINES.
Log = list[str]


def log_line(log: Log, message: str) -> None:
    log.insert(0, f"{time.strftime('%H:%M:%S')}  {message}")
    del log[LOG_LINES:]


def state_table(state: PanelState) -> str:
    """The snapshot as a Markdown table in two halves side by side: the session on the
    left, the modes on the right."""
    session = [
        ("backend", state.backend),
        ("motors", state.motors),
        ("attention", state.attention or "—"),
        ("posture", _posture(state.posture)),
        ("body yaw", f"{state.body_yaw_deg:+.0f}°"),
        ("voice", state.voice),
        ("busy", ", ".join(state.busy) or "—"),
        ("mic", f"{state.mic_sample_rate} Hz" if state.mic_sample_rate else "—"),
    ]
    modes = [
        ("presence", "on" if state.presence else "off"),
        ("idle", state.idle),
        ("wobbling", "on" if state.wobbling else "off"),
        ("tracking", "on" if state.tracking else "off"),
        ("following", "on" if state.base_following else "off"),
        ("detection", "on" if state.face_detection else "off"),
    ]
    modes += [("", "")] * (len(session) - len(modes))

    def cells(name: str, value: str) -> str:
        return f"**{name}** | {value}" if name else " | "

    lines = ["| | | | |", "|---|---|---|---|"]
    lines += [
        f"| {cells(*left)} | {cells(*right)} |" for left, right in zip(session, modes)
    ]
    return "\n".join(lines)


def _posture(posture: tuple[float, float, bool] | None) -> str:
    """The head's posture toward the followed face: its mirrored roll, its lean
    (+ toward the person, - back), and whether that person is in the personal space."""
    if posture is None:
        return "—"
    roll_deg, offset_mm, inside = posture
    text = f"roll {roll_deg:+.0f}°, lean {offset_mm:+.0f} mm"
    return f"{text}, in personal space" if inside else text


def faces_markdown(view: FaceView, rate: float | None) -> str:
    """The Faces heading, with their update rate in it, over the faces in view as a
    table, one row per face: its track id (``●`` on the one the head follows), position,
    size and, when known, roll in degrees and distance."""
    heading = "### Faces" if rate is None else f"### Faces ({rate:.1f} updates/s)"
    if not view.active:
        return f"{heading}\n\n— (no detector is looking)"
    if not view.positions:
        return f"{heading}\n\nnobody in view"
    lines = [
        heading,
        "",
        "| face | position | size | roll | distance |",
        "|---|---|---|---|---|",
    ]
    for i, (x, y) in enumerate(view.positions):
        track = f"**#{view.track_ids[i]}**" if i < len(view.track_ids) else "—"
        mark = " ●" if i == view.target else ""
        size = f"{view.sizes[i]:.2f}" if i < len(view.sizes) else "—"
        roll = view.rolls[i] if i < len(view.rolls) else None
        tilt = "—" if roll is None else f"{math.degrees(roll):+.0f}°"
        distance = view.distances[i] if i < len(view.distances) else None
        away = "—" if distance is None else f"{distance:.2f} m"
        lines.append(
            f"| {track}{mark} | ({x:+.2f}, {y:+.2f}) | {size} | {tilt} | {away} |"
        )
    return "\n".join(lines)


def _yes(flag: bool) -> str:
    return "yes" if flag else "**no**"


def users_markdown(view: UsersView) -> str:
    """The users present as a table: one row per user, ``●`` on the one the head
    follows, ``📌`` on the pinned one."""
    if not view.active:
        return "— (no detector is looking)"
    if not view.users:
        return "nobody present"
    lines = [
        "| user | seen | in view | missing | position | size | distance | identity |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for user in view.users:
        face = user.face
        mark = " ●" if user.track_id == view.track_id else ""
        pin = " 📌" if user.track_id == view.pinned_track_id else ""
        distance = "—" if face.distance_m is None else f"{face.distance_m:.2f} m"
        missing = f"{user.missing_s:.1f} s" if user.missing_s else "—"
        lines.append(
            f"| **#{user.track_id}**{mark}{pin} | {_yes(user.seen)}"
            f" | {_yes(user.in_view)} | {missing} | ({face.x:+.2f}, {face.y:+.2f})"
            f" | {face.size:.2f} | {distance} | {user.identity or '—'} |"
        )
    return "\n".join(lines)


def head_tracking_markdown(view: UsersView) -> str:
    """The Head tracking heading, with whom the head follows in it: ``#4`` (pinned or not), nobody while
    watching (with the pin a lost pinned user leaves), or tracking off; focus when on.
    The followed user's state is their row in the users table."""
    if not view.tracking:
        return "### Head tracking: off"
    focus = " · focus" if view.focus else ""
    if view.track_id is None:
        lost = (
            ""
            if view.pinned_track_id is None
            else f", pinned #{view.pinned_track_id} lost"
        )
        return f"### Head tracking: following nobody (watching{lost}){focus}"
    pinned = " · pinned" if view.pinned_track_id == view.track_id else ""
    return f"### Head tracking: following #{view.track_id}{pinned}{focus}"


def refresh_users(controller: ControlPanelController) -> tuple[str, str, str]:
    """One users tick: the users table, the head tracking, the tracking events."""
    view = controller.users_view()
    return (
        users_markdown(view),
        head_tracking_markdown(view),
        "\n".join(controller.tracking_events()),
    )


def refresh(controller: ControlPanelController, log: Log) -> tuple[str, float, str]:
    """One state tick: the state table, the mic level, the log."""
    state = controller.snapshot()
    return state_table(state), round(state.mic_level, 3), "\n".join(log)


def refresh_camera(
    controller: ControlPanelController, log: Log
) -> tuple[npt.NDArray[np.uint8] | None, str]:
    """One camera tick: the frame with a marker on each reported face (mirrored when
    ``MIRROR_CAMERA``), and the faces table."""
    view = controller.face_view()  # one report: markers, tilts and the target agree
    text = faces_markdown(view, controller.face_rate)
    try:
        frame = controller.camera_frame_rgb()
    except BridgeError as exc:
        log_line(log, f"camera: error: {exc}")
        return None, text
    if frame is None:
        return None, text
    labels = [f"#{track_id}" for track_id in view.track_ids]
    marked = draw_faces(
        frame,
        view.positions,
        view.rolls,
        view.target,
        labels=labels,
        mirror=MIRROR_CAMERA,
    )
    return marked, text


def session_buttons(session: RobotSession) -> tuple[dict[str, Any], dict[str, Any]]:
    """Start and Stop, greyed by the session's status: Start only while it is stopped,
    Stop only while it runs."""
    status = session.status
    return (
        gr.update(interactive=status == "stopped"),
        gr.update(interactive=status == "running"),
    )


def session_markdown(session: RobotSession) -> str:
    """The session line: starting or stopping, the backend running, or why the last
    Start started nothing."""
    if session.status == "starting":
        return "**Session:** starting…"
    if session.status == "stopping":
        return "**Session:** stopping…"
    controller = session.controller
    if controller is not None and controller.running:
        return (
            f"**Session:** running on the `{controller.bridge.config.backend}` backend"
        )
    if session.reasons:
        return "**Session:** not started:\n\n" + "\n".join(
            f"- {reason}" for reason in session.reasons
        )
    return "**Session:** none — press Start"


def _shown_config(model: AppModel, session: RobotSession) -> ReachyMiniConfig:
    """The config the controls start from: the session's, else the saved one, else the
    defaults (a saved config the bridge refuses)."""
    controller = session.controller
    if controller is not None:
        return controller.bridge.config
    try:
        section = model.used_config(model.saved_config()).get(SECTION, {})
        return ReachyMiniConfig.from_dict(section)
    except (ValueError, KeyError):
        return ReachyMiniConfig()


def _control_values(
    config: ReachyMiniConfig, controller: ControlPanelController | None
) -> list[Any]:
    """The controls' values for a config (the order of `_robot_tab`'s `controls`): the
    config as JSON, the emotions, the modes, the posture, the motor state read."""
    emotions = controller.emotions if controller is not None else []
    motors: str | None = None
    if controller is not None:
        try:
            motors = controller.get_motors_state()
        except BridgeError:
            motors = None
    tracking = config.motion.tracking
    return [
        dataclasses.asdict(config),
        gr.update(choices=emotions, value=emotions[0] if emotions else None),
        tracking.enabled,
        tracking.focus,
        config.motion.wobbling,
        config.motion.presence,
        config.face_detection.enabled,
        config.motion.idle,
        tracking.mirroring.enabled,
        tracking.approach.enabled,
        tracking.personal_space.enabled,
        config.motion.base_following.enabled,
        motors,
    ]


def _robot_tab(model: AppModel, session: RobotSession) -> None:
    """Start / Stop and the session line, then the panel over the running session."""
    log_lines: Log = []
    config = _shown_config(model, session)
    (
        _,
        _,
        tracking_on,
        focus_on,
        wobbling_on,
        presence_on,
        detection_on,
        idle_mode,
        mirroring_on,
        approach_on,
        personal_on,
        following_on,
        motors_now,
    ) = _control_values(config, session.controller)
    emotions = session.controller.emotions if session.controller is not None else []

    def guarded(verb: str, name: str) -> Callable[..., None]:
        """A controller call from a handler, on the running session: outcomes to the
        log, errors (no session included) to a toast."""

        def run(*args: Any) -> None:
            try:
                result = getattr(session.require(), name)(*args)
            except (BridgeError, ValueError, FileNotFoundError) as exc:
                log_line(log_lines, f"{verb}: error: {exc}")
                raise gr.Error(str(exc), title=verb, print_exception=False) from exc
            if result is True:
                log_line(log_lines, f"{verb}: done")
            elif result is False:
                log_line(log_lines, f"{verb}: stopped")
            elif isinstance(result, int):
                log_line(log_lines, f"{verb}: stopped {result}")
            else:
                log_line(log_lines, f"{verb}: ok")

        return run

    with gr.Row(equal_height=True):
        start = gr.Button(
            "Start",
            variant="primary",
            scale=0,
            interactive=session.status == "stopped",
        )
        stop = gr.Button(
            "Stop", variant="stop", scale=0, interactive=session.status == "running"
        )
        session_md = gr.Markdown(session_markdown(session))
    with gr.Accordion("Config (read-only)", open=False):
        config_json = gr.JSON(value=dataclasses.asdict(config))

    # The controls first: one column per verb group, then the modes, then the
    # motors on one line.
    with gr.Row():
        with gr.Column():
            gr.Markdown("## Expression")
            emotion = gr.Dropdown(
                emotions, value=emotions[0] if emotions else None, label="Emotion"
            )
            with gr.Row():
                play_emotion = gr.Button("Play")
                stop_emotion = gr.Button("Stop", variant="stop")

        with gr.Column():
            gr.Markdown("## Speech")
            text = gr.Textbox(label="Text", value="Hello, I am Reachy Mini.")
            with gr.Row():
                say = gr.Button("Say")
                stop_say = gr.Button("Stop", variant="stop")

        with gr.Column():
            gr.Markdown("## Sound")
            sound_file = gr.Textbox(label="Sound file (path)")
            with gr.Row():
                play_sound = gr.Button("Play sound")
                stop_sound = gr.Button("Stop sound", variant="stop")

    gr.Markdown("## Modes")
    with gr.Row(equal_height=True):
        tracking = gr.Checkbox(tracking_on, label="Tracking")
        focus = gr.Checkbox(focus_on, label="Focus")
        wobbling = gr.Checkbox(wobbling_on, label="Wobbling")
        presence = gr.Checkbox(presence_on, label="Presence")
        detection = gr.Checkbox(detection_on, label="Detection")
        idle = gr.Dropdown(list(IDLE_MODES), value=idle_mode, label="Idle")
    with gr.Row(equal_height=True):
        mirroring = gr.Checkbox(mirroring_on, label="Mirroring")
        approach = gr.Checkbox(approach_on, label="Approach")
        personal = gr.Checkbox(personal_on, label="Personal space")
        following = gr.Checkbox(following_on, label="Base following")
    gr.Markdown(
        "*Focus: while tracking, the head is held on the face, no idle motion. "
        "Mirroring, approach, personal space: the posture the head takes toward "
        "the person it follows — their head roll mirrored, a lean toward them, a "
        "step back from someone too close. Base following: the base turns toward "
        "the person, every move played around that body yaw, held after they "
        "leave.*"
    )

    gr.Markdown("## Motors")
    with gr.Row(equal_height=True):
        motors_radio = gr.Radio(
            list(MOTOR_STATES), value=motors_now, label="Torque", scale=4
        )
        apply_motors = gr.Button("Apply", scale=1)

    controls = [
        config_json,
        emotion,
        tracking,
        focus,
        wobbling,
        presence,
        detection,
        idle,
        mirroring,
        approach,
        personal,
        following,
        motors_radio,
    ]
    # Then what the robot sees and hears (left) beside what it detects: the faces
    # in view, the users present, whom the head follows (right), each heading
    # carrying its live summary.
    with gr.Row():
        with gr.Column():
            gr.Markdown("## Camera")
            camera = gr.Image(label="Camera", interactive=False, height=240)
            mic_level = gr.Slider(
                0, 1, value=0, step=0.001, label="Mic level", interactive=False
            )

        with gr.Column():
            gr.Markdown("## Detection and tracking")
            faces_md = gr.Markdown("### Faces")
            gr.Markdown("### Users")
            users_md = gr.Markdown()
            head_md = gr.Markdown("### Head tracking")

    # Last, the state (left) and the logs (right): the verbs' outcomes in one
    # tab, how the ids went in the other.
    with gr.Row():
        with gr.Column():
            gr.Markdown("## State")
            state_md = gr.Markdown()

        with gr.Column():
            gr.Markdown("## Logs")
            with gr.Tabs():
                with gr.Tab("Log"):
                    log_box = gr.Textbox(show_label=False, lines=12, interactive=False)
                with gr.Tab("Tracking events"):
                    events_box = gr.Textbox(
                        show_label=False, lines=12, interactive=False
                    )

    def on_start() -> list[Any]:
        reasons = session.start(model)
        controller = session.controller
        line = [session_markdown(session), *session_buttons(session)]
        if reasons or controller is None:
            log_line(log_lines, "start: not started")
            return [*line, *[gr.update() for _ in controls]]
        log_line(log_lines, "start: running")
        return [*line, *_control_values(controller.bridge.config, controller)]

    def on_stop() -> list[Any]:
        if session.stop():
            log_line(log_lines, "stop: done")
        return [session_markdown(session), *session_buttons(session)]

    def pressed(starting: bool) -> list[Any]:
        """Both buttons greyed at once, before the Start or Stop runs."""
        verb = "starting" if starting else "stopping"
        return [
            f"**Session:** {verb}…",
            gr.update(interactive=False),
            gr.update(interactive=False),
        ]

    line = [session_md, start, stop]
    start.click(lambda: pressed(True), outputs=line, queue=False).then(
        on_start, outputs=[*line, *controls], api_name="start"
    )
    stop.click(lambda: pressed(False), outputs=line, queue=False).then(
        on_stop, outputs=line, api_name="stop"
    )

    timer = gr.Timer(REFRESH_S)
    timer.tick(
        lambda: (
            session_markdown(session),
            *session_buttons(session),
            *refresh_session(session, log_lines),
        ),
        outputs=[*line, state_md, mic_level, log_box],
        show_progress="hidden",
        api_name="refresh",
    )
    camera_timer = gr.Timer(CAMERA_REFRESH_S)
    camera_timer.tick(
        lambda: refresh_session_camera(session, log_lines),
        outputs=[camera, faces_md],
        show_progress="hidden",
        api_name="refresh_camera",
    )
    camera_timer.tick(
        lambda: refresh_session_users(session),
        outputs=[users_md, head_md, events_box],
        show_progress="hidden",
        api_name="refresh_users",
    )

    def bind(
        event: Callable[..., Any],
        verb: str,
        name: str,
        inputs: Any = None,
    ) -> None:
        event(guarded(verb, name), inputs=inputs, api_name=verb)

    bind(apply_motors.click, "set_motors_state", "set_motors_state", motors_radio)
    bind(play_emotion.click, "play_emotion", "play_emotion", emotion)
    bind(stop_emotion.click, "stop_emotion", "stop_emotion")
    bind(say.click, "say", "say", text)
    bind(stop_say.click, "stop_saying", "stop_saying")
    bind(play_sound.click, "play_sound", "play_sound", sound_file)
    bind(stop_sound.click, "stop_sound", "stop_sound")
    for box in (tracking, focus):
        bind(box.input, "set_head_tracking", "set_head_tracking", [tracking, focus])
    for box in (mirroring, approach, personal):
        bind(box.input, "set_posture", "set_posture", [mirroring, approach, personal])
    bind(following.input, "set_base_following", "set_base_following", following)
    bind(wobbling.input, "set_wobbling", "set_wobbling", wobbling)
    bind(presence.input, "set_presence", "set_presence", presence)
    bind(idle.input, "set_idle", "set_idle", idle)
    bind(detection.input, "set_face_detection", "set_face_detection", detection)


NO_SESSION = "— (no session)"


def refresh_session(session: RobotSession, log: Log) -> tuple[str, float, str]:
    """One state tick over the session: `refresh`, or the empty state with none."""
    controller = session.controller
    if controller is None or not controller.running:
        return NO_SESSION, 0.0, "\n".join(log)
    return refresh(controller, log)


def refresh_session_camera(
    session: RobotSession, log: Log
) -> tuple[npt.NDArray[np.uint8] | None, str]:
    """One camera tick over the session: `refresh_camera`, or nothing with none."""
    controller = session.controller
    if controller is None or not controller.running:
        return None, f"### Faces\n\n{NO_SESSION}"
    return refresh_camera(controller, log)


def refresh_session_users(session: RobotSession) -> tuple[str, str, str]:
    """One users tick over the session: `refresh_users`, or nothing with none."""
    controller = session.controller
    if controller is None or not controller.running:
        return NO_SESSION, "### Head tracking", ""
    return refresh_users(controller)


def build_page(
    model: AppModel, session: RobotSession, *, started_at_launch: bool = False
) -> gr.Blocks:
    """The panel's page: its Robot tab, then app-setup's Setup and Secrets panels. It
    opens on Robot when the session started at launch, else on Setup."""
    with gr.Blocks(title=model.title) as page:
        gr.Markdown(f"# {model.title}")
        with gr.Tabs(selected="robot" if started_at_launch else "setup"):
            with gr.Tab("Robot", id="robot"):
                _robot_tab(model, session)
            with gr.Tab("Setup", id="setup"):
                setup = setup_panel(model)
            with gr.Tab("Secrets", id="secrets"):
                secrets_panel(model, setup.draft)
    return page


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    folder = default_folder(APP.name)
    parser = argparse.ArgumentParser(
        prog="python -m examples.control_panel",
        description="A Gradio control panel over ReachyMiniBridge: the robot set up in "
        "the page, every verb a button, the robot's state on screen.",
    )
    parser.add_argument(
        "--folder",
        type=Path,
        default=folder,
        help=f"the panel's folder: its config, its keys (default: {folder})",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web server address")
    parser.add_argument("--port", type=int, default=7860, help="web server port")
    add_start_arguments(parser)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    options = _arguments(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    session = RobotSession()
    # Building the model makes the folder ready and loads its .env; the session locks
    # the config while it runs.
    model = AppModel(
        APP, ConfigFolder(options.folder, APP.default_config), locked=session.locked
    )
    log.info("folder: %s", model.path)
    at_launch = starts_at_launch(model, options.start)
    if at_launch:
        for reason in session.start(model):
            log.warning("the session did not start at launch: %s", reason)
    try:
        build_page(model, session, started_at_launch=at_launch).launch(
            server_name=options.host, server_port=options.port
        )
    finally:
        session.stop()
