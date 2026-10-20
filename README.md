# AutoAdv

AutoAdv is a Windows desktop application under zero-base reconstruction. The authoritative product requirements are in [docs/integration-spec.md](docs/integration-spec.md). The archived original implementation is held on branch `temp`; the `develop` working tree contains only explicitly selected adapters and new implementation.

## Current executable path

The PySide6 window registers games and profiles, starts a pinned-compatible LunaHook Host from a selected local DLL, attaches to a game PID, previews observed HOOK threads, lets the user choose a dialogue HOOK and an optional speaker HOOK, and interprets text into speaker, reading, and per-speaker audio mode and Voice. Its settings are saved atomically as a new schema in `%APPDATA%/AutoAdv/settings-v3.json` without rewriting existing settings.

Irodori-TTS uses an existing installation and voice enumeration. Selected TTS is generated asynchronously and considered complete only when the local audio device finishes playback. Silent/reading mode waits the configured reading time. The conversation controller is the sole owner of advance permission and sends at most one input per accepted line, after the configured game window and PID are verified in the foreground. Permission is always separate from monitoring.

**Not yet connected:** Process Loopback/VAD/ASR presentation completion for native speech; AUTO mode switching; optional OCR; automatic game detection; interactive HOOK searching and restoring manually inserted HOOKs; existing-settings import and full text-rule editing. Native/AUTO routing deliberately **does not authorize input** in the current implementation. `temp` retains the former technical assets for selective adaptation. Domain tests are not a substitute for Windows game acceptance.

## Running

```powershell
py -3.11 -m pip install -e ".[gui,audio]"
py -3.11 -m autoadv
```

Provide an existing Irodori-TTS-Server installation folder and a built LunaHost.dll. Set the current game PID/HWND, attach Host, observe and select a dialogue HOOK, save routing, and start monitoring. Advance remains disabled until separately authorized. The executable does not install models or modify the Irodori environment.

## Tests

```sh
python -m unittest discover -s tests -v
```

Publishing uses the repository's [standard Publish Gateway](docs/publish.md). `main` and version are not modified without user approval.
