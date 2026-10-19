# AutoAdv

LunaTranslatorの原文取得、Windowsゲームのprocess-loopback音声、音声認識、Irodori-TTS-Serverを単一Controllerへ接続するノベルゲーム用自動進行基盤。

## 進行の安全契約

進行主体はControllerだけ。台詞世代・キャンセルトークン・画面状態・表示完了・native/TTS再生完了・手動介入・遷移ACKを検査し、入力は一行あたり最大一度とする。ゲームボイスの存在が不明、取得不能、ASR/VADの根拠不足なら無音タイマーで強制送りしない。選択肢・マップ・メニュー・不明画面は停止する。

## 実装

- `autoadv/controller.py`, `application.py`, `runtime.py`：進行State・観測統合・入力直前の独立検査。
- `autoadv/process_audio.py` + `integrations/windows-loopback/`：NAudio 3.2を使うプロセス固有WASAPI loopback。ゲームPIDのステレオPCM/heartbeatだけをPythonへ送る。
- `autoadv/audio_recognition.py`, `audio_match.py`, `native_gate.py`：音声履歴・日本語Sherpa ASR・WebRTC VAD・台詞照合と単調な進行/末尾確認。音声検出・ASR単独では発話終了を断定しない。
- `autoadv/screen.py`, `live_guard.py`：ゲーム別の正の視覚マーカー、Tesseract日本語OCR、Windows foreground/PID照合、手動入力検出。
- `autoadv/irodori.py`, `speaker.py`, `text.py`：TTS生成と実デバイス再生完了の区別、TTS中断、話者別voice/Skip、ルビ処理。
- `autoadv/luna_bridge.py` + `integrations/luna/`：Luna原文フィルタ前の候補観測。HOOK成功・安定行ID・表示完了はそれだけでは保証しない。
- `autoadv/live.py`：上記Adapterを一つに接続するWindows実行系。入力は明示 `--enable-input` と校正プロファイルが必要。

## 検証

```sh
python -m unittest discover -s tests -v
python -m autoadv --help
```

[OPERATING.md](OPERATING.md)にWindowsでの導入・画面校正・初期設定・診断・接続障害対応を記載。デフォルトの音声有無は未解決として扱い、実ゲームでの自動送り可能性はモデル/作品別の校正が必要。

**未検証**：この開発環境ではWindowsゲーム、Luna HOOK、日本語OCR/ASRモデル実データ、WASAPI COM動作、.NET 9 helperビルド、Irodori音声デバイス、代表ゲームにおける連続進行の実機試験を実行できない。Linuxの単体テスト合格だけで指示書の完了判定を行わない。

正準仕様は `docs/integration-spec.md`、開発原則は `docs/development-principles.md`。公開は `develop` へ標準Publish Gatewayを使い、`main`はユーザー承認まで不変更。
