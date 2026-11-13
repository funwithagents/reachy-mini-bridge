# Perception and tracking — the robot looks at people

How to make the robot detect the faces in front of it and follow one with its head, and how to consume what it sees: the complete configuration, the two reports, the events that wake you, and what the values do and do not promise. Contracts: [../reference/api.md](../reference/api.md); design: [specs/vision/user_perception.md](../../specs/vision/user_perception.md), [specs/motion/head_tracking.md](../../specs/motion/head_tracking.md), [specs/vision/camera.md](../../specs/vision/camera.md).

## The pipeline, in one paragraph

The bridge's **camera feed** (`bridge.camera`) is the one reader of the robot's camera: a thread pulls frames and publishes the newest with its time, for every consumer to sample (`latest()`) or follow (`frames()`). The **detection loop** runs a face detector over the feed's frames — the shipped `yunet`, upstream's own model run by the bridge, or one you register — once per new frame, off the event loop, gives every face a `track_id` it carries from frame to frame, and publishes a `FaceReport` on `bridge.faces`. The **head tracker** reads those reports, chooses whom to follow, and hands the motion loop an aim that it composes into the idle move — the robot looks at the person **and keeps breathing** — publishing its choice on `bridge.head_tracking`. The daemon's own face tracking is never armed. Everything is opt-in: with no detector named, nothing is detected and nothing tracks.

## Configuration

```json
{
  "face_detection": {"detector": "yunet", "enabled": true, "width": 640, "target_fps": null},
  "motion": {"tracking": {"enabled": true}}
}
```

