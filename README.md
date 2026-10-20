# AutoAdv

AutoAdv is a personal Windows GUI application for LunaHook dialogue acquisition, per-speaker Irodori TTS and authorized automatic dialogue advancement. The authoritative requirements are [docs/integration-spec.md](docs/integration-spec.md); code and former tests are not specifications.

The rebuilt `develop` application uses one `Controller` for dialogue identity, presentation completion and game-input authority. Selectively reused Luna native Host/ABI, Irodori and Windows input adapters are attached to that single runtime contract. Salvaged Windows Process Loopback, Japanese ASR/VAD, Tesseract OCR, manual input monitoring, Luna settings import and text-rule interpretation have also been integrated. Previous controllers, simulators and diagnostic CLI are not alternative execution paths.

## Install and operate

```powershell
py -3.11 -m pip install -e ".[gui,audio,ocr,native]"
py -3.11 -m autoadv
```

Use the pinned LunaTranslator v12.1.2.12 runtime shipped in `autoadv/luna_assets`, an existing Irodori-TTS-Server directory and configured Irodori Voices. This runtime is adopted from the upstream release once, not fetched or rebuilt during ordinary CI or app startup. Only native/AUTO sessions require a built Windows loopback helper, a SenseVoice-compatible sherpa-onnx model and tokens. Only OCR sessions require an installed Tesseract CLI. Installing models, the .NET runtime, and other upstream environments requires deliberate user action; AutoAdv does not alter those installations.

For the optional process-scoped audio adapter, build the preserved helper on Windows with .NET 9:

```powershell
dotnet build integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj -c Release
```

In the GUI, register a game and executable path, select a matching visible game window or enter its PID/HWND, start the bundled LunaHost and attach. Search/insert HOOK codes or select observed text/speaker HOOKs, save rules and voice assignments, and start monitoring. For OCR, select the explicit OCR acquisition option and set the client-relative region. Monitoring and automatic advancement are separate actions: **game input is not authorized until requested**.

Voice modes are **native** (ASR-attributed original speech completion), **tts** (Irodori playback completion), **auto** (original speech evidence or a configured TTS fallback), and **reading** (configured viewing duration). The AUTO wait is an explicit fallback policy, not proof that original speech is absent. The Controller verifies the foreground PID/HWND and manual-intervention state before each game input and never blindly retries an uncertain send. See [OPERATING.md](OPERATING.md) for details.

## Validation scope

```sh
python -m unittest discover -s tests -v
```

Local Linux tests verify domain contracts, import/storage, process-audio handling and generation invalidation, but do not certify Windows game compatibility. Windows CI validates Qt/adapter installation and the loopback helper build. Real games and real audio input remain separate acceptance requirements; unverified modes must never be described as universally supported.

Changes are published to `develop` via [Publish Gateway](docs/publish.md). `main` and the product version remain unchanged without explicit approval.

The upstream Luna source is pinned in `vendor/luna` with its GPL-3.0 notice and checksum manifest. To deliberately adopt binaries of the same release from its official ZIP, use `python scripts/adopt_luna_release.py --archive <official-release.zip>`; its SHA-256 and each required runtime file are checked. To propose a different upstream source version, explicitly run `scripts/sync_luna_upstream.py` with a selected revision, review the source and binaries together, then test the updated adapter before publishing.
