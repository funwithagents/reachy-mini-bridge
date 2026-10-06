# Documentation

The bridge's documentation, by what you want to do. The [README](../README.md) is the landing page — what the bridge is, its support status, how to install it and a first quick start; [getting-started.md](getting-started.md) continues from there.

## By task

| I want to… | Read |
|---|---|
| write my first application | [getting-started.md](getting-started.md) — a complete program on the offline fake, then the config that moves it to a sim or a robot |
| connect a robot or the simulator | [reference/backends-and-capabilities.md](reference/backends-and-capabilities.md) — what each setup needs and gives, and how far it is validated; [examples/configs/](../examples/configs/) — a short profile per setup; [guides/running-daemons.md](guides/running-daemons.md) — starting a daemon by hand, to be borrowed; [guides/linux.md](guides/linux.md) — the Linux setup |
| make the robot speak and listen | [guides/audio.md](guides/audio.md) — a voice, `say`, sound files, wobbling, the microphone for your own speech recognizer |
| make the robot look at people | [guides/perception-and-tracking.md](guides/perception-and-tracking.md) — detection and head tracking, the reports and their events |
| run my own face detector | [guides/custom-face-detector.md](guides/custom-face-detector.md) |
| replace the breathing with my own idle move | [guides/custom-idle-move.md](guides/custom-idle-move.md) |
| drive the robot from an LLM agent | [getting-started.md](getting-started.md#driving-it-from-an-agent) — tools as one-line wrappers over the verbs |
| test my project against the bridge | [guides/testing.md](guides/testing.md) — unit tests on the fake, live tests with the shipped pytest harness and its sim scene |
| find out why it does not connect, see, move or speak | [guides/troubleshooting.md](guides/troubleshooting.md) — the checks for each symptom, routed to the page that explains it |
| look up a verb, a value, an error or a contract | [reference/api.md](reference/api.md) |
| look up a config field | [reference/configuration.md](reference/configuration.md) |
| try it without writing code | the Gradio [control panel](../examples/control_panel/) — every verb a button ([README](../README.md#try-it-from-a-browser)) |

## The references

- [reference/api.md](reference/api.md) — the public API: imports, construction, lifecycle, every verb and property, the data types, the errors, cancellation and concurrency, units, the three extension contracts, the escape hatch to the upstream SDK.
- [reference/configuration.md](reference/configuration.md) — every field of `ReachyMiniConfig` with its default, effect and runtime verb.
- [reference/backends-and-capabilities.md](reference/backends-and-capabilities.md) — the one OS / target matrix: expected capabilities apart from validated support.

## For maintainers

- [specs/](../specs/) — the normative design, one spec per concept with its status; [specs/_overview.md](../specs/_overview.md) is the architecture.
- [plans/](../plans/) — the implementation plans that built the specs, the project's history.
- [AGENTS.md](../AGENTS.md) — the operating manual: the project map, the status discipline, verification, running the live tests.
- [internals/upstream-sdk-notes.md](internals/upstream-sdk-notes.md) — what we learned about the upstream `reachy_mini` SDK: observations and workarounds, dated and versioned, not bridge API promises.
