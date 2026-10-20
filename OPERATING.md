# AutoAdv — Windows operation and game calibration

## Install and start

Windows 10 build 19041 or later, Python 3.11+, .NET SDK 9, LunaTranslator and an **already installed** Irodori-TTS-Server are required for the applicable features. Japanese ASR model files and the .NET process-loopback helper must be installed independently. AutoAdv does not install the server, download models, convert voice files or perform dependency synchronization.

```powershell
py -3.11 -m pip install -e ".[live]"
dotnet build integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj -c Release
py -3.11 -m autoadv
```

1. In Luna, select/merge the HOOK(s) needed for this title. For a HOOK1 speaker + HOOK2 text title, select speaker then text and separate by a newline. Confirm the actual merged original in the AutoAdv preview. Do not assume Luna provides stable line IDs, speech timing, PID or voice metadata. Disable the game's own Auto and Luna `ttsautoforward`.
2. In AutoAdv, select the installed Irodori-TTS-Server root. Its existing `.venv` or `venv` Python and installed package are used; the configured root is saved. On that selection and each next AutoAdv launch, the app connects to the API or starts the server from the chosen folder. It will not terminate external servers. Check the Voice IDs and any API/local difference. Select specific IDs or `SKIP`; no voice assignment silently changes when an ID vanishes.
3. Set game-specific structure rules (`(?P<speaker>...)(?P<body>...)`), optional regex conditions and priority, name/ruby substitutions and voice routing. The right pane shows original → speaker/body → TTS/ASR → voice using the same interpreter as runtime. Save to a single settings file in `%APPDATA%\AutoAdv\settings.json` (atomic replacement).
4. Calibrate `examples/live-profile.example.json` for the game's real PID, HWND, Process Loopback helper, Japanese ASR model and chosen input key. Calibrate `examples/screen-profile.example.json` with **positive** images for ordinary dialogue and display-complete markers, and visual blocker for manual choices (optionally transition). The supplied coordinates and placeholder filenames are not working game calibrations. `manual_block` can apply to choice/map/menu/etc. Inconsistent, absent or unreadable positive evidence means no input.
5. Start game monitoring, verify the game/HWND and current Luna original actually correspond, then separately press **auto-advance authorize**. Startup, monitoring, TTS playback test, and server connection alone must not issue game input. Manual choice detection only suspends input until a verified new ordinary utterance; explicit pause, disconnected WS, game switch or error requires a fresh session and explicit permission.

Do not enable `allow_heuristic_absence` without real-game latency/BGM/SE/no-voice calibration. Process-loopback failure is NOT voice absence. Late native onset cancels TTS, audio-device playback (not HTTP synthesis) determines TTS completion, and no second input is allowed until a verified transition. Logs include generation, presentation path, output/ACK, stop reason and text hash; not raw audio/video. Input-free diagnostics:

```powershell
py -3.11 -m autoadv luna-origin --count 4
py -3.11 -m autoadv tts-smoke --voice YOUR_ID --play
py -3.11 -m autoadv run-windows --config examples/live-profile.example.json
py -3.11 -m autoadv audit-evaluate logs/autoadv.jsonl
```

## Undo a previously installed Luna patch

Older AutoAdv versions modified local Luna files via `scripts/luna_bridge_patch.py`. The patch is **not installed or used** by this release. Before using the Luna public WebSocket, restore the patched LunaTranslator files from the corresponding **unmodified Luna release or your own original backup** (or reinstall that Luna version), then use Luna's built-in original-text WS. Do not keep the old AutoAdv injected bridge module/startup hook or old UDP bridge configuration. AutoAdv does not attempt risky automated modifications of Luna installation files.

## Acceptance and limits

Run unit/integration tests and Fast CI for reproducible contracts. Verify with a Windows machine and a real game: speaker + body HOOK merge, voiced/unvoiced lines, late native audio, SKIP, symbolic text, manual choice return, explicit pause, unknown/same repeated line stop, automatic Irodori relaunch and Voice UI, device-drain and no duplicate inputs. Linux tests, fixture calibrations and green CI cannot establish real-game correctness.
