# AutoAdv 実音声・実ゲーム検証

この領域では、局所的なNativeAudio契約、検証基盤自身の契約、Hosted Windowsの文字送り経路、Windows実機の自動進行精度計測を分離する。Ren'Pyのビルド成功や検証器の単体テストをAutoAdv実機成立の代用にしない。

AutoAdv実経路で確認する対象は、LunaHookが実際に返したraw文字列、AutoAdvがそこから生成したUtterance、製品内推論イベント、明示許可を伴うWindows入力である。検証ゲーム側は、自分がどのfixture caseを開始し、どの本文を提示し、どのvoice fileをvoice channelへ再生指示したかという刺激事実だけを記録する。voice channel、mix、音響終端、早送り、安全性等を検証側で再推論しない。

## 検証層

### 1. NativeAudio局所契約

NativeAudioのcontinuous PCM provenance、Presentation Epoch境界、世代失効、target-conditioned alignment・human-vocal・recurrence observerから`UtteranceProgress`への引き渡し、transport観測と進行推論の分離を局所契約として確認する。ゲーム、Ren'Py、LunaHook、Windows入力は起動しない。

```powershell
python -m pytest -q tests/test_audio_lifecycle_contracts.py tests/test_domain_contracts.py
```

これは音声判断のDomain契約を確認するもので、実Process Loopback捕捉や実ゲーム自動送りの成立を証明しない。実録音はalignment benchmarkとWindows実機計測で扱う。

### 2. 検証基盤の契約

fixture manifest、fixture evidence、product trace、live-session adapter等は証拠収集システムの契約としてまとめて検証する。検証基盤自身が製品状態を説明する第二の状態機械を持たないことも契約に含める。

```powershell
python -m pytest -q validation/real_game/test_measurement_contracts.py
```

この契約が通ってもWindows実ゲーム計測済みとは扱わない。

### 3. Hosted WindowsのHOOK・文字送り経路

AutoAdv Live Advancementはprebuilt Windows fixtureを使い、`reading` phaseだけを実行する。ゲームPID/HWND検出、実LunaHost接続、実Hook候補からの校正、Controllerの本文時間判断、実Windows入力、次Hookを確認する。Process Loopback、target-conditioned alignment、NativeAudioはこのHosted経路の合否条件に含めない。

```powershell
.\validation\real_game\run_live_check.ps1 -Phase reading `
  -GameExecutable C:\path\to\AutoAdvValidationGame.exe -Automated
```

### 4. Windows実機の自動進行精度計測

`AutoAdv-Windows-Validate.cmd`は実際にAutoAdvを利用する対話可能なWindows実機で明示実行する。通常計測は`accuracy` phaseだけを使い、Hosted `reading`を重複実行しない。

```powershell
.\validation\real_game\run_live_check.ps1 -Phase accuracy `
  -GameExecutable C:\path\to\AutoAdvValidationGame.exe -Automated
```

標準ケースは**件数を固定契約にせず、一つのゲームプロセス・一つの選択HOOK・一つのAutoAdvセッションで全ケースを連続実行**する。fixtureは音源役割ではなく、`text_correspondence`、`finite_vocal_activity`、`finite_vocal_repetition`、`vocal_loop`、`vocal_crosses_boundary`、`delayed_vocal_onset`という別個のfixture事実の組合せで入力同値クラスを表す。同一ケースで複数軸が同時に成立してよく、これらはAutoAdvへ入力しない。加えて実発声開始より遅いHOOK、同一本文を異なる時間で演じるpaired case等の外部時間条件を組み合わせる。場面境界でゲーム再起動、HOOK再選択、NativeAudio/`UtteranceProgress`再初期化、profile再設定、検証用状態clearは行わない。HOOK遅延はゲーム側でvoiceを先に再生してから通常のLunaHook経路へ台詞を出し、製品へ固定HOOK通知を注入しない。

通常短文・長文、複数話者、囁き、泣き・叫び・喘ぎ相当、BGM、群衆会話、本文非対応の日本語人声、同文連続、ボイス途中から開始するSE、SE残響、機械環境音、本文量と原音durationが相関しない条件を組み合わせる。これらは上記fixture事実と外部刺激の条件であり、検証側が「どの音源が現在台詞か」「音声終了したか」を再推論するoracleには使用しない。

