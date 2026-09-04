---
code:
  - src/reachy_mini_bridge/tools.py
tests:
---

# Agent tools (`ReachyMiniTools`)

**Status:** Draft

## Purpose

`ReachyMiniTools` exposes the [interaction API](api.md) as **agent/LLM tools**: the "functions and descriptions that call the API" that let an agent perceive and control the robot. It is the outermost layer of the bridge and the one an agent runtime consumes.

## Core concepts / Decided

- **Tools are plain, fully-typed functions with docstrings.** No hand-maintained JSON schemas and no `Tool` class/registry. Each tool is an ordinary Python function with:
  - a clear, single-responsibility **docstring** (one-line summary + an `Args:` section) — this *is* the tool description an agent reads;
  - **fully type-annotated**, JSON-friendly parameters and return value, so an agent runtime (e.g. the Claude Agent SDK / Anthropic `tool_runner`) can derive the input schema by introspection.

  Docstrings and signatures are therefore the contract; they are written to be read by a model, not just a developer.
- **Each tool wraps one `ReachyMiniApi` capability.** A tool does the JSON-friendly translation at the boundary (e.g. a camera frame returned as base64-encoded JPEG rather than a numpy array) and calls straight into [api.md](api.md). It adds no robot logic of its own — orchestration lives in the Api.
- **`ReachyMiniTools(api)` binds and collects.** Source tool functions are written against a `ReachyMiniApi`; `ReachyMiniTools(api)` pre-binds that instance and returns the set of ready-to-register callables (via `functools` binding that **preserves `__name__`, `__doc__`, and annotations**, so the generated schema/description stay intact). The exposed callables show only the semantic parameters — the bound `api` is not part of the agent-visible signature. It also offers a way to enumerate the tools (e.g. iterate / `as_list()`) for handing to an agent runtime.
- **JSON-friendly in and out.** Parameters are primitives / simple containers; returns are JSON-serializable. Anything binary (images, audio) is encoded (base64) at this layer.
- **Coverage tracks the Api's v1 groups.** One tool per meaningful Api verb across Movement & gaze, Expression, Perception, and Audio out (see [api.md](api.md)). Not every Api method needs a tool; the tool set is curated for what an agent should be able to do.

## Open questions

1. **How schema is consumed.** We commit to docstring+annotation-derived tools, but which runtime(s) ingest them (Claude Agent SDK, Anthropic `tool_runner`, a manual Messages-API loop) — and whether we ship a thin adapter for any of them — is deferred. The function/docstring contract is designed to be portable regardless.
2. **Error reporting to the agent.** Whether tool failures return a structured error payload or raise (letting the runtime format them) is deferred until the consuming runtime is chosen.
3. **Perception payload sizes.** Returning full camera frames as base64 to a model is expensive; whether to downscale/limit by default is deferred to the implementation plan.
