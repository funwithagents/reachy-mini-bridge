---
code:
  - src/reachy_mini_bridge/setup.py
  - src/reachy_mini_bridge/fields.py
  - src/reachy_mini_bridge/bridge.py
  - src/reachy_mini_bridge/audio.py
tests:
  - tests/test_setup.py
---

# Setup (`reachy_mini_bridge.setup`)

**Status:** Stable

## Purpose

Everything an application needs to set a bridge up for a user without knowing the bridge: what its config looks like, which extras a config needs, what is wrong in it, what this machine is missing to run it (an extra, a key, a model to download), and whether it works. An application draws a form from it, installs what is missing, downloads what can be downloaded, tries it, and only then builds a `ReachyMiniBridge`.

The functions implement version 1 of the **module contract** defined by `module-setup`, a dev dependency only (its conformance check runs in the tests): the contract is plain data, and the bridge imports nothing from it at runtime. This spec says what the bridge puts in the shapes the contract exchanges.

The config's `tts` block is a tts-engine engine block ([config.md](config.md)), and tts-engine implements the same contract (`tts_engine.setup`): the bridge's contract **composes** it, delegating that block to it, so a form for the bridge offers every voice tts-engine offers, with its checks, its downloads and its keys.

## Core concepts / Decided

### The `setup` module

`reachy_mini_bridge.setup` exposes seven functions over the **bridge's config document** — the object `ReachyMiniConfig.from_dict` takes:

```python
def config_schema() -> dict: ...
def required_extras(config: dict) -> list[str]: ...
def check_config(config: dict) -> list[dict]: ...
def check_setup(config: dict) -> list[dict]: ...
def prepare(config: dict, on_progress: Callable[[dict], None] | None = None) -> None: ...
def options(name: str, config: dict) -> list[dict]: ...
def probe(config: dict) -> dict: ...
```

They take and return JSON-compatible data, are synchronous, and import neither `tts_engine` providers nor any extra at module level. `config_schema`, `required_extras` and `check_config` read nothing but the config and run on the base install with no network; `check_setup` also tries the extras' imports, reads the environment and looks into the Hugging Face cache, still with no network; `prepare`, `options` and `probe` may use the network.

Each is the bridge's own part plus, for the `tts` block when it is present, the function of the same name of `tts_engine.setup` over that block, its JSON Pointers prefixed with `/tts`.

### The schema: `tts` delegated, the rest raw JSON

`config_schema()` returns `{"contract": 1, "distribution": "reachy-mini-bridge", "title": "Reachy Mini", "schema": …}`, the same value on every call. The schema is an object with these properties, none required (every block of the config is optional):

| Property | In the schema | Drawn as |
|---|---|---|
| `backend` | a string, `enum` `real` / `sim` / `fake`, default `real` — declared as a field (`fields.py`, below) | a dropdown |
| `tts` | tts-engine's own schema (`tts_engine.setup.config_schema()["schema"]`), titled "Text to speech", without its `player` property | an optional object: a switch (absent: no voice), then the module choice and its fields |
| `robot`, `daemon`, `audio`, `face_detection`, `motion` | each a free-form object (`additionalProperties: true`) with a title and a description | raw JSON |

- **The bridge's blocks are not modules**: they never change shape with a choice, and they are large, nested and documented in the configuration reference. They are edited as JSON and checked by the bridge's own loader (`check_config`, below) — the form does not re-declare them field by field, so nothing in them can drift from the loader.
- **`backend` is the one field**: a plain string at the top, a choice of three. It is a choice field, not a module `oneOf`, because the blocks under it do not change with it (one file switches `sim` ↔ `real` by changing `backend` alone, [config.md](config.md)).
- **`player` is left out of the embedded schema** because the bridge never plays through tts-engine's player: it feeds the engine a robot-speaker sink, and `player` has no effect ([config.md](config.md) "`tts` block"). A config that carries one is still accepted, as before. `gain_db` stays: the engine applies it before the sink.
- `robot`, `daemon` and `audio` are `x-advanced`; `face_detection` and `motion` are not, since they are where a first-time user turns detection and tracking on.

### Fields declared as data: `fields.py`

