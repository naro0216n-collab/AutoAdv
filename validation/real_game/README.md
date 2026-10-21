# AutoAdv専用 実音声／実ゲーム検収スイート

このスイートの合否対象は **AutoAdvが行う処理だけ**。

- LunaHookが実際に受信した取得元・順序と、AutoAdvの本文／話者の解釈
- Windows Process Loopbackで得たゲーム原音に対するNativeAudioの発話終了処理
- Irodori TTSの実際の再生完了と、送信権限を伴うゲーム入力
- 同じ台詞の連続、停止・再開、ゲーム切替による古い処理の失効

ゲームのビルド、起動、ウィンドウ表示、エンジン動作、配布物取得は**検証項目ではない**。
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

## 自動実行：既存ゲームと製品GUIによる自動進行検査

Windows上のRen'Py SDK（既存`renpy/game`を起動可能なもの）を用意し、
既存ゲームの通常のHook/Process Loopback/Controller/SendInputを通す：

```powershell
.\validation\real_game\run_live_check.ps1 -Phase native -RenPySdk C:\renpy-8.4.1 -Automated -TimeoutSeconds 150
```

`-Automated`は検査**準備だけ**を自動化する。ゲームPID/HWND検出、実LunaHost接続、校正用に受信した実Hook候補からの決定的な選択、通常GUIの監視・送り許可、終了後の既存検証器による照合を行う。既存Ren'Pyゲームの実行前バリアでHook選択を待ち、校正用の非対話台詞を一度出力する。校正台詞は自動送りの成績に含めず、以降のゲーム入力はAutoAdvの製品Controllerに限定する。

校正に一致する実Hook候補がない場合など、データ収集自体が成立しなければ失敗する。Native音声捕捉・実ASR・フォーカス・次Hookの成否は、製品経路の観測結果として台詞ごとに記録する。同一校正台詞を出す複数の実Hookは通常どおり選択し、選んだHookが後続台詞を実際に取得できたかで成否を判定する。ASRの完全一致率を自動進行の合否に用いない。既存設定は一時`APPDATA`で隔離する。Lunaは再ビルドせず、必要時はProcess Loopback helperのみビルドする。

`-Phase reading`は文字時間による通常送り、`-Phase native`はBGM下の原音判断を観測する。特定のASR成功件数や次Hook到達件数を検証器の収集成功条件とはしない。音声判断が不確実なときの文字数フォールバックは`-UncertainFallback`で明示的に有効化できる。`-Phase tts`は別途稼働中の実Irodoriサーバーと`-Voice`が必要。`mixed`/`stress`で原音判定が未確定となった場合、テストが入力して後続場面へ進めたり成功に数えたりしない。

## 検査B：既存Ren'Pyゲームを用いたAutoAdv実経路検収（Windows）

対象は既存の `renpy/game/script.rpy` であり、ゲームを小さく作り直さない。
通常の台詞・原音・Irodori TTS・背景声はすべてAutoAdvの通常経路で処理し、
検証器はゲームへ送り入力を行わない。ゲーム側 `autoadv_oracle.rpy` は終了時刻や台詞終了など、
AutoAdvから独立して確認する必要がある事実を記録するだけで、進行判定へ注入しない。ゲームが指定した音声ファイルを正しく再生したか、ゲーム内UIがどのように操作されたかはAutoAdvの合否に含めない。

Ren'Py SDKが導入済みなら、通常の実行スクリプトから**既存ゲームを起動**できる。
ゲームのメニュー選択や途中の手動進行は要求しない。`-RenPySdk` は
`AUTOADV_RENPY_SDK` 環境変数でも指定可能（Ren'Py SDKのルート）。

```powershell
.\validation\real_game\run_live_check.ps1 -Phase native -RenPySdk 'C:\path\to\renpy-sdk'
.\validation\real_game\run_live_check.ps1 -Phase stress -RenPySdk 'C:\path\to\renpy-sdk'
```

