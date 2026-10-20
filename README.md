# AutoAdv

Windows PySide6 GUI application for visual-novel voice-synchronized advancement.
LunaTranslator supplies post-processed original text via its public `/api/ws/text/origin` WebSocket; AutoAdv owns per-game structure parsing, ruby/name readings, selective Irodori TTS, native audio attribution, and all input authorization. The shared Utterance is used for TTS, ASR and the GUI preview.

## Starting

```powershell
py -3.11 -m pip install -e ".[live]"
py -3.11 -m autoadv
```

In the GUI, select the existing Irodori-TTS-Server folder **once**. AutoAdv connects to an existing compatible Voice API or launches the installed virtual environment immediately, and again on subsequent app launches. It reads local `voices` files and aliases, then prioritizes IDs actually reported by `GET /v1/audio/voices`. It terminates only a server process that AutoAdv launched itself. Selecting a folder never authorizes game input.

Select a game, edit its named-group `speaker`/`body` structure regexes, replacements, readings and speaker-to-Voice assignments; save them, and preview Luna's unmodified original text alongside the exact runtime Utterance. The default is **auto-advance disabled** on every app launch. Positive target/HWID identity, game dialogue and readiness indicators, and explicit user authorization are mandatory; STOP/UNKNOWN keeps input disabled. Normal manual choice selection may return to automatic mode only after positively verified new normal text; explicit pause and runtime errors never do.

HOOK discovery, selection, sequence and merge occur in Luna. AutoAdv does not patch Luna, listen on UDP, read OCR game text or reconstruct invisible line IDs. Game original Auto and Luna `ttsautoforward` must both be disabled.

Calibration of game-specific positive visual evidence and native audio is required before automatic advancement. See [OPERATING.md](OPERATING.md) and `examples/`.

## Tests

```sh
python -m unittest discover -s tests -q
python -m autoadv --help
```

Real-game Windows Process Loopback/ASR/TTS playback, Luna HOOK results and input accuracy remain separate real-machine acceptance tests; Linux unit tests cannot validate them. Canonical requirements: `docs/README.md`, `docs/development-principles.md`, `docs/integration-spec.md`. Publish only to `develop` through the standard Gateway; `main` remains approval-gated.