`reachy_mini_bridge/fields.py` is a copy of tts-engine's `modules/fields.py`, unchanged (`Field`, `read_fields`, `field_problems`, `fields_schema`): config fields declared once as data and read three ways — validation, every problem at once, and the JSON Schema. It is standard library only and knows nothing of robots or speech. It is a copy until a shared core library holds it for every library of the family; that library then replaces it. The bridge declares one field with it today, `backend`.

### `required_extras`

The extras this config needs, as requirement strings, each naming its own distribution:

| Requirement | When |
|---|---|
| `reachy-mini-bridge[sim]` | `backend` is `sim` and `daemon.spawn` is not `never`: the bridge may spawn the MuJoCo daemon, which needs MuJoCo. A sim borrowed from someone else's daemon (`spawn: "never"`) needs nothing here |
| `reachy-mini-bridge[placo]` | `daemon.kinematics_engine` is `placo` and `daemon.spawn` is not `never`: the engine is checked when the bridge launches a daemon, never for a borrowed one ([config.md](config.md) "Kinematics engines") |
| tts-engine's own, unchanged (`tts-engine[gradium]`) | the `tts` block's `module.type` needs one |

A tts-engine requirement is passed through as tts-engine spells it: an application installs it through the bridge's matching `tts-<extra>` alias, found from the bridge's package metadata ([project.md](../project.md) "Optional extras"), so the bridge does not reword it here. It reads only those fields, never raises, and ignores anything malformed (an unknown backend, a `daemon` that is not an object): `check_config` reports those.

### `check_config`

Every problem that makes the bridge refuse this config, each `{"path": <JSON Pointer>, "message": …}`, `[]` when it is accepted:

1. **The bridge's own blocks** go through `ReachyMiniConfig.from_dict` (the `tts` block set aside) — the one validation path every constructor uses, so what `check_config` accepts is what the bridge accepts. The loader stops at the first error, so this part reports one problem at a time. Its `ConfigError` message is kept word for word; its pointer is read from the message, which quotes the field it is about (`'daemon.camera.source'` → `/daemon/camera/source`; `unknown key(s) in 'daemon': speed` → `/daemon/speed`; a message about the document as a whole → `""`).
2. **The `tts` block**, when present and not `null`: an object (else one problem at `/tts`), whose problems are `tts_engine.setup.check_config`'s, each at `/tts<its path>`. tts-engine's rules apply as they are — a key written in the block (`api_key`) is refused; the key's variable is named in `api_key_env`, which defaults to the provider's usual one.

It constructs nothing and reads no environment. The `robot` keys are checked against upstream's `ReachyMini` signature, a base dependency, as the loader does.

### `check_setup`

What this machine is missing to run the config, each item `{"id", "title", "status", "detail", "preparable"}`, asked only about a config `check_config` accepts. In this order:

1. **The bridge's extras** — one item per requirement of `required_extras` on this distribution, never preparable, found by trying the imports the extra provides (`mujoco` for `sim`, `placo` for `placo`): `ready` ("Installed."), else `missing` with the install hint (`install reachy-mini-bridge[sim]`). On Windows the `placo` item says the engine has no Windows wheels.
2. **The bridge's files**, looked up in the Hugging Face cache without network:
   - `weights:yunet` — the shipped face detector's weights, when `face_detection.detector` is `yunet`: `ready` when upstream's model file is in the cache, else `missing`, preparable.
   - `dataset:emotions` — the recorded emotions library, when `backend` is `real` or `sim` (the `fake` plays a stub library): `ready` when the dataset is in the cache, else `missing`, preparable. The bridge loads it in its own process at the first `play_emotion`, whatever the daemon preloads.
   An upstream lookup that fails (an upstream change) is `unknown`, never a raise.
