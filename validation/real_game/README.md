# AutoAdv専用 実音声／実ゲーム検収スイート

このスイートの合否対象は **AutoAdvが行う処理だけ**。

- LunaHookが実際に受信した取得元・順序と、AutoAdvの本文／話者の解釈
- Windows Process Loopbackで得たゲーム原音に対するNativeAudioの発話終了処理
- Irodori TTSの実際の再生完了と、送信権限を伴うゲーム入力
- 同じ台詞の連続、停止・再開、ゲーム切替による古い処理の失効

ゲームのビルド、起動、エンジン動作、配布物取得は**検証項目ではない**。
ゲームの実行そのものは外部の前提条件である。検証ゲームには**結果判定専用の独立観測**を追加するが、AutoAdvへ介入するIPC・固定HOOK・模擬PCM・入力命令を追加しない。
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

1. 利用者がRen'Py検証ゲームの実行環境を別途準備し、**起動前に** `AUTOADV_GAME_ORACLE` を未使用のJSONLパスに設定してから、その環境を継承するWindowsシェルでゲームを起動する。
   この変数はゲームの**観測ログ**だけを有効にし、AutoAdvの入力・音声・HOOKには介入しない。ゲームのビルド・起動はAutoAdv検証器の責務外。
2. 新規出力先を指定し `AUTOADV_VALIDATION_TRACE` を設定して、通常のAutoAdv GUIを起動。
   Windows PowerShellの例：

   `$env:AUTOADV_VALIDATION_TRACE = "$env:TEMP\\autoadv-real-game-$(Get-Date -Format yyyyMMddHHmmss).jsonl"`

3. AutoAdvのGUIから実HOOKを探索・選択し、原音＝明日香／栞、
   Irodori TTS＝主人公（Voice IDは個人のIrodori環境に応じて設定）。
   同文連続ルール `each_after_input` を必要に応じて設定。
   進行は「文字のみ判断」「音声判断」の2択。TTSの利用は話者設定で独立に指定する。音声判断かつTTS非使用の場合にProcess Loopback＋ASR照合を使用する。VAD-onlyは発声帰属の制限を理解して使う。
4. 自動送りを明示的に許可し、ゲームの通常会話を実行する。
   選択肢では一時停止・手動選択・再許可する。
5. GUIを閉じて `python -m validation.real_game.verify_trace --phase mixed --trace "$env:AUTOADV_VALIDATION_TRACE" --oracle "$env:AUTOADV_GAME_ORACLE" --report "$env:TEMP\autoadv-mixed-result.json"`。**原音終了と途中送りの検収にはゲーム側ログが必須**。HOOK・閲覧時間・TTS単独検査では実音声終了の独立観測を要求しない。

本検証は、実HOOKの通知、Utteranceの話者／本文、NativeAudio完了、
TTSの再生完了、実SendInputから得た**同一GUI実行経路の事実**を照合する。
音声データ・原文・画像はトレースに出力せず、文面はSHA-256で照合する。

HOOK・音声・入力のどれかが成立しない場合は **FAIL** とする。検証ゲームの`voice`チャンネルから独立に得た再生状態と、AutoAdvの実入力時刻が矛盾する場合もFAILとする。ゲーム観測は約20Hzの標本時刻であり、Windows再生デバイスの音響的なサンプル精度を保証しない。終了区間が曖昧なら成功とはせず、未確認として表示する。
原音の終了を評価する `native` / `mixed` / `stress`、および他のphaseで実際に非TTS原音による自動送りが含まれる場合は、ゲームの独立観測がなければその進行の安全性を検収しない。`hook`・`reading`・`tts`単独検査ではAutoAdvの実Hook、Controller判断、入力、次Hookの記録でそれぞれの対象範囲を検査できる。検査Aの成功を検査Bの成功と読み替えない。
GitHub-hosted Windowsには実ゲーム／音声出力／実験者の入力がないため、
検査BをFast CIへ偽装して自動成功させない。

## 検証目的・実行範囲（混同禁止）

- **内部契約**: 設定・状態遷移・トークン失効・複合照合の局所検証。合格しても自動送りがゲーム上で成立したとは認定しない。
- **実モデルASR**: CER・完全一致率・RTFは参考診断のみ。自動進行の合格率や改善効果をこれらで代理しない。
- **検証用ゲームの実経路**: 実LunaHook、実Process Loopback、製品NativeAudio/Controller、実SendInput、次Hookに加え、独立したRen'Pyゲーム観測によって原音終了と送り時刻を比較。進行の適正、途中送り、過剰保留、不確実時フォールバック、手動引継ぎの台詞単位結果を評価する。検証ゲームを本番の任意ゲームと同一視しない。
- **利用者の実作品**: 通常のGUI利用と実ゲーム上の確認が別途必要。第三者の市販作品ではゲーム内部の独立した再生終了観測を常に得られるわけではないため、混合PCMのVAD推定を真値として途中送り0件を宣言しない。

