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
3. Select the target window and audio helper/model/tokens, along with timings and input method. Saving the monitor settings **does not require visual markers**. Missing markers do not disable HOOK/OCR preview or audio presentation.
4. If a particular game supplies reliable, positive, game-specific state indicators, optionally capture image templates from the GUI. `advance_allowed` must represent an independently validated ordinary-advance condition, **not** a generic textbox. `manual_block` or `transition` can deny permission; `dialogue` and `text_ready` support diagnosis only. Verify markers against real gameplay, choice screens and resolution changes. Screenshots are relative to the original client size and cannot be reused silently after resizing.
5. Start monitoring independently from the explicit auto-input authorization. The controller must verify correct game/PID/HWND and focus, healthy audio, completed presentation, a current independent advance grant, no manual intervention and no pending transition ACK before sending any input. Text arrivals do not provide advance grants or ACKs. If the currently selected game has no reliable grant and transition proof, monitor text and listen to audio **without enabling auto-input**. A failed source, manual pause, game/profile switch or window change invalidates pending work.

The input-safe path currently lacks a verified general game-transition ACK provider. Consequently, continuous automatic progression must not be represented as validated even when isolated text acquisition and TTS work. Game-specific integrations need real engine evidence; a timed assumption or mere changed text is not an ACK.

## Diagnostic validation

```powershell
py -3.11 -m unittest discover -s tests -q
py -3.11 -m autoadv --help
```

Windows acceptance must separately check 32/64-bit Host/Hook build and attachment, actual HOOK events and sources, optional installed OCR recognition, native Process Loopback and VAD/ASR, Irodori playback completion, repeated/split/same text, late audio, choices, focus loss, profile and process switches, real positive advance permission and transition ACK. Passing synthetic tests or CI is not evidence that a particular game is safely operable.

## Imported legacy settings

Old Luna `savegamedata_5.3.1.json` can be read **without modifying Luna's original file**. Data are transferred explicitly to the AutoAdv catalog; there is no parallel Luna settings authority. Older AutoAdv screen templates may be imported, but generic `dialogue` and `text_ready` indicators do not authorize automated input under the new contract. An old third-party Luna patch or bridge must not run concurrently with the managed Host. No external process is killed, detached or overwritten automatically.