3. **The `tts` block's items**, when the block is present: `tts_engine.setup.check_setup`'s, unchanged (the provider's extra as `extra:tts-engine[<extra>]`, its key variable as `key:<VARIABLE>`, its files).

The bridge itself names no key variable: the only keys are the voice's, reported by tts-engine.

### `prepare`

Makes every preparable missing item ready, through the same upstream calls the bridge makes when it runs: upstream's YuNet `FaceDetector` (which downloads its weights; built, then released) and upstream's `RecordedMoves` over the emotions dataset (which downloads it). Then the `tts` block's own `prepare`. Blocking, idempotent — an item already `ready` is not fetched again — and raising with a message for the user on failure. `on_progress` receives `{"id", "fraction": None, "message"}` before each of the bridge's downloads; the `tts` block's progress is tts-engine's.

### `options`

`"voices"` is the `tts` block's (`tts_engine.setup.options("voices", config["tts"])`): each choice's `fields` merge into the module branch carrying the annotation, as tts-engine declares it. The bridge has no list of its own (its blocks are raw JSON), so every other name returns `[]`, and so does `"voices"` with no `tts` block.

### `probe`

A light, real run: it connects, reads, and lets go; it never moves the robot and never speaks.

- **The robot**: a `ReachyMiniBridge` built from the config with no voice, presence and wobbling off, face detection and head tracking off — a session entered, the motor state read (`get_motors_state()`), the session left. With `fake` it is immediate; with `sim` and a daemon to spawn it starts MuJoCo and takes seconds; with `real` it reaches the daemon (a USB robot's spawned, a wireless robot's borrowed). The daemon it spawned is stopped when the session ends.
- **The voice**, when the config has a `tts` block: `tts_engine.setup.probe` over the block with its `player` replaced by a discarding one, so nothing is played on this machine's speakers: what tts-engine proves (the key, the model, the provider) without a sound in the room.
- It returns `{"ok", "message", "duration_s"}`: `ok` when both parts succeeded, the message saying what was done ("Connected to the sim robot (motors enabled), then disconnected. Voice: …") or what failed. It never raises; it runs on a thread of its own, so it can be called from a thread that already runs an event loop; it releases what it built before it returns.

### Keys passed in code: `secrets`

`ReachyMiniBridge(config, *, synthesizer=None, secrets=None)`: `secrets` (variable name → key) are API keys the application holds in code, handed to the default synthesizer's engine (`TTSEngineSynthesizer(block, secrets=…)` → `TTSEngine(config, sink=…, secrets=…)`), which looks the variable `api_key_env` names up in them before the environment. The bridge reads no key itself, and the config never holds one. `from_dict` / `from_json` / `from_json_file` take `secrets` too. A missing key is tts-engine's `MissingSecretError` (a `ConfigError` naming the variable), kept on `bridge.synthesizer_error` like any other build failure ([bridge.md](bridge.md) "Constructed from a config").

## Testing

`tests/test_setup.py`:

- `module_setup.contract.check_contract` passes over the repo's example configs (`examples/configs/*.json`, the greeter's and the voice tour's, each voice config wrapped onto the `fake`), plus a block for every tts module no example covers.
- `config_schema()` is stable across calls; `backend` is the one declared field; the five blocks are free-form; the `tts` property is tts-engine's schema without `player`.
- `required_extras` on each rule (`sim` spawned or borrowed, `placo` spawned or borrowed, a voice), and on malformed input without raising.
- `check_config`: the example configs pass; a table of broken configs, each reported at its pointer with the loader's message; a `tts` problem at `/tts/module/...`; an `api_key` in the block refused; a `tts` block that is not an object.
- `check_setup` on each branch: an extra `ready` when its import works and `missing` with its hint when it does not; the YuNet and emotions items only when the config uses them, `ready` / `missing` from a cache lookup seam, `unknown` when the lookup fails; the `tts` items after the bridge's, a key variable `missing` until it is set.
- `prepare` fetches only what is missing, reports progress, and propagates a failure.
- `options("voices")` over a fixture voice, `[]` for an unknown name and with no `tts` block.
- `probe` on the `fake`: `ok`, the motor state in the message, the robot never moved and the session closed; with a `tone` voice, the voice's probe ok too; a voice that fails to build reported `ok: false`, never raised.
- The secrets reach the engine: a stub module with a key field builds with a key passed in `secrets` and the variable unset.

## Open questions

1. **Every problem at once.** The loader stops at its first error, so a form marks one field of the raw blocks at a time. Collecting every error means a loader that accumulates; deferred until the raw blocks are drawn as fields.
2. **The raw blocks as fields.** `daemon` and `motion` could be declared field by field (`x-advanced`) once the shared field helper and the form's nested objects make it worth it; the loader stays the authority either way.
3. **Lists the bridge could offer**: the audio devices for `daemon.audio`, the webcams for `daemon.camera.device` — they need those blocks drawn as fields first.
