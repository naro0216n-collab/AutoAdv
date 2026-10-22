# AutoAdv 実音声・実ゲーム検証

この領域では、実録音による音声処理契約、検証基盤自身の契約、実ゲームを通した製品経路検収を分離する。ゲームエンジンのビルド、Ren'Py自体の正常性、配布物生成の成功をAutoAdvの合否へ混ぜない。

AutoAdv実経路で確認する対象は、LunaHookが受信した取得元と順序、本文・話者の解釈、Windows Process Loopbackからの原音観測、NativeAudio/Controllerの判断、Irodori TTSの実再生完了、明示許可を伴うWindows入力、入力後の次Hookである。検証ゲーム側は独立した時刻・再生状態だけを記録し、AutoAdvへHook、PCM、判断結果、入力命令を注入しない。

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

`validation/real_game` 全体への一括 `unittest discover` をRealAudio検証として使用しない。検証器の単体契約が通っても、Windows実ゲーム検収済みとは扱わない。

### 3. Windows実ゲーム製品経路

Live検証は**既に構築済みのWindowsゲーム**から開始する。`run_live_check.ps1` はRen'Py SDKの取得・コンパイル・配布物生成を行わない。ゲームPID/HWNDの検出、実LunaHost接続、実Hook候補からの校正、製品監視、自動送り許可、実Process Loopback、実ASR、実Windows入力、次Hookを通常のAutoAdv経路で実行する。

```powershell
.\validation\real_game\run_live_check.ps1 -Phase reading `
  -GameExecutable C:\path\to\AutoAdvValidationGame.exe -Automated

.\validation\real_game\run_live_check.ps1 -Phase native `
  -GameExecutable C:\path\to\AutoAdvValidationGame.exe -Automated `
  -ProcessLoopbackHelper C:\path\to\AutoAdv.ProcessLoopback.dll `
  -AsrModel C:\path\to\model.int8.onnx `
  -AsrTokens C:\path\to\tokens.txt
