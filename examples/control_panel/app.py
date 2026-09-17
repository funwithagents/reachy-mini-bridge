"""The Gradio layer of the control panel (specs/control_panel.md "The UI").

`build_app(controller)` wires components to `ControlPanelController` calls and adds
nothing the controller cannot do; `main(argv)` is the `python -m examples.control_panel`
entry: a config file in, a web server out.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gradio as gr
import numpy as np
import numpy.typing as npt

from examples.control_panel.controller import ControlPanelController, PanelState
from reachy_mini_bridge import BridgeError, ReachyMiniConfig

REFRESH_S = 0.5
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
        ("breathing", "on" if state.breathing else "off"),
        ("wobbling", "on" if state.wobbling else "off"),
        ("tracking", "on" if state.tracking else "off"),
        ("busy", ", ".join(state.busy) or "—"),
        ("mic", f"{state.mic_sample_rate} Hz" if state.mic_sample_rate else "—"),
    ]
    lines = ["| | |", "|---|---|"]
    lines += [f"| **{name}** | {value} |" for name, value in rows]
    return "\n".join(lines)


def refresh(
    controller: ControlPanelController, log: Log
) -> tuple[str, float, npt.NDArray[np.uint8] | None, str]:
    """One timer tick: the state table, the mic level, the camera frame, the log."""
    state = controller.snapshot()
    try:
        frame = controller.camera_frame_rgb()
    except BridgeError as exc:
        log_line(log, f"camera: error: {exc}")
        frame = None
    return state_table(state), round(state.mic_level, 3), frame, "\n".join(log)


def build_app(controller: ControlPanelController) -> gr.Blocks:
    """The panel over a started controller."""
    config = controller.api.config
    log: Log = []

    def guarded(verb: str, fn: Callable[..., Any]) -> Callable[..., None]:
        """Run a controller call from a handler: outcomes to the log, errors to a toast."""

        def run(*args: Any) -> None:
            try:
                result = fn(*args)
            except (BridgeError, ValueError) as exc:
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

                gr.Markdown("## Gaze")
                with gr.Row():
                    weight = gr.Slider(
                        0, 1, value=1.0, step=0.05, label="Tracking weight", scale=3
                    )
                    start_tracking = gr.Button("Start tracking")
                    stop_tracking = gr.Button("Stop tracking")

                gr.Markdown("## Modes")
                with gr.Row():
                    wobbling = gr.Checkbox(config.motion.wobbling, label="Wobbling")
                    presence = gr.Checkbox(config.motion.presence, label="Presence")
                    breathing = gr.Checkbox(config.motion.breathing, label="Breathing")

        timer = gr.Timer(REFRESH_S)
        timer.tick(
            lambda: refresh(controller, log),
            outputs=[state_md, mic_level, camera, log_box],
            show_progress="hidden",
            api_name="refresh",
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
        bind(
            start_tracking.click,
            "start_head_tracking",
            controller.start_head_tracking,
            weight,
        )
        bind(stop_tracking.click, "stop_head_tracking", controller.stop_head_tracking)
        bind(wobbling.input, "set_wobbling", controller.set_wobbling, wobbling)
        bind(presence.input, "set_presence", controller.set_presence, presence)
        bind(breathing.input, "set_breathing", controller.set_breathing, breathing)

    return demo


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m examples.control_panel",
        description="A Gradio control panel over ReachyMiniApi: every verb a button, "
        "the robot's state on screen.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="JSON config file (specs/config.md). Without it: the offline fake.",
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
