# AutoAdv

AutoAdv is a personal Windows GUI application for LunaHook dialogue acquisition, per-speaker Irodori TTS and authorized automatic dialogue advancement. The authoritative requirements are [docs/integration-spec.md](docs/integration-spec.md); code and former tests are not specifications.

The rebuilt `develop` application uses one `Controller` for dialogue identity, presentation completion and game-input authority. Selectively reused Luna native Host/ABI, Irodori and Windows input adapters are attached to that single runtime contract. Salvaged Windows Process Loopback, Japanese ASR/VAD, Tesseract OCR, manual input monitoring, Luna settings import and text-rule interpretation have also been integrated. Previous controllers, simulators and diagnostic CLI are not alternative execution paths.

## Windowsでの起動と基本操作

初回のみPython 3.11の環境へ必要な依存物をインストールする。

```powershell
py -3.11 -m pip install -e ".[gui,audio,ocr,native]"
```

以後はリポジトリ内の **`AutoAdv.cmd` をダブルクリック**してGUIを起動できる。ショートカットはこのファイルから通常のWindowsの操作で作成できる。GUIは別のコンソールを必要とせず、起動に失敗した場合はエラーダイアログと`%LOCALAPPDATA%\AutoAdv\startup-error.log`に内容を表示・記録する。`.pyw`のPython関連付けが設定済みなら`AutoAdv.pyw`も直接起動できる。従来どおり`py -3.11 -m autoadv`も使用可能。

1. **プレイ**：実行中のゲームを選択・登録する。保存済み作品では「保存した設定で接続・監視」から再開できる。監視開始後も自動送りは許可するまで停止している。
2. **台詞取得**：初回のみゲームへLunaHookを接続し、ゲーム内の実際の台詞を表示して本文（必要なら話者）の取得元を選択する。取得できないときは同じ画面の「取得できない・文字化けする場合の調整」を開く。追加探索のコードは実出力が確認されるまで本文候補に現れない。
3. **台詞・音声**：受信した原文を使って解釈結果を確認し、「この話者の音声を設定」「話者・本文の切り出しを修正」「名前・単語の読みを修正」から関連する編集箇所へ移動する。読み・置換・抽出の高度な規則も、その操作の直下から開ける。
4. **プレイ**：進行方式を文字のみ判断／音声判断から選び、必要な場合のみ補助設定を調整する。監視とゲーム入力許可は別操作。共通値の継承・作品独自値も該当項目の近くで変更する。

専用の雑多な「詳細・共通設定」タブはない。作品・HOOK取得・台詞調整・音声判断・入力それぞれの詳細項目は関連する操作領域から開く。GUIはLunaHostの診断を必要に応じて示すが、ゲームに関係する通常作業へ内部データを混入させない。

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
