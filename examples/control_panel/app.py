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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import gradio as gr
import numpy as np
import numpy.typing as npt

from examples.control_panel.controller import (
    ControlPanelController,
    PanelState,
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
    """The snapshot as a Markdown table."""
    rows = [
        ("backend", state.backend),
        ("motors", state.motors),
        ("attention", state.attention or "—"),
        ("voice", state.voice),
        ("presence", "on" if state.presence else "off"),
        ("idle", state.idle),
        ("wobbling", "on" if state.wobbling else "off"),
        ("tracking", "on" if state.tracking else "off"),
        ("detection", "on" if state.face_detection else "off"),
        ("busy", ", ".join(state.busy) or "—"),
        ("mic", f"{state.mic_sample_rate} Hz" if state.mic_sample_rate else "—"),
    ]
    lines = ["| | |", "|---|---|"]
    lines += [f"| **{name}** | {value} |" for name, value in rows]
    return "\n".join(lines)


def faces_line(
    positions: list[tuple[float, float]] | None,
    rate: float | None,
    rolls: Sequence[float | None] = (),
) -> str:
    """The line under the camera: the faces — their position and, when known, their
    roll in degrees — and their update rate. ``positions`` is ``None`` while no
    detector is looking."""
    if positions is None:
        return "**Faces:** — (no detector is looking)"

    def face(i: int, x: float, y: float) -> str:
        roll = rolls[i] if i < len(rolls) else None
        tilt = "" if roll is None else f", roll {math.degrees(roll):+.0f}°"
        return f"({x:+.2f}, {y:+.2f}{tilt})"

    where = ", ".join(face(i, x, y) for i, (x, y) in enumerate(positions))
    faces = f"{len(positions)} at {where}" if where else "0"
    updates = "—" if rate is None else f"{rate:.1f}/s"
    return f"**Faces:** {faces} · **updates:** {updates}"


def refresh(controller: ControlPanelController, log: Log) -> tuple[str, float, str]:
    """One state tick: the state table, the mic level, the log."""
    state = controller.snapshot()
    return state_table(state), round(state.mic_level, 3), "\n".join(log)


def refresh_camera(
    controller: ControlPanelController, log: Log
) -> tuple[npt.NDArray[np.uint8] | None, str]:
    """One camera tick: the frame with a marker on each reported face (mirrored when
    ``MIRROR_CAMERA``), and the faces line under it."""
    view = controller.face_view()  # one report: markers, tilts and the target agree
    text = faces_line(
        view.positions if view.active else None, controller.face_rate, view.rolls
    )
    try:
        frame = controller.camera_frame_rgb()
    except BridgeError as exc:
        log_line(log, f"camera: error: {exc}")
        return None, text
    if frame is None:
        return None, text
    marked = draw_faces(frame, view.positions, view.rolls, view.target)
    if MIRROR_CAMERA:
        marked = np.ascontiguousarray(marked[:, ::-1])
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

        with gr.Row():
            with gr.Column():
                gr.Markdown("## State")
                state_md = gr.Markdown()
                mic_level = gr.Slider(
                    0, 1, value=0, step=0.001, label="Mic level", interactive=False
                )
                camera = gr.Image(label="Camera", interactive=False, height=240)
                faces_md = gr.Markdown()
                log_box = gr.Textbox(label="Log", lines=10, interactive=False)

            with gr.Column():
                gr.Markdown("## Motors")
                with gr.Row():
                    motors_radio = gr.Radio(
                        list(MOTOR_STATES), value=motors_now, label="Torque"
                    )
                    apply_motors = gr.Button("Apply")

                gr.Markdown("## Expression")
                with gr.Row():
                    emotion = gr.Dropdown(
                        emotions,
                        value=emotions[0] if emotions else None,
                        label="Emotion",
                    )
                    play_emotion = gr.Button("Play")
                    stop_emotion = gr.Button("Stop", variant="stop")

                gr.Markdown("## Speech")
                with gr.Row():
                    text = gr.Textbox(
                        label="Text", value="Hello, I am Reachy Mini.", scale=3
                    )
                    say = gr.Button("Say")
                    stop_say = gr.Button("Stop", variant="stop")
                with gr.Row():
                    sound_file = gr.Textbox(label="Sound file (path)", scale=3)
                    play_sound = gr.Button("Play sound")
                    stop_sound = gr.Button("Stop sound", variant="stop")

                gr.Markdown("## Gaze")
                with gr.Row():
                    tracking = gr.Checkbox(config.motion.tracking, label="Tracking")
                    focus = gr.Checkbox(
                        False, label="Focus (head held on the face, no idle motion)"
                    )

                gr.Markdown("## Modes")
                with gr.Row():
                    wobbling = gr.Checkbox(config.motion.wobbling, label="Wobbling")
                    presence = gr.Checkbox(config.motion.presence, label="Presence")
                    detection = gr.Checkbox(
                        config.face_detection.enabled, label="Detection"
                    )
                    idle = gr.Radio(
                        list(IDLE_MODES), value=config.motion.idle, label="Idle"
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
