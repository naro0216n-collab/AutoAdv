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

## 実環境化した検収（2026-10-21）

Windowsにログオンして**ゲームを別途起動した状態**で、通常のAutoAdv GUIを使う。
`run_live_check.ps1`はゲームのビルド・起動・検証用制御を一切行わない。
検証に使用する台詞順は `SCENARIO.json`（Ren'Py `script.rpy` の独立した期待値）にのみ置き、製品コードから参照しない。
実ゲーム以外で人工的に作成したトレースは検収実績として扱わない。

```powershell
# P0-1: HookHostの実接続、取得元を伴うflush出力とUtterance
.\validation\real_game\run_live_check.ps1 -Phase hook

# P0-2: 閲覧時間→SendInput→次のHOOK取得（冒頭などを閲覧モードに設定）
.\validation\real_game\run_live_check.ps1 -Phase reading

# P0-3: Process LoopbackのPCM、実ASR一致、原音終了と次HOOK
.\validation\real_game\run_live_check.ps1 -Phase native

# P0-4: Irodoriの合成・実再生完了と次HOOK
.\validation\real_game\run_live_check.ps1 -Phase tts

# P0-5: 原音/TTS/閲覧、同一文連続、手動停止と再許可の連続検査
.\validation\real_game\run_live_check.ps1 -Phase mixed
```

対象シナリオの途中から監視する場合は `-StartLine N`（`SCENARIO.json` の1始まりの行番号）を指定する。
同文反復から開始して期待値の対応が曖昧な場合は省略不可。
検証後にGUIを閉じると照合が実行される。実HOOK未成立、実音声・実入力未成立、台詞飛ばし、原音のASR証拠不足、次台詞を受け取れない場合はFAIL。
単独のHOOK検査は音声処理とSendInputを要求しない。
閲覧・原音・TTSはそれぞれ個別に検収でき、最初から音声環境の全要素を用意する必要はない。

**判定内容と制限**：

- HOOKについて、同じHost世代・接続世代・対象PIDのconnected通知、Native取得元のthread/context、LunaHostの出力順序、Utteranceの原文ハッシュと取得元を照合。
- 入力後の台詞は、入力より*後*にHostが受信したHOOK出力から、同じセッションで生成されたことを確認。単なるSendInput戻り値や、すでに受信済みの候補は成功証拠にしない。
- 原音について、対象ゲーム音声PIDに紐付くProcess Loopback PCM受信、ASR処理での一致と完了を区別。`native_ready`やVADのみではASR検収を通さない。TTSはVoiceJobsの実再生ドレイン完了を使用。
- 既知の選択肢入口（`choice-notice`）ではAutoAdvの入力を要求せず、利用者が停止・選択して続行する。終端にも次台詞を要求しない。その他の送信に対応する次台詞は必須。
- `mixed`は同文２回の別取得と別入力・手動停止と再許可も必要。開始から`choice-notice`までの区間で実行可能。
- 検証器自体の単体テストでは人工記録を使用するが、**実ゲームの合否を生成・保証しない**。公式Windows CIは音声解析・検証器契約の実行のみ。実セッションは利用者の対話Windows環境で別に確認する。

`AUTOADV_VALIDATION_TRACE`を直接設定してGUIを起動した場合も、終了後に
`python -m validation.real_game.verify_trace --phase hook --trace <新規JSONL>` で同じ照合を行える。
旧形式（version 1）のトレースは、入力後HOOK受信時刻とPCM証拠がないため検収対象外。
