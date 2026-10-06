# The bridge on Linux

What a Linux machine needs to run the bridge, and a daemon — the sim's, or a Lite's over USB — on it. A reference note like [upstream-sdk-notes.md](../internals/upstream-sdk-notes.md), not a spec; the platform statement is [../specs/project.md](../../specs/project.md) "Platforms".

macOS and Windows get GStreamer from `reachy_mini`'s wheels; on Linux it is the system's, and the bridge's CI runner is where the recipe below was worked out (Ubuntu 24.04, the sim, headless — [../specs/testing/ci.md](../../specs/testing/ci.md)). Upstream's own guide, [GStreamer installation](https://github.com/pollen-robotics/reachy_mini/blob/main/docs/source/SDK/gstreamer-installation.md), is the reference; this is what the bridge adds to it. The packages and the webrtc plugin apply to **every daemon on the machine** — the sim, or a Lite plugged in over USB: the media server is the same code for both — and the packages to every client, a wireless robot's included. The headless camera's Mesa and the null sink are the sim's concerns; a Lite brings its own camera and sound card. What CI verifies is the sim; a Lite on Linux has not been run by us, and its statements here follow from upstream's code, not from a test.

**Packages.** `reachy_mini` depends on PyGObject, which has no Linux wheel in the pinned range and builds against the girepository and cairo headers, and on the system GStreamer with its introspection data:

```
sudo apt-get install libgirepository1.0-dev libcairo2-dev pkg-config \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-pulseaudio gstreamer1.0-tools gstreamer1.0-nice libnice10
```

`gstreamer1.0-nice` / `libnice10` are the ICE library the daemon's WebRTC server needs. Ubuntu 22.04's GStreamer is too old (1.22 or newer is required; see upstream's guide for the PPA).

**The Rust webrtc plugin — for any daemon on this machine.** The daemon's media server hard-requires the `webrtcsink` element of the GStreamer Rust plugins (`libgstrswebrtc.so`), and without it the daemon starts but serves **no camera and no daemon-side audio** (`Failed to initialize media server: Failed to create webrtcsink element` in its log). No distribution ships it. Two routes: build it with cargo as upstream's guide says (several minutes, the `webrtc` plugin of `gst-plugins-rs` 0.14.5, installed under `/opt/gst-plugins-rs` and put on `GST_PLUGIN_PATH`), or take the prebuilt x86_64 binary Pollen publishes in its public desktop-app repository, which upstream's own CI and the bridge's install, pinned to a commit and a sha256 — see the `webrtc-plugin` composite action in upstream's `.github/actions`, or the bridge's [workflow](../../.github/workflows/ci.yml). Check with `gst-inspect-1.0 webrtcsink`. A client that only connects to a wireless robot's own daemon does not need it.

**The headless sim's camera** renders offscreen through Mesa's EGL (`libegl1 libgl1-mesa-dri`; `libosmesa6` for `MUJOCO_GL=osmesa`), as [running-daemons.md](running-daemons.md) "Headless sim" says. The viewer opens under the plain interpreter on Linux (`mjpython` is macOS only), with a display.

**No software echo cancellation on Ubuntu 24.04.** Its `gstreamer1.0-plugins-bad` lacks the `webrtcdsp` element, so a sim or USB robot's audio on such a host runs without the software AEC the bridge's client enables elsewhere (`Cannot enable webrtcdsp` in the log, harmless): a conversation through real speakers and a mic will hear itself. A robot's own XVF3800 does the cancellation in hardware and is unaffected.

**No sound card at all** (a server, a CI runner): the daemon logs that audio is unavailable and the bridge's audio works for nobody. A PulseAudio null sink gives it a device whose monitor loops playback back to capture — the recipe is in [testing.md](testing.md) "A Linux box without a sound card".