検証ゲームのゲーム側観測は `renpy/game/autoadv_oracle.rpy` にあり、
`config.all_character_callbacks` と `config.periodic_callbacks` を使用して台詞表示・再生状態・台詞を閉じた事実を独立記録する。AutoAdv本体はそのログもシナリオ期待値も読まない。ゲーム側観測は入力・台詞・再生の**操作を一切変更しない**。

ゲームは**AutoAdvを実行する前**に、次の新規出力パスを継承する環境から別途起動する。

```powershell
$env:AUTOADV_GAME_ORACLE = Join-Path $env:TEMP ('autoadv-game-' + [guid]::NewGuid().ToString('N') + '.jsonl')
# このPowerShellから検証ゲームを別途起動する（テストコマンドは起動しない）
.\validation\real_game\run_live_check.ps1 -Phase native -Oracle $env:AUTOADV_GAME_ORACLE
```

原音終了後の過剰保留の評価基準を調整する場合は `-MaxVoiceHoldMs` で指定できる（既定2000ms）。これは**検証品質の閾値であり製品の進行ロジックではない**。音声判定の内部設定は変えない。観測終了前の送りはNG、再生終了境界のサンプル精度不足は未確認としてNG、再生終了後の過剰保留は実測値付きでNGとする。文字のみ判断では原音終了を送り条件にしない。

## 実環境化した検収（2026-10-21）

Windowsにログオンして**ゲームを別途起動した状態**で、通常のAutoAdv GUIを使う。
`run_live_check.ps1`はゲームのビルド・起動・検証用制御を一切行わない。
検証に使用する台詞順は `SCENARIO.json`（Ren'Py `script.rpy` の独立した期待値）にのみ置き、製品コードから参照しない。
実ゲーム以外で人工的に作成したトレースは検収実績として扱わない。検証器の単体テストで生成する製品側・ゲーム側の人工ログは**検証器の拒否契約だけ**を検証する。

```powershell
# P0-1: HookHostの実接続、取得元を伴うflush出力とUtterance
.\validation\real_game\run_live_check.ps1 -Phase hook -Oracle $env:AUTOADV_GAME_ORACLE

# P0-2: 文字のみ判断で文字数時間→SendInput→次HOOK（TTS使用は別設定）
.\validation\real_game\run_live_check.ps1 -Phase reading -Oracle $env:AUTOADV_GAME_ORACLE

# P0-3: 音声判断・TTS非使用のPCM、ASR、原音終了と次HOOK
.\validation\real_game\run_live_check.ps1 -Phase native -Oracle $env:AUTOADV_GAME_ORACLE

# P0-4: 音声判断・TTS使用の合成・実再生完了と次HOOK
.\validation\real_game\run_live_check.ps1 -Phase tts -Oracle $env:AUTOADV_GAME_ORACLE

# P0-5: 原音/TTS/閲覧、同一文連続、手動停止と再許可の連続検査
.\validation\real_game\run_live_check.ps1 -Phase mixed -Oracle $env:AUTOADV_GAME_ORACLE
```

対象シナリオの途中から監視する場合は `-StartLine N`（`SCENARIO.json` の1始まりの行番号）を指定する。
同文反復から開始して期待値の対応が曖昧な場合は省略不可。
検証後にGUIを閉じると照合が実行される。実HOOK未成立、実音声・実入力未成立、台詞飛ばし、原音のASR証拠不足、次台詞を受け取れない場合はFAIL。
単独のHOOK検査は音声処理とSendInputを要求しない。`hook`・`reading`・`tts`の単独検証では `-Oracle` は省略可能。`native`・`mixed`・`stress`では必須。単独検証中でも非TTS原音の送りを記録した場合、その送りの安全性を独立観測なしで合格としない。
閲覧・原音・TTSはそれぞれ個別に検収でき、最初から音声環境の全要素を用意する必要はない。