Two things, and both are needed: a **detector** and a **switch**. `face_detection.detector` names what runs on the camera feed's frames — `"yunet"`, the shipped one (upstream's model run by the bridge: nothing to install, the weights download into the Hugging Face cache on first use), or `"custom"`, one you register from code ([custom-face-detector.md](custom-face-detector.md)); by default it is `null` and nothing is detected or tracked. The name alone starts nothing: `face_detection.enabled` runs the detector from session entry so `bridge.faces` reports who is there, `motion.tracking.enabled` makes the head follow the face the tracker chooses (and implies detection) — either switch on with no detector is a `ConfigError`. Neither needs motors: the head moves once they are `enabled`. At run time the same two switches are `set_face_detection(enabled)` and `start_head_tracking()` / `stop_head_tracking()`, or `set_tracking(settings)` for the whole tracking object. The two cost knobs, `face_detection.width` (the shipped detector's working width) and `face_detection.target_fps` (a ceiling on detections per second, for any detector — the lever on a wireless robot's 30 fps stream), are documented with their defaults in [../reference/configuration.md](../reference/configuration.md#face_detection--who-is-in-front-of-the-robot); ten seconds into a run the loop logs the detector's mean time and the rate it achieved, the numbers to tune them against.

**Where the camera comes from.** On a robot, its eye camera (the wireless robot's over WebRTC at 30 fps). On the sim, `daemon.camera.source`: `"sim"` renders the scene from the eye camera — under the viewer anywhere, headless on Linux only — and `"webcam"` relays your computer's camera, so the simulated robot sees and follows **you**, headless or not ([running-daemons.md](running-daemons.md) "You in front of the sim"). The sim's rendered scene has nothing to look at unless the test scene's portrait is shown ([testing.md](testing.md) "Testing tracking without a person").

## Whom the head follows

The tracker follows **one** face by its `track_id` and holds on to it: the biggest face in view, of at least a minimum size, taken when nobody is followed; kept while it is seen, whatever other faces appear. A followed face that disappears is waited for one second, the head holding toward where it was, and if it is not back the head turns to the biggest other face. After two seconds with no face at all the aim is withdrawn: the head eases back into the idle move, and the next face is acquired at once. `focus=False` (the default) composes the aim into the idle move — the head breathes and the antennas flick while it looks; `focus=True` holds the head exactly on the face, antennas still moving. Emotions play as recorded over all of it; the gaze fades back in afterwards.

### Choosing whom to follow

When your code knows whom the robot should look at — a person your recogniser named, the one an agent was told about — pin their face by its `track_id`:

```python
users = [u for u in bridge.users.value.users if u.seen]
leftmost = min(users, key=lambda u: u.face.x)
await bridge.start_head_tracking(track_id=leftmost.track_id)
```

The head turns to that face and follows it alone: it never switches to anyone else, a bigger face included. When the pinned user leaves — two seconds missing while the camera looks where they were — the head eases back into the idle move and tracking stays on, **watching**, the pin kept — nobody else is taken. Called again while tracking runs, `start_head_tracking` switches the target: another `track_id` pins that face, no `track_id` returns to the automatic choice above. Leaving `track_id` out always means automatic (where leaving `focus` out keeps focus), so a focus change while pinned names the face again: `start_head_tracking(focus=True, track_id=7)`. `stop_head_tracking()` clears the pin. An id that is not a user on `bridge.users` — never issued, not confirmed yet, or gone — raises `ValueError`; detection has to be running for anyone to be a user.

A pin follows a **track**, not a person: if the person's track breaks (hidden longer than the miss window, a jump the tracker could not follow) they come back under a new `track_id` and the pin ends in the loss. Watch the report and re-pin:

```python
async for state in bridge.head_tracking.changes():
    if state.pinned_track_id is not None and state.attention == "watching":
        face = find_my_person(bridge.faces.value)  # your recognition, by crop
        if face is not None:
            await bridge.start_head_tracking(track_id=face.track_id)
```

## The posture toward the person

While the head follows someone it also stands toward them, three behaviours each switched in `motion.tracking` (fields: [../reference/configuration.md](../reference/configuration.md#motion--what-the-robot-does-at-rest)), all on by default:

- **Mirroring.** The head rolls with the person's head roll, as a mirror does: both lean toward the same side of the room. A tilt under 3° reads as upright, and the roll is read against the head's own pose when the frame was taken, so the robot's own breathing or mirroring is never taken for the person's. It is as good as the detector's roll: YuNet at the default width (640) reads about two thirds of a tilt at 0.45 m from the Lite, more nearer, little beyond 0.6 m; raise `face_detection.width`, set `mirroring.gain` above 1, or register a detector that fits a head for more.
- **Approach.** The head sits `lean_mm` (15 mm) forward along the direction it looks: the robot comes a little toward the person.
- **Personal space.** Someone nearer than `distance_m` (0.40 m) pushes the head back, up to `max_back_mm` (20 mm) over `span_m` (0.20 m) of intrusion — a recoil, since the head's travel is a few centimetres. The distance is each face's `distance_m`, from its size in the image against an adult face's height, good to a few centimetres at desk range.

Someone standing well above the robot — a robot on a table, a person on their feet — is looked up at only as far as `motion.max_tilt_deg` (25°): the head keeps turning toward them and holds that tilt, their face near the top of the frame, since a head asked to tilt further stalls and stops turning altogether. The posture gives way to what the robot can reach: when a retreat and a roll far to one side would ask the head for a pose its kinematics cannot solve, the motion loop drops the idle move's motion first, then the offset, then the roll, and if need be turns the head only part of the way — the head keeps looking at you rather than freezing on its last pose. Each eases in within a second and freezes while the followed face is missing; the aim withdrawn, the posture resets and grows in again with the next person. Switched off at run time, a behaviour eases out instead of stepping:

```python
from dataclasses import replace

settings = bridge.tracking
await bridge.set_tracking(replace(settings, approach=replace(settings.approach, enabled=False)))
```

`bridge.head_tracking.value.roll_deg` and `offset_mm` read the posture the head holds.

`bridge.attention` is the derived state: `"engaged"` while a face is followed, `"watching"` while tracking is on and nobody is, `None` when tracking is off. The robot's own moves do not count as the person leaving: an emotion that turns the camera away from the face for its length leaves the head engaged, and the person keeps their `track_id` when the head comes back onto them. Only the time the camera frames where they were counts toward the 2 s after which the head is handed back.

## Tuning the frame delay

A face is seen in a frame the camera took a little before the bridge receives it, from wherever the head was then. The tracker aims each face against the head's pose at the frame's arrival minus `motion.tracking.frame_delay_s`: `0` by default, which on a Lite over USB and on the simulator is within a few hundredths of a second of the truth. A camera path that takes longer, such as a wireless robot's stream, a slow machine or a heavy detector upstream of the bridge, wants a longer delay. The head's behaviour tells you which way to go:

| What the head does | The delay is | Change |
|---|---|---|
| It turns onto someone, swings past them and comes back, or hunts around a person standing still | too short: the head's own motion since the frame is counted twice | raise it |
| It trails someone walking sideways and advances in steps, pausing and surging | too long: the aim mixes in where the head was before | lower it |
| It turns onto you in one movement and follows a steady walk smoothly | right | keep it |

Change it by 0.02 s at a time, standing about a metre away. Stand still to one side, let the head settle on you, then step across once quickly and once slowly. Raise the delay while it overshoots when you stop. Lower it while it steps as you walk. The setting applies on the next aim, so you can change it while the session runs:

```python
from dataclasses import replace

await bridge.set_tracking(replace(bridge.tracking, frame_delay_s=0.04))
```

`"estimate"` has the tracker estimate the delay from the head's own turns. It needs nothing measured, but on a robot it drifts toward its 0.5 s limit while the person and the head both move, which is the stepping in the table above.

## The base toward the person

While the head follows someone well to one side, the base turns toward them too (`motion.base_following`, on by default — fields: [../reference/configuration.md](../reference/configuration.md#motion--what-the-robot-does-at-rest)). The body yaw the loop chooses is a frame every move plays in: the idle move breathes around it, an emotion played meanwhile is performed toward the person rather than toward the front, and once the person leaves the base keeps facing where they were for `hold_s` (20 s) before easing back to the front. Two reasons: a robot that only turns its head at 60° reads as distracted, and the head's workspace is the body's — with the base turned onto the person the whole posture stays within reach at the far sides, where a front-facing base loses it.

The base turns only for a turn worth making: once the person's direction is more than `deadband_deg` (20°) from where the base points, it waits `lag_s` (0.8 s), by when the head is on them, then eases onto them and catches up fully, so they move another 20° before the next turn: the head leads, the body follows. Every turn is eased, never stepped, and an emotion never swings the base back to the front. Switch it off at run time the same way as a posture behaviour:

```python
from dataclasses import replace

await bridge.set_base_following(replace(bridge.base_following, enabled=False))
```

`bridge.body_yaw_deg` reads where the base points, in degrees, positive to the left.

## The three reports

**`bridge.faces`** — a `FaceReport`: every face the detector reports, in `track_id` order (oldest track first), each with its normalised position (`x`, `y` in [-1, 1], the nose when known), its `size` (box height as a fraction of the frame), its `distance_m` from the camera, its pixel `bbox`, its head angles in **radians** (`roll`; `pitch` and `yaw` from a detector that fits a head), and its `track_id`; plus `active` (a detector is looking), `source` (`"yunet"` / `"custom"`), `ts`, and `frame` — the `CameraFrame` the faces were found in. The report is about detection: it singles nobody out, and it follows the frame — a person the robot's own move turned the camera away from is not in it.

**`bridge.users`** — a `UsersReport`: the people in front of the robot, whether or not the camera sees them right now. Each `User` is a confirmed track the detection loop holds — its `track_id`, whether it was `seen` on the latest frame, whether the camera was looking where they were (`in_view`), `last_seen_ts`, and the `face` of their last sighting. A track joins once seen three times over 0.3 s, so a one-frame false detection never arrives; it leaves when the camera has looked where the person was and missed them for about 2 s (`"left"`), when nobody has seen them for 20 s (`"unseen"`), or when detection stops (`"stopped"`). An emotion that turns the head away keeps everyone present.

**`bridge.head_tracking`** — a `HeadTrackingReport`: `active`, `focus`, `attention`, the `track_id` of the face the head follows (`None` unless engaged), the `pinned_track_id` you pinned (`None` while the tracker chooses), and the posture it holds (`roll_deg`, `offset_mm`).

All three are `Observable`s, readable at any time and outliving the session — their inactive values before it starts and after it ends ([../reference/api.md](../reference/api.md) "Reading and subscribing").

### Consuming them

Four separate recipes — each is a fragment for a place in your program, not steps of one script (the two `async for` loops run forever, each in a task of its own that you cancel at shutdown — [../reference/api.md](../reference/api.md#lifecycle) "Applications own their tasks").

**Read the newest state** — any time, from any thread, refreshed on every detection:

```python
report = bridge.faces.value
if report.active:
    print(f"{len(report.faces)} people in view")
```

**Be told when someone arrives or leaves** — `every=True`, so a greeting that takes seconds misses no one who arrived meanwhile:

```python
async for report in bridge.users.changes(every=True):
    for track_id in report.arrived:
        print(f"hello, {track_id}")
    for departure in report.left:
        print(f"goodbye, {departure.track_id} ({departure.reason})")
```

**Be told when the frame's faces change** — a face entering or leaving the frame, someone replacing someone, detection starting or stopping — never "a face moved". The robot's own moves count here: an emotion turning the head away empties the frame.

```python
async for report in bridge.faces.changes():
    if not report.active:
        print("not looking")
    else:
        print(f"now {len(report.faces)} in the frame")
```

**Be told whom the head follows** — the tracker's event, not the detection's:

```python
async for state in bridge.head_tracking.changes():
    print(state.attention, state.track_id)
```

**Be told when the person comes close** — `in_personal_space` flips, a published change on the same report, when the followed person enters the personal space (nearer than `motion.tracking.personal_space.distance_m`) and when they leave it (`exit_margin_m` beyond, 5 cm by default at 0.40 m, so standing at the boundary never flickers it). It is reported whether or not the head's retreat is enabled:

```python
close = False
async for state in bridge.head_tracking.changes():
    if state.in_personal_space != close:
        close = state.in_personal_space
        print("too close" if close else "back at a comfortable distance")
```

**Wait until somebody is there** — reacts to publications, an arrival, not a refresh:

```python
report = await bridge.users.wait_for(lambda r: r.active and len(r.users) > 0)
```

A subscriber that falls behind gets the latest value, not a backlog — `changes(every=True)` gives it every published report in order instead ([../reference/api.md](../reference/api.md) "Reading and subscribing"); a face gone from the frame is published once its absence has held for 0.3 s, so a detector's blink does not fire *left the frame*. A face **moving** never wakes a subscriber: a graph that wants every observation samples `faces.value` at its own rate and skips repeats on `frame_id`, the way it would any latest-value upstream.

### Enriching a face

`report.frame` is the frame the faces were found in, kept with them — where `bridge.camera.latest()` has usually moved on. Crop with the box, in pixels of that frame, and copy before you draw:

```python
report = bridge.faces.value
if report.active and report.frame is not None:
    h, w = report.frame.image.shape[:2]
    for face in report.faces:
        x, y, bw, bh = (int(v) for v in face.bbox)
        crop = report.frame.image[max(0, y) : min(h, y + bh), max(0, x) : min(w, x + bw)].copy()
        ...  # recognise, read an expression — your model, joined to the bridge's faces by face.track_id
```

Who it is stays yours: keep a map from `track_id` to the identity your model found, read `bridge.head_tracking.value.track_id` to know which of them the head is looking at, and pin the one it should look at ("Choosing whom to follow" above).

## What the values promise, and what they do not

- **`active=False` means unknown, not empty.** No detector is looking — detection off, or no camera frame for five seconds (a headless macOS sim has no camera). Such a report always carries no faces; never read it as nobody there. The loop logs one warning when it loses the camera and reports active again on the next frame.
- **A `track_id` is continuity, not identity.** The same positive integer follows a person while the detector keeps seeing them, through a short run of missed frames, and is never reused. Two people crossing, or someone leaving and returning, can change ids. Ids are stable *enough* to join your results to; they are not recognition.
- **Index zero is not the one the head follows.** The list is ordered by track age; the followed face is `head_tracking.value.track_id`.
- **Angles are radians, offsets are degrees.** A face's `roll` / `pitch` / `yaw` are radians as seen from the camera; the motion API's offsets (`IdleOffsets`) are degrees and millimetres ([../reference/api.md](../reference/api.md) "Units").
- **Frames are shared and read-only.** `frame.image` is the feed's array, seen by every consumer: copy before drawing on it.
- **Tracking moves the head only with torque on.** It is a mode: it holds, and the head starts moving once motors are `enabled`.
- **The sim camera's geometry is handled.** The bridge's tracker aims with its own pinhole model of the rendered eye camera, and treats a webcam as fixed at the robot's resting eye, with the source and field of view the daemon reports ([running-daemons.md](running-daemons.md) "You in front of the sim"), so the head converges on the face in the sim as on a robot.

## Beside a vision graph

A vision pipeline of your own — hands, gestures, a recogniser — samples `bridge.camera` directly: it has the shape a latest-value runtime expects (`latest()` returning items with `frame_id`, `ts`, `image`), with no adapter and no second reader of the camera. Faces reach your graph through `bridge.faces` rather than a second detector; a detector you want the **head** to follow is registered with the bridge as a `custom` detector ([custom-face-detector.md](custom-face-detector.md) "Beside a vision graph").

## Seeing it in the simulator

With the viewer open, `daemon.sim_displays` draws the camera stream in the window's corner (`camera_overlay`), a line along the robot's gaze (`robot_gaze`) and an ellipsoid per detected face where the bridge places it, green for the followed one (`face_markers`) — viewer-only, invisible to the camera and the detector ([running-daemons.md](running-daemons.md)). Once tracking has settled, the gaze line passes through the followed face's marker.
