# AutoAdv 実音声・実ゲーム検証

この領域では、局所的なNativeAudio契約、検証基盤自身の契約、Hosted Windowsの文字送り経路、Windows実機の自動進行精度計測を分離する。Ren'Pyのビルド成功や検証器の単体テストをAutoAdv実機成立の代用にしない。

AutoAdv実経路で確認する対象は、LunaHookが受信した取得元と順序、本文・話者の解釈、Windows Process Loopbackからの音声観測、NativeAudio/Controllerの判断、Irodori TTSのWindows音声システム上の再生完了、明示許可を伴うWindows入力、入力後の次Hookである。検証ゲーム側は独立した時刻・voice再生状態だけを記録し、AutoAdvへHook、PCM、判断結果、入力命令を注入しない。

## 検証層

### 1. NativeAudio局所契約

NativeAudioのcontinuous PCM provenance、Presentation Epoch境界、世代失効、target-conditioned alignment・human-vocal・recurrence observerから`UtteranceProgress`への引き渡し、transport観測と進行推論の分離を局所契約として確認する。ゲーム、Ren'Py、LunaHook、Windows入力は起動しない。

```powershell
python -m pytest -q tests/test_audio_lifecycle_contracts.py tests/test_domain_contracts.py
```

これは音声判断のDomain契約を確認するもので、実Process Loopback捕捉や実ゲーム自動送りの成立を証明しない。実録音はalignment benchmarkとWindows実機計測で扱う。

### 2. 検証基盤の契約

fixture manifest、ゲームoracle、trace verifier、live-session adapter等は計測システムの契約としてまとめて検証する。

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
  -GameExecutable C:\path\to\AutoAdvValidationGame.exe -Automated -MeasurementOnly
