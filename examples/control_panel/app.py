"""The Gradio layer of the control panel (specs/examples/control_panel.md "The UI").

`build_app(controller)` wires components to `ControlPanelController` calls and adds
nothing the controller cannot do; `main(argv)` is the `python -m examples.control_panel`
entry: a config file in, a web server out.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gradio as gr
import numpy as np
import numpy.typing as npt

from examples.control_panel.controller import (
    ControlPanelController,
    FaceView,
    PanelState,
    UsersView,
    draw_faces,
)
from reachy_mini_bridge import BridgeError, ReachyMiniConfig
from reachy_mini_bridge.config import IDLE_MODES

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


def build_app(controller: ControlPanelController) -> gr.Blocks:
    """The panel over a started controller."""
    config = controller.bridge.config
    log: Log = []

    def guarded(verb: str, fn: Callable[..., Any]) -> Callable[..., None]:
        """Run a controller call from a handler: outcomes to the log, errors to a toast."""

        def run(*args: Any) -> None:
            try:
                result = fn(*args)
            except (BridgeError, ValueError, FileNotFoundError) as exc:
                log_line(log, f"{verb}: error: {exc}")
                raise gr.Error(str(exc), title=verb, print_exception=False) from exc
            if result is True:
                log_line(log, f"{verb}: done")
            elif result is False:
                log_line(log, f"{verb}: stopped")
            elif isinstance(result, int):
                log_line(log, f"{verb}: stopped {result}")
            else:
                log_line(log, f"{verb}: ok")

        return run

    emotions = controller.emotions
    try:
        motors_now: str | None = controller.get_motors_state()
    except BridgeError:
        motors_now = None

    with gr.Blocks(title=f"Reachy Mini control panel · {config.backend}") as demo:
        gr.Markdown(f"# Reachy Mini control panel — `{config.backend}` backend")
        with gr.Accordion("Config (read-only)", open=False):
            gr.JSON(value=dataclasses.asdict(config))

        # The controls first: one column per verb group, then the modes, then the
        # motors on one line.
        with gr.Row():
            with gr.Column():
                gr.Markdown("## Expression")
                emotion = gr.Dropdown(
                    emotions,
                    value=emotions[0] if emotions else None,
                    label="Emotion",
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
            tracking_config = config.motion.tracking
            tracking = gr.Checkbox(tracking_config.enabled, label="Tracking")
            focus = gr.Checkbox(tracking_config.focus, label="Focus")
            wobbling = gr.Checkbox(config.motion.wobbling, label="Wobbling")
            presence = gr.Checkbox(config.motion.presence, label="Presence")
            detection = gr.Checkbox(config.face_detection.enabled, label="Detection")
            idle = gr.Dropdown(list(IDLE_MODES), value=config.motion.idle, label="Idle")
        with gr.Row(equal_height=True):
            mirroring = gr.Checkbox(
                tracking_config.mirroring.enabled, label="Mirroring"
            )
            approach = gr.Checkbox(tracking_config.approach.enabled, label="Approach")
            personal = gr.Checkbox(
                tracking_config.personal_space.enabled, label="Personal space"
            )
            following = gr.Checkbox(
                config.motion.base_following.enabled, label="Base following"
            )
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
                        log_box = gr.Textbox(
                            show_label=False, lines=12, interactive=False
                        )
                    with gr.Tab("Tracking events"):
                        events_box = gr.Textbox(
                            show_label=False, lines=12, interactive=False
                        )

        timer = gr.Timer(REFRESH_S)
        timer.tick(
            lambda: refresh(controller, log),
            outputs=[state_md, mic_level, log_box],
            show_progress="hidden",
            api_name="refresh",
        )
        camera_timer = gr.Timer(CAMERA_REFRESH_S)
        camera_timer.tick(
            lambda: refresh_camera(controller, log),
            outputs=[camera, faces_md],
            show_progress="hidden",
            api_name="refresh_camera",
        )
        camera_timer.tick(
            lambda: refresh_users(controller),
            outputs=[users_md, head_md, events_box],
            show_progress="hidden",
            api_name="refresh_users",
        )

        def bind(
            event: Callable[..., Any],
            verb: str,
            fn: Callable[..., Any],
            inputs: Any = None,
        ) -> None:
            event(guarded(verb, fn), inputs=inputs, api_name=verb)

        bind(
            apply_motors.click,
            "set_motors_state",
            controller.set_motors_state,
            motors_radio,
        )
        bind(play_emotion.click, "play_emotion", controller.play_emotion, emotion)
        bind(stop_emotion.click, "stop_emotion", controller.stop_emotion)
        bind(say.click, "say", controller.say, text)
        bind(stop_say.click, "stop_saying", controller.stop_saying)
        bind(play_sound.click, "play_sound", controller.play_sound, sound_file)
        bind(stop_sound.click, "stop_sound", controller.stop_sound)
        for box in (tracking, focus):
            bind(
                box.input,
                "set_head_tracking",
                controller.set_head_tracking,
                [tracking, focus],
            )
        for box in (mirroring, approach, personal):
            bind(
                box.input,
                "set_posture",
                controller.set_posture,
                [mirroring, approach, personal],
            )
        bind(
            following.input,
            "set_base_following",
            controller.set_base_following,
            following,
        )
        bind(wobbling.input, "set_wobbling", controller.set_wobbling, wobbling)
        bind(presence.input, "set_presence", controller.set_presence, presence)
        bind(idle.input, "set_idle", controller.set_idle, idle)
        bind(
            detection.input,
            "set_face_detection",
            controller.set_face_detection,
            detection,
        )

    return demo


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m examples.control_panel",
        description="A Gradio control panel over ReachyMiniBridge: every verb a button, "
        "the robot's state on screen.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="JSON config file (specs/core/config.md). Without it: the offline fake.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web server address")
    parser.add_argument("--port", type=int, default=7860, help="web server port")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = (
        ReachyMiniConfig.from_json_file(args.config)
        if args.config is not None
        else ReachyMiniConfig(backend="fake")
    )
    with ControlPanelController(config) as controller:
        build_app(controller).launch(server_name=args.host, server_port=args.port)