実行終了後、`run_live_check.ps1`は製品トレースとは別に `<Trace>.result.json` を保存する。`-Report` で保存先を指定でき、検証器が解析できた不合格についても取得済みの台詞別進行結果、途中送り・過剰保留・未送信、時刻差、および失敗理由を保持する。`verify_trace.py` を直接起動する場合は `--report` を指定する。`line_results` の `no_autoadv_input` は進行成功とみなさず、`game_evidence` の `game_dismissed_without_autoadv_input` は外部入力でゲームが進んだ記録であってAutoAdvの成功ではない。結果に実ゲームのトレースがない場合は、検証器の単体試験結果を実ゲーム検収結果へ転用しない。

**判定内容と制限**：

- HOOKについて、同じHost世代・接続世代・対象PIDのconnected通知、Native取得元のthread/context、LunaHostの出力順序、Utteranceの原文ハッシュと取得元を照合。
- 入力後の台詞は、入力より*後*にHostが受信したHOOK出力から、同じセッションで生成されたことを確認。単なるSendInput戻り値や、すでに受信済みの候補は成功証拠にしない。
- 原音について、対象ゲーム音声PIDに紐付くProcess Loopback PCM受信、ASR処理での一致と完了を区別し、**ゲーム側の実voiceチャンネル終了観測より早い自動入力を失敗**とする。`native_ready`やVADのみではASR検収を通さない。TTSはVoiceJobsの実再生ドレイン完了を使用。
- 既知の選択肢入口（`choice-notice`）ではAutoAdvの入力を要求せず、利用者が停止・選択して続行する。終端にも次台詞を要求しない。その他の送信に対応する次台詞は必須。
- `mixed`は同文２回の別取得と別入力・手動停止と再許可も必要。開始から`choice-notice`までの区間で実行可能。
- 検証器自体の単体テストでは人工記録を使用するが、**実ゲームの合否を生成・保証しない**。公式Windows CIは音声解析・検証器契約の実行のみ。実セッションは利用者の対話Windows環境で別に確認する。

`AUTOADV_VALIDATION_TRACE`を直接設定してGUIを起動した場合も、終了後に
`python -m validation.real_game.verify_trace --phase hook --trace <新規JSONL> --report <結果JSON>` で同じ製品経路の照合を行える。原音終了と途中送りを検証する場合は `--phase native --oracle <ゲーム側JSONL>` を使用する。
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
.\validation\real_game\run_live_check.ps1 -Phase stress -Oracle $env:AUTOADV_GAME_ORACLE
```

`stress_asr`は実モデルの推論時間・RTF・文字列誤認・背景声の誤帰属リスクを**観測するだけ**で、
ゲームの進行結果は認定しない。`-Phase stress`は実HOOK・PCM・ASR・ユーザーの停止操作を使う。
**背景声が表示台詞と完全に一致する場合、Process LoopbackとテキストASRだけでは発話者を識別できない。**
したがって、このケースの誤完了はモデル品質の閾値調整で解決できたと解釈せず、製品の既知の識別限界を
検出する負荷検証とする。現在の製品がこのケースで安全停止できることは未確認であり、必ずPASSする条件として扱わない。

## 自動送りの実経路での時系列検収

録音PCMを`NativeAudio.feed`へ直接投入し、固定HOOKを`Controller.observe`へ渡して
自動入力を模擬する時系列検証は行わない。音声開始・HOOK到着遅延等の既知条件は
検証ゲーム側で設定可能だが、**結果の取得は通常GUIのLunaHook受信、実Process Loopback、
本体NativeAudio/Controller、Windows入力、次HOOKを通して行う**。

Windows上でゲームを起動してから、次のコマンドで通常GUIの実セッションを検収する。

```powershell
.\validation\real_game\run_live_check.ps1 -Phase native -Oracle $env:AUTOADV_GAME_ORACLE
.\validation\real_game\run_live_check.ps1 -Phase stress -Oracle $env:AUTOADV_GAME_ORACLE
```

原音開始がHOOKより先行する場合・終了後にHOOKが届く場合も、実ゲームでその条件を
発生させ、トレース中のHOOK受信時刻、VAD推定開始/終了、ASR、Controller判断、
Windows入力、次HOOKから観測する。固定された音声時刻を真値として進行判定に注入しない。
VAD終了は混合音声による推定であり、ゲーム内再生終了の独立した真値ではない。
実音声による途中送りを確定評価するにはゲーム側の実際の再生完了イベント等の
別系統の観測が必要であり、推定時刻だけで合否を確定しない。

録音コーパスは**検証ゲーム内の実再生**とASRの独立計測に使用できる。ただし録音PCMのAutoAdv内部への直接注入を、実経路の証拠にはしない。CIは実ゲームを起動しないため、
CIの実モデル結果を自動進行の実環境合格と称さない。

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
