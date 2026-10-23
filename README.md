# AutoAdv

AutoAdv is a personal Windows GUI application for LunaHook dialogue acquisition, per-speaker Irodori TTS and authorized automatic dialogue advancement. The authoritative requirements are [docs/integration-spec.md](docs/integration-spec.md); code and former tests are not specifications.

The rebuilt `develop` application uses one `Controller` for dialogue identity, presentation completion and game-input authority. Selectively reused Luna native Host/ABI, Irodori and Windows input adapters are attached to that single runtime contract. Salvaged Windows Process Loopback, Japanese ASR/VAD, Tesseract OCR, manual input monitoring, Luna settings import and text-rule interpretation have also been integrated. Previous controllers, simulators and diagnostic CLI are not alternative execution paths.

## Windowsでの起動と基本操作

Python 3.11を導入した後は、リポジトリ内の **`AutoAdv.cmd` をダブルクリック**して起動する。初回はAutoAdv専用runtimeを`%LOCALAPPDATA%\AutoAdv\runtime`へ自動準備し、GUI・audio・OCR・native依存、Haqumei G2P、固定ASR model/tokensを配置する。Process Loopback helperは同じ管理領域へsource digest単位で配置し、成立済みの依存・モデル・helperを毎回再作成しない。

以後は同じ **`AutoAdv.cmd`** からGUIを起動できる。ショートカットはこのファイルから通常のWindowsの操作で作成できる。GUIは別のコンソールを必要とせず、起動に失敗した場合はエラーダイアログと`%LOCALAPPDATA%\AutoAdv\startup-error.log`に内容を表示・記録する。`.pyw`のPython関連付けが設定済みなら`AutoAdv.pyw`も直接起動できる。従来どおり`py -3.11 -m autoadv`も使用可能。

1. **プレイ**：実行中のゲームを選択・登録するとLunaHookへの接続を自動的に開始する。保存済み作品では「保存した設定で接続・監視」から再開できる。監視開始後も自動送りは許可するまで停止している。
2. **台詞取得**：ゲーム内の台詞を表示し、実際に取得できた本文（必要なら話者）を単一の一覧で比較・選択する。別の取得元が必要な場合は一覧の「ほかの取得候補を探す」を使う。探索で得たコードは自動的に試用され、本文を実際に受信した候補だけが同じ一覧に現れる。探索結果専用の選択画面はない。
3. **台詞・音声**：受信した原文を使って解釈結果を確認し、「この話者の音声を設定」「話者・本文の切り出しを修正」「名前・単語の読みを修正」から関連する編集箇所へ移動する。読み・置換・抽出の高度な規則も、その操作の直下から開ける。
4. **プレイ**：進行方式を文字のみ判断／音声判断から選び、必要な場合のみ補助設定を調整する。監視とゲーム入力許可は別操作。共通値の継承・作品独自値も該当項目の近くで変更する。

専用の雑多な「詳細・共通設定」タブはない。作品・HOOK取得・台詞調整・音声判断・入力それぞれの詳細項目は関連する操作領域から開く。GUIはLunaHostの診断を必要に応じて示すが、ゲームに関係する通常作業へ内部データを混入させない。

LunaTranslator v12.1.2.12の固定Native/Host資産は`autoadv/luna_assets`に同梱される。起動時の再取得・再ビルドは行わない。Irodori TTSは既存サーバーや指定された環境を利用する。音声判断のASR/G2P/Process LoopbackはAutoAdv管理runtimeを既定とし、個別パス指定は上書き用途に限る。Process Loopback helperを初めて生成する環境では.NET 9 SDKが必要である。OCR利用時のTesseract本体は別途必要となる。

## Validation scope

```sh
python -m unittest discover -s tests -v
```

Local Linux tests verify domain contracts, import/storage, process-audio handling and generation invalidation, but do not certify Windows game compatibility. Windows CI validates Qt/adapter installation and the loopback helper build. Hosted Live validation covers the real LunaHook → text-decision → Windows-input → next-Hook path only. Auto-advance accuracy measurement is an explicit actual-Windows-machine suite and is not part of Fast CI, Windows Adapter Validation, Hosted Live, or any broad/full test suite. It measures voiced and unvoiced dialogue through the real product path; unverified modes must never be described as universally supported.

Changes are published to `develop` via [Publish Gateway](docs/publish.md). `main` and the product version remain unchanged without explicit approval.

The upstream Luna source is pinned in `vendor/luna` with its GPL-3.0 notice and checksum manifest. To deliberately adopt binaries of the same release from its official ZIP, use `python scripts/adopt_luna_release.py --archive <official-release.zip>`; its SHA-256 and each required runtime file are checked. To propose a different upstream source version, explicitly run `scripts/sync_luna_upstream.py` with a selected revision, review the source and binaries together, then test the updated adapter before publishing.
