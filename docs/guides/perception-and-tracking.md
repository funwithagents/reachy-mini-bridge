# Perception and tracking — the robot looks at people

How to make the robot detect the faces in front of it and follow one with its head, and how to consume what it sees: the complete configuration, the two reports, the events that wake you, and what the values do and do not promise. Contracts: [../reference/api.md](../reference/api.md); design: [specs/vision/user_perception.md](../../specs/vision/user_perception.md), [specs/motion/head_tracking.md](../../specs/motion/head_tracking.md), [specs/vision/camera.md](../../specs/vision/camera.md).

## The pipeline, in one paragraph

The bridge's **camera feed** (`bridge.camera`) is the one reader of the robot's camera: a thread pulls frames and publishes the newest with its time, for every consumer to sample. The **detection loop** runs a face detector over the feed's frames — the shipped `yunet`, upstream's own model run by the bridge, or one you register — once per new frame, off the event loop, gives every face a `track_id` it carries from frame to frame, and publishes a `FaceReport` on `bridge.faces`. The **head tracker** reads those reports, chooses whom to follow, and hands the motion loop an aim that it composes into the idle move — the robot looks at the person **and keeps breathing** — publishing its choice on `bridge.head_tracking`. The daemon's own face tracking is never armed. Everything is opt-in: with no detector named, nothing is detected and nothing tracks.

## Configuration

```json
{
  "face_detection": {"detector": "yunet", "enabled": true, "width": 320, "target_fps": null},
  "motion": {"tracking": true}
}
```