accuracy scenarioが`game_input_after_ms`を持つケースでは、validation driverはfixture case開始から固定時間後に通常のWindowsキーボード入力を送り、`validation_game_input`として記録する。製品状態を直接変更せず、foreground化も強制しないため、その刺激が実際に対象ゲームへ配送されたかは`WindowsGameInputObserver`の`game_input_observed`とfixture側の進行事実から独立に確認する。authored input数に対して製品observerの観測数が不足するrunは証拠不成立として扱う。製品の外部入力検出、保持音響、ゲーム側での実際の進行は独立した事実としてのみ扱い、外部入力を音響区間下限へ昇格させたり近接時刻から対応を捏造したりしない。製品の進行状態・音声判断・HOOK状態を参照して刺激時刻を変えない。後続ケースを隠さないための`validation_recovery_input`は引き続き対象HWNDへの`PostMessage`で送り、製品の外部入力観測へ混入させない。回復入力は、同じfixture caseが12秒変化しない場合だけ一度送るwatchdogで、`pause_reason`、`UtteranceProgress`、Hook状態、voice channel等を参照しない。いずれも製品成功へ読み替えず、runnerが失敗とするのはtrace/evidence破損や要求fixture case未到達等の証拠収集不成立だけである。

自動進行精度実機計測はFast CI、Windows Adapter Validation、AutoAdv Live Advancement、その他の一括／Fullテストへ含めない。

## 標準Windows runtime

forced alignment/G2P/human-vocal observer/Process Loopbackを検証専用環境として持たない。通常の`AutoAdv.cmd`は`%LOCALAPPDATA%\AutoAdv\runtime`にAutoAdv管理runtimeを準備し、GUI・audio・OCR・native依存、Haqumei G2P、音響observer、固定target-conditioned alignment model/vocabularyを自動配置する。Process Loopback helperも同じ管理領域へhelperソースdigest単位で配置し、既に成立しているruntimeを毎回再構築しない。

alignment modelの固定repository/revision/SHA-256は`autoadv.native_runtime`が所有する。精度計測、alignment benchmark、通常製品は同じmanaged model/vocabularyを使用し、検証専用model downloaderや第二の配置正本を持たない。個別パスは必要な場合だけ詳細設定から上書きできる。

## Prebuilt validation game

`validation/real_game/renpy/game/`がfixture正準ソースである。`.github/workflows/validation-fixture.yml`はfixture入力が変わったときだけPublish Gatewayから明示dispatchされ、専用pre-releaseへprebuilt Windows fixtureを保存する。通常publishやFast CIでRen'Py SDK取得・fixture再コンパイルを反復しない。

Windows実機の自動進行精度計測とHosted Live Advancementは共通の`python -m validation.real_game.fixture_runtime stage`で現行ソースdigestに対応するfixtureだけを取得する。各実機runでは展開したfixtureを一時利用し、計測終了後に削除する。完了済みの旧runに検証専用`venv`や展開済み`validation-game`が残っている場合も次回計測で回収する。永続保存するのはproduct trace、fixture evidence、report、summary、transcript等の証拠であり、venv、fixture展開物、build outputではない。

## 精度計測専用メディア

`ADVANCEMENT_ACCURACY_ASSETS.json`は精度計測でゲーム側から再生する外部メディアだけを固定する。巨大archiveから一部を展開せず、JVS公式個別sampleと個別配布BGM・ambient・SEを直接取得する。各ファイルはURL、SHA-256、最大サイズ、runtime配置先、権利情報を固定し、`.autoadv-validation/cache/media`へ一度だけ取得して再利用する。

JVS等の再配布不可素材はGit checkout、prebuilt fixture、GitHub artifact、Issue uploadへ含めない。素材metadataとfixtureの音響事実は検証結果の層別化にだけ使い、AutoAdvの判断へ入力しない。

## Issue診断面

Windows実機計測のGitHub Issueは、チャット環境から解析できるリモート証拠面として使う。4個の固定履歴slotをリングとして扱い、通常runは同一runのslotまたは最古slotの1件だけをPATCHする。検証器が推論した「known-good baseline」は維持しない。

