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
   進行は「文字のみ判断」「音声判断」の2択。TTSの利用は話者設定で独立に指定する。音声判断かつTTS非使用の場合にProcess Loopback＋ASR照合を使用する。VAD-onlyは発声帰属の制限を理解して使う。
4. 自動送りを明示的に許可し、ゲームの通常会話を実行する。
   選択肢では一時停止・手動選択・再許可する。
5. GUIを閉じて `python -m validation.real_game.verify_trace --phase mixed --trace "$env:AUTOADV_VALIDATION_TRACE"`。

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

# P0-2: 文字のみ判断で文字数時間→SendInput→次HOOK（TTS使用は別設定）
.\validation\real_game\run_live_check.ps1 -Phase reading

# P0-3: 音声判断・TTS非使用のPCM、ASR、原音終了と次HOOK
.\validation\real_game\run_live_check.ps1 -Phase native

# P0-4: 音声判断・TTS使用の合成・実再生完了と次HOOK
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

## 原音ASRとIrodori TTSの独立性

「音声判断」かつ**TTS非使用**の台詞だけProcess LoopbackのPCMを実ASRへ送り、現在のUtteranceとの照合・発話終了を判断する。
TTS使用台詞はIrodoriの合成と**デバイスでの再生終了**を待ち、TTS音声をASRへ再投入しない。「文字のみ判断」＋TTS使用ではさらに文字数時間の経過を要する。
根拠不足時フォールバックは明示ONの音声判断・非TTS台詞だけに作用し、原音不存在・原音終了の認定ではない。送信権限は一台詞一回のみ。
以下の特殊音声検証は**原音だけ**を扱い、Irodoriサーバー・Voice・TTS区間を事前条件にしない。

## 隔離検証環境のASRモデル準備（明示実行のみ）

モデルはGitワークツリーにも通常のアプリ設定にも書き込まず、Windowsでは既定で
`%LOCALAPPDATA%\AutoAdv\validation\asr` に保存する。その他のOSではユーザーキャッシュ下に保存する。
通常利用のASRモデルの選択は作品プロファイルが所有し、この検証用キャッシュは設定正本ではない。
実HOOKからの原音検収では、GUIで該当作品のASRモデル／tokensに、この隔離キャッシュのパスを明示指定する。
TTS専用の作品設定にはASRモデルの指定も準備も要求しない。
版・SHA-256は `ASR_MODEL.json` に固定し、既存ファイルが不一致なら上書きせず失敗する。
新規取得は検証者が下記を明示実行したとき、または検証専用ドラフトPRを再オープンしてReal Audio Validationの実モデル検証を要求したときに限る。通常起動とFast CIでは取得しない。

```powershell
python -m validation.real_game.prepare_asr_model
python -m validation.real_game.prepare_asr_model --verify-only
```

隔離ディレクトリを変更する場合は `--directory` を指定し、ベンチマークへも `--model`・`--tokens` を渡す。
モデルまたはtokensの取得に失敗した場合、実モデルASR検証は**未検収**であり、FakeRecognizer / VAD-onlyの結果を代替しない。

## ASR特殊音声・背景ボイスの挑戦シナリオ（Irodori不要）

Ren'Pyゲームの手動メニュー「ASR非言語発声・背景音声テスト」から、通常原音対照例・同時背景発話・
原音より後に続く背景声・背景声だけ・表示文と完全一致する背景声・泣き声・叫び声を検証できる。背景声は短い実録音 `normal_020.wav` のループを複数台詞にまたがって流し、混合PCMが本体の原音と区別できない条件を確認する。録音混合のASR検証でも同じ短い音声を反復する。
泣き声と叫び声の実録音はCC0音源を固定の48 kHz mono PCMに変換して同梱し、SHA-256で内容を検証する。取得元の403などを理由に検証を迂回しない。

```powershell
python -m validation.real_game.stress_asr --selection all --rounds 2 --output asr-stress.json
.\validation\real_game\run_live_check.ps1 -Phase stress
```

`stress_asr`は実モデルの推論時間・RTF・文字列誤認・背景声の誤帰属リスクを**観測するだけ**で、
ゲームの進行結果は認定しない。`-Phase stress`は実HOOK・PCM・ASR・ユーザーの停止操作を使う。
**背景声が表示台詞と完全に一致する場合、Process LoopbackとテキストASRだけでは発話者を識別できない。**
したがって、このケースの誤完了はモデル品質の閾値調整で解決できたと解釈せず、製品の既知の識別限界を
検出する負荷検証とする。現在の製品がこのケースで安全停止できることは未確認であり、必ずPASSする条件として扱わない。

## 録音を時間順に通したNativeAudio実ASR判定（実モデル専用検証）

`benchmark_asr`は録音全体を一度に推論し、`stress_asr`は混合音声の全体転写を観測する。
これらとは別に、`timeline_asr`は同じ録音14音声と特殊音声8場面を
40msずつ実時間に近い間隔で、送信相当の観測境界から既定600ms後にHOOKが届く条件で**製品の`NativeAudio.feed`・`VoiceActivity`・`JapaneseASR`・`TargetMatcher`**へ渡す。
原音の観測開始、PCM末尾の無音、部分認識のカバレッジ、発話終了通知、推論滞留を時刻付きで保存する。
旧台詞の推論結果は新台詞へ適用しない。波形の加工・独立ASR判定器・第二の進行Controllerは使用しない。

