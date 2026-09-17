import os

# A local tool: no usage telemetry to Gradio's servers (set before gradio imports).
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

from examples.control_panel.app import main

if __name__ == "__main__":
    main()