Issueはローカルrun directoryの複製先ではなく、通常の原因解析に必要な証拠面とする。常時共有するのは`validation-summary.json`、offline verifier report、完全なfixture evidenceと、全Presentationを含むproduct analysis traceである。完全な周期product traceはローカルrun directoryを正本として保持し、Issueへは`presentation-analysis-jsonl-v1`として、Luna上流raw出力、選択Hook事実、Utterance、外部入力・Controller操作、統合音声判断のstate遷移、各遷移時とPresentation終端時の音響観測、observerの全Presentation集計、alignmentの状態変化と性能集計を共有する。失敗ケースだけへ切り詰めず全Presentationを残す一方、同じ状態の周期観測やinferenceを全件送らない。したがって共有量は観測polling頻度ではなくPresentation数、実際の状態変化、異常事実に主として比例する。各共有時刻・seqは元traceの値を保持し、集計されていない完全な測定列はローカルtraceから失われない。JSON report/summaryは情報を落とさず列形式またはcompact JSONで共有し、summaryとoffline reportで同一のfactsはreport側を一度だけ保持してsummaryから参照する。manifestにはsanitized sourceのSHA-256、source文字数、共有文字数、表現形式を記録する。Issue送信対象はこの契約で列挙した証拠に限定し、run directoryへ別の`autoadv-*`診断が増えても自動では混入させない。

`validation.log`はlauncher/runner/report収集異常の診断用であり、正常に計測が完了したrunではIssueへ送らない。`autoadv-human-vocal.result.json`はローカルには完全保存するが、Issueではdigestを常時共有する。同じdigestが固定履歴内ですでに共有済みの正常診断はdigest参照だけとし、未共有digestまたは診断収集自体が空・error等で成立していない場合だけobserver/file数等の要約と完全な詳細を添付する。これはobserver能力を製品合否へ昇格させる規則ではなく、同一診断結果の反復送信を避けるtransport契約である。

完全なproduct trace、fixture evidence、report、summary、transcript、observer診断はローカルrun directoryに残す。ローカル保存はIssue送信失敗時にも失われない耐障害正本であり、Issue更新は計測後の共有処理とする。GitHub側の送信失敗だけでローカルに成立した精度計測を失敗へ変更しない。

## Alignment品質・stress計測

既知本文のassociation率、固定clean-source annotationに対するlexical境界誤差、alignment未確認率、推論費用を測り、自動進行精度とは分離して扱う。これらはmodel/backendの品質診断であり、association率やcommit率そのものを製品合格条件にはしない。full fileを一度だけalignせず、製品と同じ累積snapshot、単一workerの推論backpressure、PCM idle時の未解析tail drainを再生し、既知本文へどの範囲を対応付けたかを評価する。forced alignmentの`committed target end`はlexical/phonetic evidenceの品質値であり、human vocal event終了や`UtteranceProgress`成功とは扱わない。tail drainはheartbeatを音声終了証拠へ使わず、既に捕捉済みのPCMをforced alignmentへ渡すscheduler動作だけを再現する。`ALIGNMENT_ENDPOINT_ORACLE.json`は製品aligner出力を使わずclean source波形から固定した独立annotationであり、音声file端とは区別する。必要時にmanaged alignment runtimeを使ってWindows実機で明示実行する。

```powershell
python -m validation.real_game.benchmark_alignment --conditions clean quiet noise clipped truncated bgm --rounds 2 --output .\alignment-performance.json

`accuracy`実機runでは、同じmanaged YAMNet/TEN-VADをstaged fixtureの孤立WAVへ直接適用した`autoadv-human-vocal.result.json`も診断証拠として保存する。これはprocess mixやPresentation帰属を含まないobserver能力の測定であり、閾値や製品合否をfixtureから自動決定しない。実ゲームmixでの`native_audio_observation`と比較し、「observer自体が情報を出していない」のか「mix・統合・帰属で失われた」のかを分離するために使う。
python -m validation.real_game.stress_alignment --selection all --rounds 2 --output .\alignment-stress.json
```

## GitHub上の実行経路

- `develop` publish: Fast CI。Domain/Application/GUI契約とclean package install、復元用source snapshotを扱う。
- fixture入力変更時: Validation Fixture Runtime。prebuilt fixture生成だけを扱う。
- 検証用draft PRのclose → reopen: Windows Adapter Validation。Windows固有adapter境界だけを確認し、Linux Fast CIと同じDomainテスト全件を再実行しない。
- PRタイトルに`[RUN LUNA NATIVE]`: Luna Native Source and ABI Validation。
- PRタイトルを`[RUN AUTOADV LIVE]...`: Hosted `reading`実経路。
- `AutoAdv-Windows-Validate.cmd`: GitHub Actions外のWindows実機自動進行精度計測。

各証拠階層は相互に代用しない。
