# AutoAdv 実音声・実ゲーム検証

この領域では、実録音による音声処理契約、検証基盤自身の契約、実ゲームを通した製品経路検収を分離する。ゲームエンジンのビルド、Ren'Py自体の正常性、配布物生成の成功をAutoAdvの合否へ混ぜない。

AutoAdv実経路で確認する対象は、LunaHookが受信した取得元と順序、本文・話者の解釈、Windows Process Loopbackからの原音観測、NativeAudio/Controllerの判断、Irodori TTSのWindows音声システム上の再生完了、明示許可を伴うWindows入力、入力後の次Hookである。検証ゲーム側は独立した時刻・再生状態だけを記録し、AutoAdvへHook、PCM、判断結果、入力命令を注入しない。物理スピーカーやヘッドホンから音が可聴であることはRealAudioの成立条件ではなく、ゲームまたはAutoAdvの実再生ストリームとProcess Loopbackの実PCMがシステム上で成立していることを扱う。

## 検証層

### 1. 実録音NativeAudio契約

実録音をProcess Loopback互換PCMへ変換し、AutoAdvの既存NativeAudioへ投入する局所契約である。ゲーム、Ren'Py、LunaHook、Windows入力を起動しない。

```powershell
python -m unittest validation.real_game.test_native_audio -v
```

この結果はVAD/NativeAudioの不変条件を確認するもので、実Process Loopback捕捉や実ゲーム自動送りの成立を証明しない。ASR品質・速度の測定も別扱いとする。

### 2. 検証基盤の契約

fixture manifest、ゲームoracle、trace verifier、live-session adapter等の「検証器自身」の契約は実録音テストとは別に実行する。

```powershell
python -m unittest `
  validation.real_game.test_fixture_runtime `
  validation.real_game.test_gui_session `
  validation.real_game.test_trace_contract `
  validation.real_game.test_e2e_verifier `
  validation.real_game.test_game_oracle `
  validation.real_game.test_stress_verifier -v
```

`validation/real_game` 全体への一括 `unittest discover` をRealAudio検証として使用しない。検証器の単体契約が通っても、Windows実ゲーム計測済みとは扱わない。

### 3. Hosted WindowsのHOOK・文字送り経路

HostedのAutoAdv Live Advancementは**既に構築済みのWindowsゲーム**から開始する。`run_live_check.ps1` はRen'Py SDKの取得・コンパイル・配布物生成を行わない。Hosted側では`reading` phaseだけを実行し、ゲームPID/HWNDの検出、実LunaHost接続、実Hook候補からの校正、製品監視、自動送り許可、文字判断、実Windows入力、次Hookを通常のAutoAdv経路で確認する。Process Loopback、ASR、NativeAudioはHostedの合否条件に含めない。

```powershell
.\validation\real_game\run_live_check.ps1 -Phase reading `
  -GameExecutable C:\path\to\AutoAdvValidationGame.exe -Automated
```

Hosted検収のゲーム入力は常にAutoAdvのControllerとWindows入力が担当し、検証driverは入力を代行しない。Hostedでこの経路が成立してもRealAudioを計測済みとは扱わない。RealAudioだけは、製品が自力で次ケースへ到達できない保留の後も全ケースの観測を続けるため、結果を記録した後の回復入力を許容する。この入力は製品成功へ算入しない。

### 4. Windows実機RealAudio計測

RealAudioはGitHub hosted runnerでは実行せず、実際にAutoAdvを利用する対話可能なWindows実機で自動進行精度を計測する。明示的な局所実行では次のNative phaseを使える。

```powershell
.\validation\real_game\run_live_check.ps1 -Phase native `
  -GameExecutable C:\path\to\AutoAdvValidationGame.exe -Automated -MeasurementOnly `
  -ProcessLoopbackHelper C:\path\to\AutoAdv.ProcessLoopback.dll `
  -AsrModel C:\path\to\model.int8.onnx `
  -AsrTokens C:\path\to\tokens.txt
