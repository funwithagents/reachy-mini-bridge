# Your own idle move — `motion.idle: "custom"`

How to replace the robot's breathing with an idle animation of your own: the motion loop plays it between verbs, blends it in and out like breathing, composes it around a tracked face, and falls back to a still hold if it fails. Design: [specs/motion/motion.md](../../specs/motion/motion.md) "Custom idle moves"; the contract in brief: [../reference/api.md](../reference/api.md) "Extension contracts".

## The move

A move is a subclass of `IdleMove` whose `offsets(t)` returns the pose `t` seconds into the idle entry, as signed offsets from neutral in human units — millimetres of head height, degrees of head roll / pitch / yaw and of each antenna's outward lean. `IdleOffsets()` is neutral. This one nods slowly, and nods less while the head follows someone:

```python
import asyncio
import math

from reachy_mini_bridge import IdleMove, IdleOffsets, ReachyMiniBridge


class SlowNod(IdleMove):
    """A six-second nod: the head pitches 4 degrees down and back, starting at rest."""

    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(pitch_deg=4.0 * (1.0 - math.cos(2.0 * math.pi * t / 6.0)) / 2.0)

    def gaze_offsets(self, t: float) -> IdleOffsets:
        # While the head tracks a face, the move happens around the face: a smaller nod.
        return self.offsets(t).scaled(rotation=0.5)


async def main() -> None:
    config = {"backend": "fake", "motion": {"idle": "custom"}}  # the mode; code supplies the move
    async with ReachyMiniBridge.from_dict(config) as bridge:
        await bridge.set_idle_move(SlowNod)  # a factory: the loop builds a fresh SlowNod at every idle entry
        await bridge.set_motors_state("enabled")  # the loop commands the head once torque is on
        await asyncio.sleep(1.0)  # the nod plays
        print(bridge.idle, bridge.idle_move is SlowNod)  # custom True


asyncio.run(main())
```

Run as is, it plays on the offline fake; `"backend": "sim"` with `"daemon": {"spawn": "auto"}` shows it in the simulator, `"real"` on the robot. The same registration from a config assembled in code:

```python
from reachy_mini_bridge import ReachyMiniConfig
from reachy_mini_bridge.config import MotionSettings

config = ReachyMiniConfig(motion=MotionSettings(idle="custom", idle_move=SlowNod))
```

A JSON file names the mode (`"idle": "custom"`); `idle_move` is a Python-only field, since a file cannot carry code.

## What the loop asks of a move

- **A factory, called at every idle entry.** You register a zero-argument callable returning a fresh `IdleMove` — the class itself, or a function building one — and the loop calls it each time the custom idle is entered: at session start, after every emotion, after a mode change. `t` starts at 0 at every entry, so a randomised move draws a new plan each time.
- **`offsets(t)` runs on the motion thread at 60 Hz.** It returns promptly, never blocks, never touches the robot or the bridge. It is a pure function of `t` for a given instance and continuous.
- **It starts at rest.** The entry blend ends at rest at `offsets(0)`, so the move must have zero velocity there (the cosine above does; a sine starting at its steepest would step). `offsets(0)` itself may be any pose.
- **Amplitude is yours.** The loop checks every offset is finite; upstream's SDK clamps the joint ranges (head pitch and roll ±40°, yaw ±180°). The library's recorded emotions are a good yardstick for what reads as alive rather than agitated.
- **Under gaze, the move is motion around the person.** `gaze_offsets(t)` is composed onto the tracker's aim: a nod stays a nod at the person, a yaw sweep becomes a sweep around them. The default is neutral — the head sits on the aim and the antennas rest. Breathing's own `gaze_offsets` keeps its breath and antennas and tones its roaming down. Tracking wins by construction: an idle move cannot look away from a tracked face.
- **Checked at registration.** `set_idle_move` — and `MotionSettings(idle_move=...)` at session start — calls the factory once and raises `ValueError` when the value is not callable, the call raises, the result is not an `IdleMove`, or its `offsets(0.0)` / `gaze_offsets(0.0)` is not an `IdleOffsets` of finite numbers. A rejected factory changes nothing; the one registered before stays.
- **A move that fails while playing falls back to the hold.** If the factory or `offsets` raises on the motion thread, or returns a bad value, the loop logs one `WARNING` and plays the still neutral wherever it would have played your move, until a move is registered again (the same factory included).

## The mode and the move are independent

The registered factory is stored whatever the idle mode is and survives mode changes; it plays whenever the mode is `"custom"`. With the mode `"custom"` and nothing registered, the loop plays the hold. `set_idle("breathing")` puts breathing back without forgetting your move; `set_idle_move(None)` clears it. Registering while your move plays takes effect at once: the playing move fades out to neutral and the new one blends in. `presence` off stops every idle move, yours included: the loop then commands nothing between verbs.

## Testing one

On the `fake`, the motion loop streams every pose it commands to `bridge.robot.targets` (a list of `(head, antennas, body_yaw)` tuples, the head a 4×4 matrix), so a test reads the pitch of the head over time out of the recorded targets and asserts the nod's shape — the pattern is in [testing.md](testing.md) "Unit tests".
