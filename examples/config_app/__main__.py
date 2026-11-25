import os

# A local tool: no usage telemetry to Gradio's servers (set before gradio imports).
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

from examples.config_app import main

if __name__ == "__main__":
    main()