| Field | Default | Effect |
|---|---|---|
| `face_detection.detector` | `null` | `"yunet"` — the shipped detector, nothing to install, the weights download into the Hugging Face cache on first use; `"custom"` — yours, registered from code ([custom-face-detector.md](custom-face-detector.md)); `null` — no detection and no tracking (either switch on is then a `ConfigError`) |
| `face_detection.enabled` | `false` | Run the detector from session entry so `bridge.faces` reports who is there, whether or not the head follows. Needs no motors |
| `motion.tracking` | `false` | The head follows the face the tracker chooses, from session entry. Implies detection. Needs no motors — the head moves once they are `enabled` |
| `face_detection.width` | `320` | The width the shipped detector works at: its cost against its precision (640 quadruples the cost and halves the landmark error; `null` the full frame) |
| `face_detection.target_fps` | `null` | A ceiling on detections per second, for any detector; `null` detects on every new frame (10 a second on a local daemon, 30 on a wireless robot's stream — the lever there) |

The name alone starts nothing; one of the two switches does. At run time: `set_face_detection(enabled)`, `start_head_tracking(focus=False)` / `stop_head_tracking()`. Ten seconds into a detection run the loop logs one line with the detector's mean time and the rate it achieved — the numbers to tune `width` and `target_fps` against on your machine.

**Where the camera comes from.** On a robot, its eye camera (the wireless robot's over WebRTC at 30 fps). On the sim, `daemon.camera.source`: `"sim"` renders the scene from the eye camera — under the viewer anywhere, headless on Linux only — and `"webcam"` relays your computer's camera, so the simulated robot sees and follows **you**, headless or not ([running-daemons.md](running-daemons.md) "You in front of the sim"). The sim's rendered scene has nothing to look at unless the test scene's portrait is shown ([testing.md](testing.md) "Testing tracking without a person").

## Whom the head follows

The tracker follows **one** face by its `track_id` and holds on to it: the biggest face in view, of at least a minimum size, taken when nobody is followed; kept while it is seen, whatever other faces appear. A followed face that disappears is waited for one second, the head holding toward where it was, and if it is not back the head turns to the biggest other face. After two seconds with no face at all the aim is withdrawn: the head eases back into the idle move, and the next face is acquired at once. `focus=False` (the default) composes the aim into the idle move — the head breathes and the antennas flick while it looks; `focus=True` holds the head exactly on the face, antennas still moving. Emotions play as recorded over all of it; the gaze fades back in afterwards.

`bridge.attention` is the derived state: `"engaged"` while a face is followed, `"watching"` while tracking is on and nobody is, `None` when tracking is off.

## The two reports

**`bridge.faces`** — a `FaceReport`: every face the detector reports, in `track_id` order (oldest track first), each with its normalised position (`x`, `y` in [-1, 1], the nose when known), its `size` (box height as a fraction of the frame), its pixel `bbox`, its head angles in **radians** (`roll`; `pitch` and `yaw` from a detector that fits a head), and its `track_id`; plus `active` (a detector is looking), `source` (`"yunet"` / `"custom"`), `ts`, and `frame` — the `CameraFrame` the faces were found in. The report is about detection: it singles nobody out.

**`bridge.head_tracking`** — a `HeadTrackingReport`: `active`, `focus`, `attention` and the `track_id` of the face the head follows (`None` unless engaged).

Both are `Observable`s, readable at any time and outliving the session — their inactive values before it starts and after it ends ([../reference/api.md](../reference/api.md) "Reading and subscribing").

### Consuming them

```python
# The newest state, any time, from any thread: refreshed on every detection.
report = bridge.faces.value
if report.active:
    print(f"{len(report.faces)} people in view")

# Events: someone appeared / left, detection started / stopped. Never "a face moved".
async for report in bridge.faces.changes():
    if not report.active:
        print("not looking")
    else:
        print(f"now {len(report.faces)} in view")

# Whom the head follows — the tracker's event, not the detection's.
async for state in bridge.head_tracking.changes():
    print(state.attention, state.track_id)

# Wait until somebody is there (reacts to publications: a count change, not a refresh).
report = await bridge.faces.wait_for(lambda r: r.active and len(r.faces) > 0)
```

A subscriber that falls behind gets the latest value, not a backlog; the count's drop is published once it has held for 0.3 s, so a detector's blink does not fire *left*. A face **moving** never wakes a subscriber: a graph that wants every observation samples `faces.value` at its own rate and skips repeats on `frame_id`, the way it would any latest-value upstream.

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

Who it is stays yours: keep a map from `track_id` to the identity your model found, and read `bridge.head_tracking.value.track_id` to know which of them the head is looking at.

## What the values promise, and what they do not

- **`active=False` means unknown, not empty.** No detector is looking — detection off, or no camera frame for five seconds (a headless macOS sim has no camera). Such a report always carries no faces; never read it as nobody there. The loop logs one warning when it loses the camera and reports active again on the next frame.
- **A `track_id` is continuity, not identity.** The same positive integer follows a person while the detector keeps seeing them, through a short run of missed frames, and is never reused. Two people crossing, or someone leaving and returning, can change ids. Ids are stable *enough* to join your results to; they are not recognition.
- **Index zero is not the one the head follows.** The list is ordered by track age; the followed face is `head_tracking.value.track_id`.
- **Angles are radians, offsets are degrees.** A face's `roll` / `pitch` / `yaw` are radians as seen from the camera; the motion API's offsets (`IdleOffsets`) are degrees and millimetres ([../reference/api.md](../reference/api.md) "Units").
- **Frames are shared and read-only.** `frame.image` is the feed's array, seen by every consumer: copy before drawing on it.
- **Tracking moves the head only with torque on.** It is a mode: it holds, and the head starts moving once motors are `enabled`.
- **The sim camera's geometry is handled.** The bridge's tracker aims with its own pinhole model of the rendered eye camera, and treats a webcam as fixed at the robot's resting eye (`daemon.camera.hfov_deg` sets its field of view), so the head converges on the face in the sim as on a robot.

## Beside a vision graph

A vision pipeline of your own — hands, gestures, a recogniser — samples `bridge.camera` directly: it has the shape a latest-value runtime expects (`latest()` returning items with `frame_id`, `ts`, `image`), with no adapter and no second reader of the camera. Faces reach your graph through `bridge.faces` rather than a second detector; a detector you want the **head** to follow is registered with the bridge as a `custom` detector ([custom-face-detector.md](custom-face-detector.md) "Beside a vision graph").

## Seeing it in the simulator

With the viewer open, `daemon.sim_displays` draws the camera stream in the window's corner (`camera_overlay`), a line along the robot's gaze (`robot_gaze`) and an ellipsoid per detected face where the bridge places it, green for the followed one (`face_markers`) — viewer-only, invisible to the camera and the detector ([running-daemons.md](running-daemons.md)). Once tracking has settled, the gaze line passes through the followed face's marker.