```powershell
python -m validation.real_game.timeline_asr --selection all --hook-delay-ms 600 --output asr-timeline.json
# 原音が完全に終わってからHOOKを受ける条件の追加診断も可能:
python -m validation.real_game.timeline_asr --selection corpus --hook-delay-ms 3000 --output asr-late-hook.json
```

`Real Audio Validation`の検証専用PR再オープン時に、この測定を**実モデル**で実行する。
モデル・録音は通常起動やFast CIで取得しない。録音の末尾には無音を追加し、完了通知が
「録音ファイル終端より前」に出たかを診断する。ただし録音末尾の無音と実発声終了は異なるため、
終端前というだけで早送りと断定しない。`native_completions=0`も自動で合格・失格とみなさない。
データは実Process Loopback、ゲームHOOK、GUI、実入力を経由していないため、
**実ゲーム検収の成功を示さない**。実ゲームではON/OFF両方の送り・手動引継ぎを別途検証する。

## 音声認識品質と処理速度（任意実行、Fast CI対象外）

**実モデルの認識精度をFast CIの合否条件にはしない。** 同一モデル・コーパス・前処理ではほぼ不変のため、モデル・前処理・照合設定を採用／変更した場合、または実機での品質・応答速度を調査する際に明示実行する。手元で明示実行する場合は既存のASR環境を利用し、通常起動とFast CIでは取得しない。検証専用ドラフトPRのクローズ→再オープンでは`Real Audio Validation`が隔離キャッシュに固定モデルを検証付きで用意し、実モデルベンチマークを実行する。

```powershell
python -m validation.real_game.benchmark_asr `
  --conditions clean quiet noise clipped truncated bgm `
  --rounds 2 --output .\asr-performance.json
```

モデルとtokensに指定がなければ隔離検証用キャッシュを使用する。実行時に`JapaneseASR`（製品使用の`sherpa-onnx`）で既存14音声を認識する。`clean`に加え、低音量・ノイズ・クリッピング・先頭欠落・シナリオ同梱のBGMを**メモリ内で加工**して同じ経路で評価する。モデル読込時間、各発話の実推論wall-clock時間、RTF（推論時間÷音声時間）、正規化CER、条件別のP50/P95と最大値を記録する。モデル／tokensのハッシュ、実行環境、条件・seedも保存する。テスト音声の改変ファイルは生成・再配布しない。速度と認識品質は**観測結果**であり、全音声を認識できるという固定アサーションにしない。言語の読み・表記揺れがあるためCERは単独で送り判断の品質を確定しない。

通常の実ゲーム検収 (`run_live_check.ps1`) は、検証トレースに追加された**製品処理そのものの時間**を集計する。`verify_trace.py`のJSON結果に含まれる`performance`は、HOOK取得通知→Utterance、Utterance→提示完了、提示完了→入力試行、入力試行→次HOOK受信、**VAD推定終了→判断／入力、HOOK→VAD推定発声開始（負値は音声先行）**、実ASR推論・キュー待ち、Irodori合成・実再生の各時間とRTFのP50/P95／最大値／件数。`input_to_next_hook_ms`は次HOOKの受信元時刻を使う。トレースを有効にしていない一般利用時には追加診断を記録しない。

計測時は実際に使用するCPU/GPU・ASRモデル・音声機器・ゲーム負荷を記録して比較する。少数の台詞から得たP95は推定値であり、異なるPCやCIランナーとの絶対性能比較には使用しない。待ち時間には原音の実再生・閲覧時間が含まれるため、純粋な処理オーバーヘッドと分けて解釈する。VAD終了時刻は受信した混合PCMの推定値であり、原音単体の物理的な再生終了を保証しない。背景の断続的ループでは特に誤帰属・過剰保留の両方を観測する。音声が認識できない場合や入力権限が失効した場合、計測値の大小ではなく本体の既存状態遷移と安全停止を検証する。

## GitHubでの専用検証の実行操作

検証専用ドラフトPR（`develop` → `main`、マージ禁止）をクローズしてから再オープンすると、Real Audio Validationを含む3つの重量級検証が起動する。追加branchやワークフロー変更は不要。Fast CIのpush検証とは別に結果を確認する。PRをマージして`main`を変更しないこと。実SenseVoice推論による品質・RTFの測定は実ゲームでの自動進行成立の証拠ではない。

## 音声判定ストレス検証の解釈（2026-10-21）

`ASR_STRESS_SCENARIO.json`に一律の`expected_hold`は存在しない。泣き声・叫び声、別発話、BGM、後続背景音のケースは、PCM時系列・実ASR結果・判定根拠・フォールバック設定・実入力時刻を記録して誤送りと余分な保留を比較する。「背景音声だけで表示文と同一」という作者分類は音源所有を独立に証明しないため、`background_only_same_text`は診断例に限定する。`stress_asr.py`による録音全体の認識結果は実時系列の途中照合精度や実ゲームの合格を保証しない。
