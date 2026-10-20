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

Game-screen calibration is **not required to acquire text, prepare an `Utterance`, or listen to the game's audio**. Auto-input is a separate opt-in permission. Receipt of text, playback completion and UI focus do not prove that an ordinary advance is safe or that the previous input completed. Without **independently validated game-specific positive evidence**, auto-input remains blocked. Optional image templates only authorize when the game provides a uniquely verified affirmative `advance_allowed` indicator; Removed dialogue/readiness image markers are not used for text confirmation. Missing choice or transition evidence never means it is safe to send input. Repeated input is prohibited until a separate verified transition ACK.

**Current development limitation:** no general real-game provider for independent transition ACK or nonvisual positive advancement has been validated. The source, TTS and audio pathways can operate without images, but safe continuous automatic input is not yet confirmed on real games. The Windows x86/x64 Luna native binaries, injection, OCR engine integration and audio synchronization require Windows CI and real-game verification; passing fixture/unit tests is not sufficient.

See [OPERATING.md](OPERATING.md) for operation and validation, and `docs/integration-spec.md` for the canonical product contract.

## Tests

```sh
python -m unittest discover -s tests -q
python -m autoadv --help
```

Normal development targets `develop` through the standard Publish Gateway. `main` and version changes require explicit approval.
