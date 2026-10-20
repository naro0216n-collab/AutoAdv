# AutoAdv — Windows use and verification

## Prerequisites

Windows 10 build 19041 or newer; Python 3.11; .NET SDK 9 for the Process Loopback helper; existing Irodori-TTS-Server installation, an installed native LunaHook Host/Hook build for the relevant target game, and local ASR model/tokens for native speech synchronization. Tesseract with Japanese trained data is needed **only** for an OCR profile. No separate running LunaTranslator is part of the normal HOOK workflow. AutoAdv does not download/update engines or models or modify other programs' installations on startup.

```powershell
py -3.11 -m pip install -e ".[live]"
dotnet build integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj -c Release
py -3.11 -m autoadv
```

## Normal GUI operation

1. Register the game and choose its profile. A new profile selects **HOOK** by default. For that path, configure a managed LunaHook Host, choose and preview one verified HOOK, save its selection and confirm the actual target PID. Multiple independent HOOKs cannot yet be accepted as one verified combined utterance; do not treat separate speaker and text outputs as synchronized. For an unsupported HOOK, manually select **OCR**, an already installed Tesseract executable, language (e.g. `jpn`) and the target client-area rectangle. OCR does not fall back to HOOK, or vice versa, without explicit intervention.
2. Configure speaker/body extraction, ruby and name reading, voice routing and SKIP in the one game/profile record. The editable preview and active `Utterance` remain distinct. Configure the installed Irodori server root once; subsequent AutoAdv starts connect or start the installed server, then update available voice IDs. An absent voice ID is never silently replaced.
3. Select the target window and audio helper/model/tokens, along with timing and input method. HOOK requires **no** screen-marker calibration. For optional OCR, select a Tesseract installation and specify its client-area text rectangle; OCR is observation/audio-only by default.
4. Start monitoring without granting input. When a selected HOOK is receiving text, you can explicitly authorize auto-input. Audio attribution and completion precede each input, and the fresh guard checks target PID/HWND, foreground, capture health, TTS/native state and user intervention.
5. After one input, AutoAdv waits for a changed selected-HOOK candidate in the same session. A timer or same text cannot cause another input. A timeout, manually pressed key/button, process/window/source change or unresolved audio stops automation. Choices/special screens that cannot be identified are handled manually; the application does not promise game-state recognition.

Continuous advancement and choice-screen behavior must be verified with each real game; a changed HOOK candidate is not proof of a fully observed game transition.

## Diagnostic validation

```powershell
py -3.11 -m unittest discover -s tests -q
py -3.11 -m autoadv --help
```

Windows acceptance must separately check 32/64-bit Host/Hook build and attachment, actual HOOK events and sources, optional installed OCR recognition, native Process Loopback and VAD/ASR, Irodori playback completion, repeated/split/same text, late audio, same/partial HOOK text, manual choice intervention, focus loss, profile and process switches, and one-input-per-candidate behavior. Passing synthetic tests or CI is not evidence that a particular game is safely operable.

## Imported legacy settings

Old Luna `savegamedata_5.3.1.json` can be read **without modifying Luna's original file**. Data are transferred explicitly to the AutoAdv catalog; there is no parallel Luna settings authority. Old screen templates remain in the preserved original backup but are not imported as operational settings. An old third-party Luna patch or bridge must not run concurrently with the managed Host. No external process is killed, detached or overwritten automatically.