```

通常の実機計測は配布用 `AutoAdv-Windows-Validate.cmd` を使う。このCMDは検証実装を内蔵せず、起動時に最新 `develop` を隔離checkoutし、Git管理下の `validation/real_game/run_windows_validation.ps1` を実行するだけのbootstrapとする。Git側PowerShellがprebuilt fixture取得、Process Loopback helper build、固定ASR runtime準備、`reading` / `native` phase、Issueへの結果送信を所有する。両phaseはmeasurement modeで実行し、一方の品質低下を理由に他方をskipしない。`native` は全原音ケースを一つのゲーム・一つのAutoAdvセッションで連続実行する。AutoAdv自身の送りが成立しない台詞ではその観測結果を保持し、検証driverが`validation_recovery_input`を記録してから一度だけゲームを進め、後続ケースのPCM/ASR/VAD/Controller観測を継続する。回復入力後の次HookはAutoAdv成功に数えず、要求ケース数に対する自動進行率、安全な自動進行率、回復率、早送り率、過剰保留率、誤帰属率を報告する。品質値に固定合格点は置かず、trace/oracle破損、実音声経路やfixtureの未成立、要求ケースの収集未完了など計測不能だけをrunner異常とする。強制入力の直前に`confirm_external_advance()`で既知の境界だけを通知し、同一本文の後続ケースも収集可能にするが、AutoAdvの送り成功や音声判断へは算入しない。ケース別再起動は行わない。`run_live_check.ps1` 自身はhelper buildやASR取得を行わない。このため検証経路を改修した場合にCMDを再配布して手順を二重管理しない。

RealAudioはFast CI、Windows Adapter Validation、AutoAdv Live Advancement、その他の一括／Fullテストへ含めない。Hosted runnerの音声endpoint有無を製品のNativeAudio成否に読み替えない。

## Prebuilt validation game

`validation/real_game/renpy/game/` が検証ゲームの正準ソースである。実行用fixtureは `.github/workflows/validation-fixture.yml` がこのソースから独立に生成し、専用のpre-release `autoadv-validation-fixtures` へソースSHA-256を含むZIPとして保存する。fixture構築入力または配布契約が`develop`へpublishされた場合だけPublish GatewayがValidation Fixture Runtimeを明示dispatchし、通常の製品publishやFast CIでは重量級fixture buildを反復しない。`workflow_dispatch`は欠損asset再生成用の回復経路である。Windows実機RealAudioとHosted Live Advancementはともに`python -m validation.real_game.fixture_runtime stage`を使用し、現行ソースdigestに対応するassetがpublish直後でまだ生成中なら一定時間待機した上で、同一assetだけを取得・展開・manifest照合する。release名、asset命名、URL組立はこのPython契約が所有し、consumer側のPowerShell/workflowへ重複させない。このpre-releaseはAutoAdv製品のversionやreleaseではなく、検証用binaryの固定保管場所である。

Live Advancement workflowも独自のRelease download手順を持たず、同じ`fixture_runtime stage`から現行fixtureソースSHA-256と一致するassetだけを取得する。`fixture-manifest.json` では次の最小条件だけを確認してからゲームを起動する。

- 現行 `renpy` fixture入力の正規化SHA-256（`.rpy`/`.txt` の改行差は同一内容として扱う）
- 起動対象Windows executableの存在

Ren'Py版と固定SDK SHA-256は生成元の診断情報としてmanifestへ残すが、展開後runtime全体の再ハッシュは行わない。配布ZIPの転送整合性やGitHub Releaseの保管を重ねて独自検証することはAutoAdvの製品成立条件ではなく、実ゲーム検証を検証器自身の差異で失敗させるためである。

fixtureソースとprebuilt runtimeが一致しない、または対応assetが存在しない場合はAutoAdv製品失敗ではなく検証前提不成立として停止する。Live workflow内で代わりにRen'Py SDKを取得してゲームを再構築しない。

校正用の非対話台詞 `AutoAdvフック校正専用` は測定対象外である。実台詞と同じ話者付き構造で一度出し、検証driverはApplication自身が各取得元へ適用した構造抽出Previewで本文がこの校正文へ確定する候補だけを本文取得元候補とする。生文字列に校正文を含むだけでは採用しない。本文だけを返す候補を優先し、話者＋本文を返す候補しかない場合もApplicationの通常`auto`抽出で本文が校正文へ分離できるものは利用できる。確定後に監視と自動送りを開始し、その取得レシピが後続実台詞でも成立したかで評価する。

## 収録コンテンツ・権利

`renpy/game/` は短い独立した検証ゲームの素材一式である。話者「明日香」は通常声、「栞」はささやき声、「主人公」は無音声。通常音声・ささやき音声はあみたろの声素材工房、文章はITAコーパスを使用する。詳細は `renpy/LICENSE_*_ja.txt`、`renpy/CREDITS.txt`、`RIGHTS_AND_SCOPE.md` を参照する。

音声SHA-256、台詞、ファイルパスは `VOICE_MANIFEST.json` に固定する。通常声とささやき声は同一録音者であり、別話者互換性の検証ではない。音声単体素材集として再配布しない。

## 実ゲームphase

`reading` は文字時間による通常送り、`native` はTTS非使用の原音16件を同一セッションで連続実行する。説明文と手動選択肢はnative専用fixture条件では表示せず、各原音台詞はAutoAdvの通常Controller入力だけで次へ進む。`tts` は実IrodoriサーバーとVoiceを必要とする。`mixed` は原音・TTS・文字待機の混在、`stress` は背景発話、非言語発声、誤帰属、不確実状態を扱う。

`native` / `mixed` / `stress` ではゲーム側oracleによる音声エンジン上の原音再生終了の独立観測を必要とする。物理出力の可聴性は要求しない。`hook` / `reading` / `tts` では原音終了oracleを合否条件にしない。ゲーム側oracleは `autoadv_oracle.rpy` にあり、台詞表示、再生状態、dismissalを記録するだけでゲーム進行を変更しない。

原音が不確実なケースでテスト側が入力して後続場面へ進めることは禁止する。安全停止した時点で未到達の後続台詞を検査済み扱いしない。`line_results` の `no_autoadv_input` や外部dismissalを製品成功へ読み替えない。

## ASRモデルの準備

検証用モデルはGitワークツリーや通常利用設定へ格納せず、Windowsでは既定で `%LOCALAPPDATA%\AutoAdv\validation\asr` に隔離する。版とSHA-256は `ASR_MODEL.json` に固定し、不一致の既存ファイルは上書きせず失敗する。

通常起動、Fast CI、Windows Adapter Validation、AutoAdv Live Advancementではモデルを新規取得しない。**固定manifestのASR model/tokensを検証キャッシュへ必要時取得するのは、明示的に起動したWindows実機RealAudio計測だけ**とする。これはASR品質ベンチマークではなく、実機NativeAudio経路が実際に使用するruntime依存物の準備である。

ローカルで明示準備する場合は次を使う。

```powershell
python -m validation.real_game.prepare_asr_model
python -m validation.real_game.prepare_asr_model --verify-only
```

モデルまたはtokensを準備できなければ実NativeAudio検収は未検収であり、FakeRecognizerやVAD単独結果で代替しない。

## ASR品質・速度とstress計測

CER、完全一致率、RTFは原因分析用の測定値で、自動進行の合否条件ではない。品質・速度測定はFast CIやWindows Adapter Validationから外し、必要時に実利用Windows環境で明示実行する。

```powershell
python -m validation.real_game.benchmark_asr `
  --conditions clean quiet noise clipped truncated bgm `
  --rounds 2 --output .\asr-performance.json

