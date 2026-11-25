"""A config app: one bridge config file edited in the control panel's Setup and Secrets
tabs, with no session (specs/examples/control_panel.md "A config app").

    uv run python -m examples.config_app FILE [--env PATH] [--host ADDRESS] [--port N]

The file is a bridge config document (any of ``examples/configs/``); Save writes it back,
the keys go to the ``.env`` next to it unless ``--env`` names another.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from app_setup.app import AppModel
from app_setup.config_file import ConfigFileError
from app_setup.launch import add_config_app_arguments, open_config_app
from app_setup.ui import config_page

from examples.control_panel.app import APP

__all__ = ["main", "open_model"]


def open_model(
    argv: Sequence[str] | None = None,
) -> tuple[argparse.Namespace, AppModel]:
    """The options and the model over the file named, writable, its .env loaded. A
    missing or unreadable file stops here, with the reason (`SystemExit`)."""
    parser = argparse.ArgumentParser(
        prog="python -m examples.config_app",
        description="Edit a Reachy Mini bridge config file: its Setup and its Secrets.",
    )
    add_config_app_arguments(parser)
    options = parser.parse_args(argv)
    try:
        return options, open_config_app(options, APP)
    except ConfigFileError as exc:
        parser.error(str(exc))


def main(argv: Sequence[str] | None = None) -> None:
    options, model = open_model(argv)
    config_page(model).launch(server_name=options.host, server_port=options.port)
