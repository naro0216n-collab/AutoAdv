# AutoAdv専用 実音声／実ゲーム検収スイート

このスイートの合否対象は **AutoAdvが行う処理だけ**。

- LunaHookが実際に受信した取得元・順序と、AutoAdvの本文／話者の解釈
- Windows Process Loopbackで得たゲーム原音に対するNativeAudioの発話終了処理
- Irodori TTSの実際の再生完了と、送信権限を伴うゲーム入力
- 同じ台詞の連続、停止・再開、ゲーム切替による古い処理の失効

ゲームのビルド、起動、エンジン動作、配布物取得は**検証項目ではない**。
ゲームの実行そのものは外部の前提条件であり、ゲームのソースにテスト用IPCを追加しない。
AutoAdv本体は検証シナリオ／台詞一覧／音声ファイルの内容を参照せず、
通常のLunaHook・Process Loopback・Irodori・Windows入力だけを用いる。

## 収録コンテンツ・権利

`renpy/game/` はRen'Pyで表示される短い独立した検証ゲームの素材一式。
話者「明日香」は通常声、「栞」はささやき声、「主人公」は無音声。
音声素材：あみたろの声素材工房（https://amitaro.net/）。
`renpy/LICENSE_*_ja.txt`、`renpy/CREDITS.txt` および `RIGHTS_AND_SCOPE.md` を参照。
音声単体の素材パックとして再配布しないこと。公開後の著作者への事後報告を要確認。

音声SHA-256、台詞、ファイルパスは `VOICE_MANIFEST.json` に固定する。
原音は人間の録音。通常声とささやき声は同一話者であり別人の発話検査ではない。

## 検査A：実音声を使ったAutoAdv音声解析契約（CI・ローカル）

`python -m unittest discover -s validation/real_game -p 'test_*.py' -v`

実際のWAVを48 kHzのProcess Loopback互換PCMへ変換し、
**AutoAdvのNativeAudioとVoiceActivityの既存コード**に渡す。
音声開始、末尾無音を受け取る前に完了しないこと、完了が一度だけになることを確認。
ゲームの実HOOK、Process Loopbackの捕捉、Irodori実音声、Windows実入力の成立は**証明しない**。
Fast CIとは別の `Real Audio Validation` ワークフローで走らせる。

## 検査B：起動済み実ゲームに対するAutoAdv実経路検収（Windows）

1. 利用者がRen'Pyゲームの実行環境を別途準備してゲームを起動する。
   これはテストによる検証ではない。
2. 新規出力先を指定し `AUTOADV_VALIDATION_TRACE` を設定して、通常のAutoAdv GUIを起動。
   Windows PowerShellの例：

   `$env:AUTOADV_VALIDATION_TRACE = "$env:TEMP\\autoadv-real-game-$(Get-Date -Format yyyyMMddHHmmss).jsonl"`

3. AutoAdvのGUIから実HOOKを探索・選択し、原音＝明日香／栞、
   Irodori TTS＝主人公（Voice IDは個人のIrodori環境に応じて設定）。
   同文連続ルール `each_after_input` を必要に応じて設定。
   `native` はProcess Loopback＋ASR照合を推奨。VAD-onlyの利用条件を満たさない環境で使わない。
4. 自動送りを明示的に許可し、ゲームの通常会話を実行する。
   選択肢では一時停止・手動選択・再許可する。
5. GUIを閉じて `python -m validation.real_game.verify_trace --trace "$env:AUTOADV_VALIDATION_TRACE"`。

本検証は、実HOOKの通知、Utteranceの話者／本文、NativeAudio完了、
TTSの再生完了、実SendInputから得た**同一GUI実行経路の事実**を照合する。
音声データ・原文・画像はトレースに出力せず、文面はSHA-256で照合する。

HOOK・音声・入力のどれかが成立しない場合は **FAIL** とする。
実ゲームの観測記録が未取得の場合は未検収であり、検査Aの成功を検査Bの成功と読み替えない。
GitHub-hosted Windowsには実ゲーム／音声出力／実験者の入力がないため、
検査BをFast CIへ偽装して自動成功させない。