```

標準ケースは**件数を固定契約にせず、一つのゲームプロセス・一つの選択HOOK・一つのAutoAdvセッションで全ケースを連続実行**する。通常のclean/mix場面に加え、実発声開始より遅いHOOK、同一本文を異なる時間で演じるpaired case、長い非言語tail、内部短無音を含む非言語発声、epoch内開始とepoch跨ぎのvocal loop、有限の自然反復を含める。場面境界でゲーム再起動、HOOK再選択、NativeAudio/`UtteranceProgress`再初期化、profile再設定、検証用状態clearは行わない。HOOK遅延はゲーム側でvoiceを先に再生してから通常のLunaHook経路へ台詞を出し、製品へ固定HOOK通知を注入しない。

原音ありでは通常短文・長文、複数話者、囁き、泣き・叫び・喘ぎ相当、BGM、群衆会話、日本語背景発話、同文連続、ボイス途中から開始するSE、SE残響、機械環境音に加え、本文量と原音durationが相関しない条件を測る。原音なしは複数のmixへ分散し、accuracyで常時ONのフォールバックが`UtteranceProgress`成功とは別理由で働くことと、背景音・背景人声・loopを有限targetへ誤帰属しないことを測る。台詞境界をまたぐBGM・環境音・SE・persistent vocalは意図的に保持する。独立game oracleはisolated foreground WAVからclean-source acoustic endpointを算出し、実際のsource開始時刻をHOOK到着とは独立に記録する。AutoAdv入力がvoice channelを途中停止させても、開始時に固定した独立source acoustic endpointとの比較で早送りを判定できる。music/ambient/background/SFXの実再生も記録し、`ADVANCEMENT_ACCURACY_SCENARIO.json`の想定mixが実際に成立しなかったrunは完全な計測として扱わない。

途中で保留、早送り、誤帰属、入力失敗、次Hook未取得等になっても品質値として記録し、後続ケースの実測を失わないためだけに`validation_recovery_input`を送って収集を継続できる。回復入力はAutoAdvの成功へ算入しない。固定合格点は置かず、trace/oracle破損、fixture・実音声経路の未成立、要求ケースの収集未完了等だけを計測不能としてrunner異常にする。

自動進行精度実機計測はFast CI、Windows Adapter Validation、AutoAdv Live Advancement、その他の一括／Fullテストへ含めない。

## 標準Windows runtime

forced alignment/G2P/human-vocal observer/Process Loopbackを検証専用環境として持たない。通常の`AutoAdv.cmd`は`%LOCALAPPDATA%\AutoAdv\runtime`にAutoAdv管理runtimeを準備し、GUI・audio・OCR・native依存、Haqumei G2P、音響observer、固定target-conditioned alignment model/vocabularyを自動配置する。Process Loopback helperも同じ管理領域へhelperソースdigest単位で配置し、既に成立しているruntimeを毎回再構築しない。

alignment modelの固定repository/revision/SHA-256は`autoadv.native_runtime`が所有する。精度計測、alignment benchmark、通常製品は同じmanaged model/vocabularyを使用し、検証専用model downloaderや第二の配置正本を持たない。個別パスは必要な場合だけ詳細設定から上書きできる。

## Prebuilt validation game

`validation/real_game/renpy/game/`がfixture正準ソースである。`.github/workflows/validation-fixture.yml`はfixture入力が変わったときだけPublish Gatewayから明示dispatchされ、専用pre-releaseへprebuilt Windows fixtureを保存する。通常publishやFast CIでRen'Py SDK取得・fixture再コンパイルを反復しない。

Windows実機の自動進行精度計測とHosted Live Advancementは共通の`python -m validation.real_game.fixture_runtime stage`で現行ソースdigestに対応するfixtureだけを取得する。各実機runでは展開したfixtureを一時利用し、計測終了後に削除する。完了済みの旧runに検証専用`venv`や展開済み`validation-game`が残っている場合も次回計測で回収する。永続保存するのはtrace、report、oracle、summary、transcript等の計測証拠であり、venv、fixture展開物、build outputではない。

## 精度計測専用メディア

`ADVANCEMENT_ACCURACY_ASSETS.json`は精度計測でゲーム側から再生する外部メディアだけを固定する。巨大archiveから一部を展開せず、JVS公式個別sampleと個別配布BGM・ambient・SEを直接取得する。各ファイルはURL、SHA-256、最大サイズ、runtime配置先、権利情報を固定し、`.autoadv-validation/cache/media`へ一度だけ取得して再利用する。

JVS等の再配布不可素材はGit checkout、prebuilt fixture、GitHub artifact、Issue uploadへ含めない。素材分類や`environment`/`speaker_family`は検証結果の層別化metadataであり、AutoAdvの判断へ入力しない。

## Issue診断面

Windows実機計測のGitHub Issueは、チャット環境から比較・解析できるリモート診断面として使う。4個の固定履歴slotをリングとして扱い、通常runは同一runのslotまたは最古slotの1件だけをPATCHする。別に直近の完全計測・recoveryなし・voice safety成立runをbaselineとして保持する。

各履歴・baseline commentには、人が読む要約だけでなく、精度解析を再実行できる構造化証拠をZIP圧縮・base64化して同梱する。対象は`validation-summary.json`、`autoadv-accuracy.result.json`、`autoadv-accuracy.jsonl`、game oracle JSONL、sanitized transcriptであり、各fileのSHA-256をmanifestへ記録する。検証用音声・fixture・runtime・再配布不可素材は含めない。これによりローカルrun directoryへアクセスできないチャット環境でも、Issue commentだけからケース単位のtrace・alignment・oracleを復元して解析できることを共有契約とする。

完全な証拠はローカルrun directoryにも残す。Issue更新は計測後の共有処理であり、GitHub側の送信失敗だけでローカルに成立した精度計測を失敗へ変更しない。

## Alignment品質・stress計測

既知本文のassociation率、固定clean-source annotationに対するlexical境界誤差、alignment未確認率、推論費用を測り、自動進行精度とは分離して扱う。full fileを一度だけalignせず、製品と同じ累積snapshot、単一workerの推論backpressure、PCM idle時の未解析tail drainを再生し、既知本文へどの範囲を対応付けたかを評価する。forced alignmentの`committed target end`はlexical/phonetic evidenceの品質値であり、human vocal event終了や`UtteranceProgress`成功とは扱わない。tail drainはheartbeatを音声終了証拠へ使わず、既に捕捉済みのPCMをforced alignmentへ渡すscheduler動作だけを再現する。`ALIGNMENT_ENDPOINT_ORACLE.json`は製品aligner出力を使わずclean source波形から固定した独立annotationであり、音声file端とは区別する。必要時にmanaged alignment runtimeを使ってWindows実機で明示実行する。

```powershell
python -m validation.real_game.benchmark_alignment --conditions clean quiet noise clipped truncated bgm --rounds 2 --output .\alignment-performance.json
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