スクリプトは、既存ゲームの該当する台詞区間を開始条件として選択する。
`native` はシナリオ6行目から、`tts` は4行目から、`stress` は特殊音声分岐から開始する。
これは**開始位置だけ**の変更で、ゲームが台詞を自動で送る仕組みではない。
台詞送りは必ずAutoAdvのControllerとWindows入力が担当する。
通常の `mixed` は同文反復までを進め、選択肢に入る代わりに検証用終端行を表示する。
通常ゲームの選択肢は残すが、**自動進行テストの合格のために選択・手動停止する必要はない**。
`mixed` の検証用終端は通常の自動送りによる次Hook取得まで確認する。

既存ゲームを自分で起動している場合、従来どおり `-Oracle` にゲーム側の観測ログを
渡して検収できる。`native` / `mixed` / `stress` では音声終了を比較するためOracleが必要。
`hook` / `reading` / `tts` の専用検収では原音終了Oracleを必須としない。`reading` の自動診断では、実入力後に次Hookが来ない原因を切り分けるため、既存の台詞終了観測を追加で記録する。これはゲーム動作の合格条件ではなく、送信後の状態を説明する参考情報である。対象ウィンドウは実行ファイルと既存Ren'PyのSDLウィンドウから識別し、ゲームGUIの自動操作は行わない。

現行の `run_live_check.ps1` は通常のAutoAdv GUIを起動し、終了後に既存の検証器で
AutoAdvの取得・複合判定・SendInput・次Hookを照合する。
`-Automated`では通常GUIが持つ作品設定・Hook選択・監視開始・送り許可のメソッドを
検証準備から呼び出す。ゲーム画面を外部から操作するGUI自動化は使用しない。
収集の整合性と実際の自動進行成立は別の結果として判定する。自動進行成立は実行結果の台詞取得・音声判断・製品入力・次Hookで判断する。
失敗時も `<Trace>.result.json` に取得済みの判断・遅延・進行結果を保存する。
実際のゲーム実行・音声デバイス・GUIが稼働していない場合は実環境未検収とする。

ゲームのビルドやRen'Pyエンジン自体のテストはAutoAdvの合否対象に含めない。
`-RenPySdk` は既存エンジンの通常起動であり、SDK取得・ビルドを行わない。

## 検証目的・実行範囲（混同禁止）

- **内部契約**: 設定・状態遷移・トークン失効・複合照合の局所検証。合格しても自動送りがゲーム上で成立したとは認定しない。
- **実モデルASR**: CER・完全一致率・RTFは参考診断のみ。自動進行の合格率や改善効果をこれらで代理しない。
- **検証用ゲームの実経路**: 実LunaHook、実Process Loopback、製品NativeAudio/Controller、実SendInput、次Hookに加え、独立したRen'Pyゲーム観測によって原音終了と送り時刻を比較。進行の適正、途中送り、過剰保留、不確実時フォールバック、手動引継ぎの台詞単位結果を評価する。検証ゲームを本番の任意ゲームと同一視しない。
- **利用者の実作品**: 通常のGUI利用と実ゲーム上の確認が別途必要。第三者の市販作品ではゲーム内部の独立した再生終了観測を常に得られるわけではないため、混合PCMのVAD推定を真値として途中送り0件を宣言しない。

検証ゲームのゲーム側観測は `renpy/game/autoadv_oracle.rpy` にあり、
`config.all_character_callbacks` と `config.periodic_callbacks` を使用して台詞表示・再生状態・台詞を閉じた事実を独立記録する。AutoAdv本体はそのログもシナリオ期待値も読まない。ゲーム側観測は入力・台詞・再生の**操作を一切変更しない**。

ゲーム側の独立観測を必要とするphaseは上記 `-RenPySdk` から自動起動できる。
既存の実行中ゲームを使用する場合に限り、先に `AUTOADV_GAME_ORACLE` を設定して起動する。

原音終了後の保留時間は閾値による合否ではなく実測値として集計する。任意の診断基準と比較したい場合だけ `-MaxVoiceHoldMs` を指定できる（既定では基準なし）。これは製品の進行ロジックを変更しない。観測終了前の送りは実測区間に基づいて判定し、終了境界内の送りは「不確定」として早送り・安全進行のいずれにも数えない。過剰保留は実測値付きで判定する。文字のみ判断では原音終了を送り条件にしない。

## 実経路の評価範囲