python -m validation.real_game.stress_asr `
  --selection all --rounds 2 --output .\asr-stress.json
```

背景発話が表示文と一致するケース、泣き声、叫び声等はモデルの認識率だけで安全性を断定しない。Process Loopbackの混合PCM、発音を含むASR対応、ASR token終端、VADの補助的音響証拠、Controller状態、実入力、ゲーム側独立観測を分けて評価する。VAD末尾無音は必須条件にしない。

## GitHub上の実行経路

- `develop` publish: Publish GatewayがFast CIを明示dispatchする。fixture構築入力に差分がある場合だけValidation Fixture Runtimeも明示dispatchし、通常の変更ではfixtureや実ゲームを起動しない。
- fixture構築入力の変更時: Validation Fixture Runtimeが専用pre-releaseへprebuilt game assetを生成する。AutoAdvの合否判定はしない。`workflow_dispatch`は欠損assetの回復用。
- 検証用draft PRの close → reopen: Windows Adapter Validation。
- PRタイトルへ `[RUN LUNA NATIVE]` を付ける編集: Luna Native Source and ABI Validation。
- PRタイトルを `[RUN AUTOADV LIVE]...` にする編集: AutoAdv Live Advancement。prebuilt fixtureを取得し、Hostedの`reading`実製品経路だけを実行する。
- RealAudio: GitHub Actions外で`AutoAdv-Windows-Validate.cmd`を実際のWindowsデスクトップから明示実行し、`reading`と`native`を計測する。

検証用PRはマージしない。Fast CI、Windows adapter、Luna native、Hosted Live Advancement、Windows実機RealAudio、ASR品質測定は証拠階層が異なり、相互の証拠を代用しない。
