# AutoAdv

LunaTranslatorを原文観測基盤とした、音声同期型のノベルゲーム自動進行Controller。

## 現在の実装範囲

* 共通状態機械（単一advance主体）、原文表現と記号/短文/空イベント区分、音声有無と再生状態の独立管理。
* 同期的な入力前ガード、epoch/token、遷移ACK、手動停止、重複入力禁止。
* Irodori `/v1/audio/speech`クライアントと中断可能なWAV再生Worker（`audio`オプション）。
* Luna `/api/ws/text/origin`観測Adapter（**診断専用**、台詞ID/表示完了が不明なため自動送りには使用不可）。
* Windows Enter Driver（Runtimeのガードを経ない単独使用は禁止）。

## 未接続の必須箇所

Lunaのフィルタ前イベント境界、実ゲーム画面完了・選択判定、Windows process loopback、VAD/ASRによる現在行への発話帰属と終了確認、作品profile。これらを通すまでは実ゲームの自動入力を有効化しない。

## 検証

```sh
python -m unittest discover -s tests -v
```

Irodoriとゲームの動作確認はWindows上で別途必要。実機での未検証事項を合格とみなさない。

## 安全な接続診断

```sh
# Luna標準原文WebSocketの確認（台詞境界は保証されない）
python -m autoadv luna-origin --url ws://127.0.0.1:2333/api/ws/text/origin --count 10

# Irodori-Serverの実生成確認。VOICE_IDはIrodoriに登録済みのものを指定
python -m autoadv tts-smoke --voice VOICE_ID --output sample.wav

# Lunaの任意差分を導入する場合。LunaTranslator.pyのあるディレクトリを指定
python scripts/luna_bridge_patch.py PATH_TO_LUNA_PYTHON_DIR --install
python -m autoadv bridge-monitor
```

bridge-monitorのraw line candidateにも表示完了・ゲーム画面分類・台詞境界の保証はないため、現時点で自動入力へ直結しない。Luna側でHOOK/OCRより手前にある重複・空文字フィルタの有無は作品とLunaバージョンごとに再確認する。

Publish Gatewayの移植準備と初回bootstrapの区別は [`docs/publish.md`](docs/publish.md) に記載。