通常の製品GUIでHook探索・選択と進行許可を行い、ゲームの自動送りはAutoAdvに任せる。
`run_live_check.ps1` はRen'Py SDKを指定したとき既存ゲームを起動するが、
GUI設定をテスト側で迂回するものではない。選択肢や手動停止・再許可は通常機能の契約として別途検査し、
**自動進行の合格要件にしない**。

```powershell
.\validation\real_game\run_live_check.ps1 -Phase hook -RenPySdk 'C:\path\to\renpy-sdk'
.\validation\real_game\run_live_check.ps1 -Phase reading -RenPySdk 'C:\path\to\renpy-sdk'
.\validation\real_game\run_live_check.ps1 -Phase native -RenPySdk 'C:\path\to\renpy-sdk'
.\validation\real_game\run_live_check.ps1 -Phase tts -RenPySdk 'C:\path\to\renpy-sdk'
.\validation\real_game\run_live_check.ps1 -Phase mixed -RenPySdk 'C:\path\to\renpy-sdk'
.\validation\real_game\run_live_check.ps1 -Phase stress -RenPySdk 'C:\path\to\renpy-sdk'
```

`native` はゲーム原音に対するASR/VAD帰属と終了判定、`mixed` はTTS・原音・閲覧時間の混在、
`stress` は不確実・背景発話による誤送りと停止を対象とする。未進行のまま終了した場合も
失敗・未確認を結果へ残す。特殊音声で安全停止したことを理由に、未到達の後続台詞を
検査済み扱いしない。

失敗・成功とも `run_live_check.ps1` は既定で `<Trace>.result.json` を保存する。
`line_results` の `no_autoadv_input` は成功でなく、
`game_evidence` の `game_dismissed_without_autoadv_input` は外部入力であって製品成功ではない。
実際のゲーム記録なしに単体の合成記録から実経路の合格を生成しない。

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
モデルまたはtokensの取得に失敗した場合、実モデルASR検証は**未検収**であり、FakeRecognizerやVAD単独観測の結果を代替しない。

## ASR特殊音声・背景ボイスの挑戦シナリオ（Irodori不要）

既存Ren'Pyゲームの `-Phase stress` 起動から、通常原音対照例・同時背景発話・
原音より後に続く背景声・背景声だけ・表示文と完全一致する背景声・泣き声・叫び声を検証できる。背景声は短い実録音 `normal_020.wav` のループを複数台詞にまたがって流し、混合PCMが本体の原音と区別できない条件を確認する。録音混合のASR検証でも同じ短い音声を反復する。
泣き声と叫び声の実録音はCC0音源を固定の48 kHz mono PCMに変換して同梱し、SHA-256で内容を検証する。取得元の403などを理由に検証を迂回しない。

```powershell
python -m validation.real_game.stress_asr --selection all --rounds 2 --output asr-stress.json
.\validation\real_game\run_live_check.ps1 -Phase stress -Oracle $env:AUTOADV_GAME_ORACLE
```

`stress_asr`は実モデルの推論時間・RTF・文字列誤認・背景声の誤帰属リスクを**観測するだけ**で、
ゲームの進行結果は認定しない。`-Phase stress`は実HOOK・PCM・ASRとAutoAdvの実際の進行状態を観測し、外部操作で進んだ台詞を自動進行として合格扱いしない。
**背景声が表示台詞と完全に一致する場合、Process LoopbackとテキストASRだけでは発話者を識別できない。**
したがって、このケースの誤完了はモデル品質の閾値調整で解決できたと解釈せず、製品の既知の識別限界を
検出する負荷検証とする。現在の製品がこのケースで安全停止できることは未確認であり、必ずPASSする条件として扱わない。

## 自動送りの実経路での時系列検収

録音PCMを`NativeAudio.feed`へ直接投入し、固定HOOKを`Controller.observe`へ渡して
自動入力を模擬する時系列検証は行わない。音声開始・HOOK到着遅延等の既知条件は
検証ゲーム側で設定可能だが、**結果の取得は通常GUIのLunaHook受信、実Process Loopback、
本体NativeAudio/Controller、Windows入力、次HOOKを通して行う**。

Windows上で `-RenPySdk` を指定した場合、テストスクリプトが既存ゲームを起動して通常GUIの実セッションを検収する。

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
