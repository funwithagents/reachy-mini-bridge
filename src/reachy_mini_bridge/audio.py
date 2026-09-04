"""Audio & media session: routing TTS out and exposing the mic through the robot.

**Placeholder — design only.** Specified by [specs/audio.md](../../specs/audio.md)
(Status: Draft). No implementation yet; see the spec for the intended design —
the shared media session, the bridge-owned pluggable ``SpeechSynthesizer`` for
speech output (``tts-engine`` as the default adapter), the echo-cancelled
microphone stream callers attach their own ASR to, and the XVF3800
echo-cancellation config.
"""
