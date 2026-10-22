# AutoAdv

AutoAdv is a personal Windows GUI application for LunaHook dialogue acquisition, per-speaker Irodori TTS and authorized automatic dialogue advancement. The authoritative requirements are [docs/integration-spec.md](docs/integration-spec.md); code and former tests are not specifications.

The rebuilt `develop` application uses one `Controller` for dialogue identity, presentation completion and game-input authority. Selectively reused Luna native Host/ABI, Irodori and Windows input adapters are attached to that single runtime contract. Salvaged Windows Process Loopback, Japanese ASR/VAD, Tesseract OCR, manual input monitoring, Luna settings import and text-rule interpretation have also been integrated. Previous controllers, simulators and diagnostic CLI are not alternative execution paths.

## Windowsでの起動と基本操作

初回のみPython 3.11の環境へ必要な依存物をインストールする。

```powershell
py -3.11 -m pip install -e ".[gui,audio,ocr,native]"
```

以後はリポジトリ内の **`AutoAdv.cmd` をダブルクリック**してGUIを起動できる。ショートカットはこのファイルから通常のWindowsの操作で作成できる。GUIは別のコンソールを必要とせず、起動に失敗した場合はエラーダイアログと`%LOCALAPPDATA%\AutoAdv\startup-error.log`に内容を表示・記録する。`.pyw`のPython関連付けが設定済みなら`AutoAdv.pyw`も直接起動できる。従来どおり`py -3.11 -m autoadv`も使用可能。

1. ゲームを起動し、AutoAdvの「ゲーム・自動進行」→「実行中のゲーム・プロセスから選択して登録」を開く。登録済みの実行ファイルがなくても実行中のウィンドウを検索・選択でき、選択した実行ファイルを作品として保存する。ウィンドウのないプロセスも明示的に一覧へ表示できるが、この場合、入力先ウィンドウは別途設定する。
2. 「HOOK候補」タブの「選択したゲームにLunaHookを接続」を押す。Hostは必要に応じて起動される。ゲームで台詞を表示し、取得した本文を先頭列に持つ候補一覧から一件選ぶ。自動候補がない場合には「HOOK候補を追加探索」を使用する。
3. 本文HOOKを選択し「選択したHOOKで監視開始」を押す。作品ごとのHOOK・補正・話者別音声はプロファイルに保存・再利用する。監視開始と自動送りの許可は別操作である。PID/HWND、HOOKコード、OCR設定などの手入力は「詳細・共通設定」タブにある。
4. 「話者・音声」で複数話者のTTS使用有無・Voiceを一覧編集し、「本文整形」で話者抽出・TTS/ASR別の置換・読み辞書を優先順位とプレビューを確認して編集する。HOOK原文を試験入力に転送でき、未保存の編集中の内容も仮適用して結果を確認できる。詳しくは[OPERATING.md](OPERATING.md)。

LunaTranslator v12.1.2.12の固定Native/Host資産は`autoadv/luna_assets`に同梱される。起動時の再取得・再ビルドは行わない。Irodori TTSは既存サーバーや指定された環境を利用する。音声判断で原音を観測する場合のみ、Windows Process Loopback helperと対応するASRモデルが必要となる。OCRの利用時のみTesseractが必要である。

.NET 9導入済みWindowsで任意にProcess Loopback helperをビルドする場合：

```powershell
dotnet build integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj -c Release
```

## Validation scope

```sh
python -m unittest discover -s tests -v
```

Local Linux tests verify domain contracts, import/storage, process-audio handling and generation invalidation, but do not certify Windows game compatibility. Windows CI validates Qt/adapter installation and the loopback helper build. Real games and real audio input remain separate acceptance requirements; unverified modes must never be described as universally supported.

Changes are published to `develop` via [Publish Gateway](docs/publish.md). `main` and the product version remain unchanged without explicit approval.

The upstream Luna source is pinned in `vendor/luna` with its GPL-3.0 notice and checksum manifest. To deliberately adopt binaries of the same release from its official ZIP, use `python scripts/adopt_luna_release.py --archive <official-release.zip>`; its SHA-256 and each required runtime file are checked. To propose a different upstream source version, explicitly run `scripts/sync_luna_upstream.py` with a selected revision, review the source and binaries together, then test the updated adapter before publishing.