```

音声判断用のProcess Loopback helper、ASR model、tokensは製品経路の実依存物なので、Live実行より前に用意する。`run_live_check.ps1` 自身はそれらをビルド・取得しない。ゲーム入力は常にAutoAdvのControllerとWindows入力が担当し、検証driverは入力を代行しない。

ローカルWindows検証の配布用 `AutoAdv-Windows-Validate.cmd` は検証実装を内蔵しない。起動時に最新 `develop` を隔離checkoutし、Git管理下の `validation/real_game/run_windows_validation.ps1` を実行するだけのbootstrapとする。prebuilt fixture取得、Process Loopback helper build、固定ASR runtime準備、各phase実行、Issueへの結果送信はGit側のPowerShellが所有する。このため検証経路を改修した場合にCMDを再配布して手順を二重管理しない。

## Prebuilt validation game

`validation/real_game/renpy/game/` が検証ゲームの正準ソースである。実行用fixtureは `.github/workflows/validation-fixture.yml` がこのソースから独立に生成し、専用のpre-release `autoadv-validation-fixtures` へソースSHA-256を含むZIPとして保存する。fixtureを変更した場合はValidation Fixture Runtimeを明示実行して対応assetを生成する。通常publishやFast CIから重量級fixture buildを自動連鎖させない。このpre-releaseはAutoAdv製品のversionやreleaseではなく、検証用binaryの固定保管場所である。

Live Advancement workflowは現行fixtureソースSHA-256と一致するassetだけを取得し、`fixture-manifest.json` では次の最小条件だけを確認してからゲームを起動する。

- 現行 `renpy` fixture入力の正規化SHA-256（`.rpy`/`.txt` の改行差は同一内容として扱う）
- 起動対象Windows executableの存在

Ren'Py版と固定SDK SHA-256は生成元の診断情報としてmanifestへ残すが、展開後runtime全体の再ハッシュは行わない。配布ZIPの転送整合性やGitHub Releaseの保管を重ねて独自検証することはAutoAdvの製品成立条件ではなく、実ゲーム検証を検証器自身の差異で失敗させるためである。

fixtureソースとprebuilt runtimeが一致しない、または対応assetが存在しない場合はAutoAdv製品失敗ではなく検証前提不成立として停止する。Live workflow内で代わりにRen'Py SDKを取得してゲームを再構築しない。

校正用の非対話台詞 `AutoAdvフック校正専用` は測定対象外である。ゲームはAutoAdvのHook接続完了を待ってこの台詞を一度出し、検証driverはApplicationが保持する実取得候補から本文取得元を選択してCapture Workspaceへ確定する。その後に監視と自動送りを開始する。同一校正台詞を複数の実Hookが出した場合は決定的に一つを選び、その取得元が後続実台詞を取得できたかで評価する。

## 収録コンテンツ・権利

`renpy/game/` は短い独立した検証ゲームの素材一式である。話者「明日香」は通常声、「栞」はささやき声、「主人公」は無音声。通常音声・ささやき音声はあみたろの声素材工房、文章はITAコーパスを使用する。詳細は `renpy/LICENSE_*_ja.txt`、`renpy/CREDITS.txt`、`RIGHTS_AND_SCOPE.md` を参照する。

音声SHA-256、台詞、ファイルパスは `VOICE_MANIFEST.json` に固定する。通常声とささやき声は同一録音者であり、別話者互換性の検証ではない。音声単体素材集として再配布しない。

## 実ゲームphase

`reading` は文字時間による通常送り、`native` はTTS非使用の原音3件から開始する。`tts` は実IrodoriサーバーとVoiceを必要とする。`mixed` は原音・TTS・文字待機の混在、`stress` は背景発話、非言語発声、誤帰属、不確実状態を扱う。

`native` / `mixed` / `stress` ではゲーム側oracleによる原音再生終了の独立観測を必要とする。`hook` / `reading` / `tts` では原音終了oracleを合否条件にしない。ゲーム側oracleは `autoadv_oracle.rpy` にあり、台詞表示、再生状態、dismissalを記録するだけでゲーム進行を変更しない。

原音が不確実なケースでテスト側が入力して後続場面へ進めることは禁止する。安全停止した時点で未到達の後続台詞を検査済み扱いしない。`line_results` の `no_autoadv_input` や外部dismissalを製品成功へ読み替えない。

## ASRモデルの準備

検証用モデルはGitワークツリーや通常利用設定へ格納せず、Windowsでは既定で `%LOCALAPPDATA%\AutoAdv\validation\asr` に隔離する。版とSHA-256は `ASR_MODEL.json` に固定し、不一致の既存ファイルは上書きせず失敗する。

通常起動、Fast CI、Windows Adapter Validationではモデルを新規取得しない。一方、**明示的に起動するAutoAdv Live Advancementでは、実NativeAudio経路を成立させる前提として固定manifestのASR model/tokensを検証キャッシュへ必要時取得してよい**。これはASR品質ベンチマークをCI合否へ導入することではなく、製品が実際に使用する固定runtime依存物の準備である。

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

背景発話が表示文と一致するケース、泣き声、叫び声等はモデルの認識率だけで安全性を断定しない。Process Loopbackの混合PCM、ASR対応、VAD末尾、Controller状態、実入力、ゲーム側独立観測を分けて評価する。

## GitHub上の実行経路

- `develop` push: Fast CI。fixtureや実ゲームを起動しない。
- fixture構築入力の変更時: Validation Fixture Runtime。専用pre-releaseへprebuilt game assetを生成する。AutoAdvの合否判定はしない。
- 検証用draft PRの close → reopen: Windows Adapter Validation。
- PRタイトルへ `[RUN LUNA NATIVE]` を付ける編集: Luna Native Source and ABI Validation。
- PRタイトルを `[RUN AUTOADV LIVE]...` にする編集: AutoAdv Live Advancement。prebuilt fixtureを取得し、reading/nativeの実製品経路を実行する。

検証用PRはマージしない。Fast CI、Windows adapter、Luna native、Live Advancement、ASR品質測定は証拠階層が異なり、相互に合格を代用しない。
