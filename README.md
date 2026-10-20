# AutoAdv

Windows PySide6 GUI application for game-text acquisition, selective Irodori TTS, native game-audio synchronization and conservative, explicitly authorized advancement.

**HOOK is the default text source**, using a managed LunaHook Host. It does not require a separate running LunaTranslator or its public WebSocket. An existing Tesseract installation is an **optional, explicitly selected OCR source** for games where HOOK cannot be used. HOOK and OCR do not run in parallel to create or merge dialogue. Game/profile-specific rules feed a single shared `Utterance` interpretation, voice routing and Controller.

## Starting

```powershell
py -3.11 -m pip install -e ".[live]"
dotnet build integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj -c Release
py -3.11 -m autoadv
```

Select an installed Irodori-TTS-Server folder in the GUI once; AutoAdv reuses a running server or launches the existing environment on future starts and reads available Voice IDs. It does not install models, convert references or install Tesseract for you.

Register a game and optional additional profiles. HOOK profiles select managed HOOK candidates and their order, then configure text parsing, readings, Voice and audio synchronization. OCR profiles must explicitly choose OCR, set an installed OCR executable, language and the client-area rectangle. Both use the same per-game/profile settings store and text interpreter. Audio capture, ASR files, target window and game-input configuration are supplied in the GUI. Existing standalone monitor JSON files may be imported explicitly; they are not a second settings authority.

HOOK text is matched against native game speech through Process Loopback, VAD and ASR. Native voice takes priority; Irodori TTS is used selectively when needed, and audio playback must finish before an explicitly authorized advance. The Controller checks window/PID, focus, live capture, manual intervention and previous input before pressing the configured key. It then waits for a **new, different candidate from the same selected HOOK and session**, not a generic screen-state proof or artificial game-transition ACK. Identical output or time alone cannot repeat a keypress. OCR provides text/audio processing but does not automatically inject input.

AutoAdv does **not** try to recognize every menu, choice screen or game state. Choices that are not identifiable from the HOOK may require manual pause/selection and explicit reauthorization. Per-game continuous advancement and audio synchronization still require verification on Windows real games; unit tests cannot establish compatibility.

See [OPERATING.md](OPERATING.md) for operation and validation, and `docs/integration-spec.md` for the canonical product contract.

## Tests

```sh
python -m unittest discover -s tests -q
python -m autoadv --help
```

Normal development targets `develop` through the standard Publish Gateway. `main` and version changes require explicit approval.
