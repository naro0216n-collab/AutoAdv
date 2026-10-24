# AutoAdv ゼロベース再構築・移行計画

本書は [`integration-spec.md`](integration-spec.md) の製品を現行コードから構築するための作業計画である。仕様の正本ではない。現行実装の棚卸しを行った基点は2026年10月20日の`develop`（`436fd188150e90cc4ed3afc97a81bdb595abda83`）。実装時は最新`develop`と正準仕様を確認する。

## 旧実装の隔離と新しい作業正本

2026年10月21日の再構築着手に伴い、旧実装を通常開発から隔離した。

- GitHub `temp` は旧実装の最後の公開済み `develop` commit `a9eb55612f072b4cfd8dd2461285faa477d1950c` を固定保存する。
- ローカル `temp` は未公開の旧経路削除commit `48018e5` までを保存する。未公開変更を新実装の要件と解釈しない。
- `develop` に旧実装を一括復元する運用は行わない。LunaHook Native/Host/ABI、Irodori、Windows入力、上流取込資産などは、新しい契約に適合し検証できる単位で採用する。
- `temp` は隔離した参照先であり、通常開発やPublish Gatewayの中継経路には使用しない。`main`とversionは承認まで不変とする。
- 現在の `develop` は、一つのゲーム取得・Utterance・提示完了・送り権限・設定保存の経路を新規実装する。旧Controller・Runtime・旧テストは新規テスト契約で置き換える。

既存コードと旧テストの全件監査を移行条件にしない一方、採用するNative/IPC等の実動作は省略してよいわけではない。新しい実装が未対応の機能は、旧コードをGUIで隠して温存せず、未成立として明示する。

## 1. 実施方針

**完成時の実行モデルを先に確定し、使える既存資産を採用する。** 既存コードの構造・状態・テストを網羅して正当化する作業は行わない。反対に、既に動くNative、音声、Utterance、Irodoriの処理を一律に作り直すこともしない。

作業の成果は、利用者がGUIからゲームを選び、HOOK原文の取得・話者別TTS設定・音声同期・会話送り・手動停止を一連の操作として利用できることにある。設計の綺麗さ、旧ファイルの撤去量、テスト件数を独立した達成目標にしない。

## 2. 最初に確定する実行モデル

現行コードに合わせて状態名を決めず、次の処理とデータの所有関係を実例で確定する。

| 処理 | 入出力と所有する情報 |
|---|---|
| 作品と実行セッション | ゲーム・プロファイル・接続対象、選択HOOK、音声と入力の対象、現在世代 |
| 原文取得 | HOOK探索・選択・復元、候補の本文・取得元・受信順序・更新情報。任意OCRは同じ後続処理に接続 |
| Utterance | 原文から話者・本文・読み・照合文・Voice・音声方針を一意に導出 |
| 音声・提示 | continuous Process Loopback、既知本文・human-vocal・recurrenceの観測、`UtteranceProgress`、Irodoriの実再生・取消、閲覧時間、提示完了 |
| 会話送り | 明示許可、現行台詞の提示完了、入力対象、送信履歴、次候補の受付、停止・再開 |
| 設定とGUI | 共通・作品・プロファイルの一元保存、実状態と一致した表示・操作 |

具体的な判断を着手時に決める対象は、次の四点に絞る。

1. **台詞の区切り**：HOOKの逐次更新、複数HOOK、同文の別台詞を、取得イベントと作品別規則からどう処理するか。
2. **音声の選択**：話者別TTS使用と進行2モードの独立、音響観測と`UtteranceProgress`の責務、不確実な原音と明示本文時間フォールバック。
3. **送りの条件**：閲覧・実再生完了と明示許可を入力へつなぎ、送信後に同じ台詞への不要な入力を防ぎながら次台詞を扱う方法。
4. **日常操作**：ゲーム・HOOK・Voice設定の保存と復元、途中停止・再開、作品切替を一つのGUIで完結する操作順。

必要な情報が取得できない状況は、実例に即して設定・保留・手動操作を選ぶ。取得方法や判断機能は、再現した問題に対して追加する。

## 3. 既存資産の採用調査

以下は調査の優先順位であって、ファイル単位の維持・撤去命令ではない。採用する処理が新しい実行契約に合っていれば、コード変更を最小限にできる。

| 対象 | 再利用の判断 |
|---|---|
| Luna Native、`scripts/sync_luna_upstream.py`、ビルド資産 | 上流の採用範囲、64bit Windows上のx86/x64ゲームプロセス対応、HOOK候補の実接続、ライセンス、更新経路を確認して活用 |
| `luna_abi.py`、`hook_host.py`、`hook_host_worker.py`、`hook_protocol.py` | ABIとコールバック、IPC、Host生存・再接続、取得元情報の受渡しを確認して採用 |
| `hook_application.py`、HOOK設定 | 探索・手動追加・復元・取得補正・必要な複数出力の設定が実際に機能するか確認 |
| `text.py` | 話者分離、ルビ・辞書、表示・照合・読み上げ文、音声方針への接続を確認 |
| `irodori.py`、`irodori_server.py` | サーバー自動起動・Voice取得・再生完了・キャンセルを確認して採用 |
| `process_audio.py`、`audio_buffer.py`、`forced_alignment.py`、`native_audio.py` | Process Loopback、連続音響履歴、既知本文alignment、human-vocal/recurrence観測を完成モデルへ接続できる部分だけ活用 |
| `acquisition.py`、`ocr.py` | 取得元の一貫性、標準HOOKと任意OCRの使い分けを確認して採用 |
| `profiles.py`、`preferences.py` | 作品・Voice・HOOK情報の保存と復元が完成モデルに合う部分を利用 |
| `gui.py` | 個々の有用なWidget処理は参照してよいが、旧タブ構成、状態要約、手動接続・検出・保存ボタン、WidgetからProfileを再構成する経路は再利用しない。Play / Game Workspace / SettingsをApplication契約から再構築する |
| `controller.py`、`application.py`、`runtime.py`、`live.py`、`live_guard.py`、`services.py` | 完成時の処理順・状態所有から再設計し、入力・イベント配送等の有用なコードを取り込む |
| `evaluation.py`、補助スクリプト | 実接続・障害調査・テストのために有効な処理を利用 |

継続採用したモジュールにも、Application・Persistence・GUIとの接続不良があれば是正する。新しい経路と同じ責務を果たす旧経路を通常実行で並走させない。

## UI再構築のApplication境界

GUIはHookHost、NativeAudio、Irodori、保存、Controller内部状態を組み合わせて製品状態を作らない。Applicationが利用者の操作を受け、必要な内部処理を自動実行し、GUIへはPlay・会話取得Workspace・作品設定・全体設定の意味へ解決済みのViewを返す。正常状態を多数の説明ラベルへ変換しない。

会話取得はWorkspaceを開いている間に通常候補を継続収集する。HOOK自身へ台詞/話者の排他的roleを持たせず、各取得元の原文から話者候補・本文候補を独立抽出し、最終会話の本文・話者それぞれがどの取得元を採るかをdraftとして構成する。抽出規則は取得元ごとに持ち、複数の実サンプルへ即時適用して、取得元の整形Previewと最終会話Previewを同じWorkspaceで確認する。同一HOOKから両フィールドを取る場合と複数HOOKから別々に取る場合を同一契約で扱い、別flush集約間の値を推測で持ち越さない。Voice・TTS話者設定は会話取得から分離する。追加HOOK探索は通常候補が不足した場合の障害回復であり、通常取得の開始ボタンにしない。

独立したVoice・読み・進行設定等は変更単位で保存する。画面全体のdirty flagと「作品設定を保存」を通常経路にしない。取得構成のように途中状態が意味を持つ相互依存編集だけtransactionを持つ。

## 4. 現状から優先して解消する利用上の問題

現行コードの既知の論点は、再構築後の実動作で解決する。これらの古い内部構造を全件追跡することは着手条件ではない。

- **自動送りの開始**：すでにHOOK本文を受信している状態から、ユーザーがGUIで自動送りを許可できること。
- **原音とTTSの選択**：音声推論が確定できない場合やBGMがある場合にも、話者別TTS使用設定とフォールバックON/OFFに沿って入力を保留または継続できること。保留は自動送りON/OFFを書き換えない。
- **HOOKの活用**：保存候補の復元、手動HOOK、必要な複数HOOK、話者と本文の対応が、GUIで設定した通り実行されること。
- **入力と連続会話**：AutoAdv未送信で次HOOKが成立する外部進行を原因推測なしに扱い、同文反復や部分更新のある作品でも取得元のイベントと提示状況から進行すること。
- **作品の再利用**：実際のHOOK・音声・入力対象の対応を保存済み作品情報から確立し、毎回のPIDやVoice再設定を減らすこと。
- **UIの一致**：許可の可否、取得状況、適用設定、TTSと原音の経路がGUIの表示と一致すること。

## 5. 実装・検証checkpoint

### A. 一つの台詞処理契約

GameSession、候補更新、Utterance、音声提示、送りの順序と所有者を決める。実際に発生するHOOKイベント列と再生イベント列を用いて、同文反復・部分更新・音声混在をどう扱うか明確にする。利用者の許可と停止・再開の操作を契約へ含める。

### B. HOOKからGUIまで

実HOOKの探索・選択・保存・再接続から、話者と本文のプレビュー・Utterance・Voice割当てへ接続する。複数ゲームとプロファイルの保存・復元も確認し、Luna側の重複設定なしで使えるようにする。

### C. 原音・TTS・閲覧の同期

Utteranceの音声方針を使って、既存音声の区間と再生終了、Irodori生成・実再生・取消、閲覧時間を一つの提示処理につなげる。有声、無声、話者混在、BGM、遅発原音、TTS専用を連続した台詞処理で確認する。

### D. 会話自動送り

自動送りをGUIから許可し、提示終了・入力対象・直前状態を確認して適切なタイミングで入力する。入力後の新HOOK候補、同文の別台詞、停止・再開・切替を結合し、重複入力と台詞飛ばしを抑える。

### E. 通常運用と実ゲーム

Irodoriフォルダ自動起動、Voice試聴、作品の検出・復元、HOOK取得、音声・送り、手動操作をWindows GUIで連続して確認する。64bit Windows上でx86/x64ゲームプロセス向けNativeと複数種類の実ゲームを用いて、対応が成立した条件と残る問題を記録する。

checkpointは自然な責務のまとまりを示す。各段階の中でも、接続と関連検証が成立した部分はcommit・publishしてよい。成果は一連の実利用経路の成立で判断する。

## 6. Luna設定の取込み

Lunaの作品・HOOK資産は読取り専用で取り込み、元ファイルへ書き戻さない。旧AutoAdv設定は取り込まず、現行設定形式だけを製品設定として保存・復元する。

## 7. テスト再編

検証するのは、仕様が要求する利用結果と、それを成立させる技術契約である。

- HOOK探索・取得元と世代・保存復元からUtteranceとGUI表示まで。
- 話者・読み・音声方針とVoice選択、TTS利用有無と進行2モード・ルビ計数・明示フォールバック、遅発原音のリスク。
- 許可後の通常会話、同文反復・部分更新、入力重複防止、停止・再開、セッション失効。
- 複数作品とプロファイルのGUI操作、Luna設定の読取り専用取込み。
- WindowsのNative Host・実音声・実入力を含めた統合動作。

既存テストは個々の検出内容と完成時仕様との関係から判断する。旧アサーションを通すためだけに実装を変えず、実処理を模擬したテストだけで完成を判定しない。

## 8. 作業・publish

作業開始時に`docs/README.md`と`docs/development-principles.md`を読み、正準仕様を確認する。ローカルrepoは最新`develop`のFast CI `source-snapshot`から復元し、`scripts/publish_request.py init`でpublish基点とrepo-local identityを設定する。有効な最新ローカルrepoがある場合はそのまま継続利用する。

ローカル実装・関連テスト・commit後、`scripts/publish_request.py prepare`で生成した手順を順序通り実行してPublish Gatewayを使う。成功後は`record`し、次のpublish前に前回Fast CIの結果を確認する。`main`とversionは利用者承認まで変更しない。細部の手順は`docs/publish.md`に従う。

各checkpointの報告は、利用可能になった操作・再利用した資産・検証結果・未成立事項を明確にする。

## 音声同期2モードへの接続順

1. 共通・作品・話者別TTS使用と進行方式を別データとして確定する。現行設定形式だけを製品設定として保存・復元し、旧AutoAdv形式の互換読込み・移行経路は持たない。
2. Utteranceの発音基準文からHaqumei primary pronunciationを一度だけ取得し、モーラ相当の読み単位数と最低本文時間・1読み単位時間から`text_wait_ms`を算定してセッション中は凍結する。本文時間は閲覧・明示フォールバックの方針であり、原音durationへ変換しない。
3. 音声判断は、Presentation Epochに属する連続音響履歴へ既知本文を後付けし、lexical progress、human-vocal activity、recurrence/persistence等を一つの`UtteranceProgress`へ統合する。個々のobserverをcompletion判定器にしない。
4. ControllerへTTS実再生、本文時間、`UtteranceProgress`終了、明示フォールバックを互いに異なる完了理由として接続する。ゲーム入力権限はController一箇所に保つ。
5. GUIの進行選択を2モードだけとし、フォールバック・計算時間・完了理由・送信履歴と保存設定を一致させる。
6. 実録音ベンチマーク、NativeAudio時系列、Windows実ゲームを別々に検収し、モデル単体指標や旧fixtureを製品精度の代理にしない。

音声判断の非TTS台詞で確証が得られない場合、同じ本文発音量期限を使ってONなら理由を区別したフォールバック、OFFなら自動送信せず入力保留とする。肯定的な音響証拠の消失だけで物理的な原音終了を認定しない。Windows実ゲーム未実行の場合は入力保留と誤送りの評価を未検収のまま残す。

実録音のobserver単体指標と本体`NativeAudio`へ時系列PCMを投入した`UtteranceProgress`結果は異なる証拠として記録する。実モデルによる後者の再現はWindows実機で行い、局所テストだけで実ゲーム未実施を解消したとは扱わない。

## 音声観測・進行の再接続

- `NativeAudio`はセッション中のProcess LoopbackをHOOK間も連続観測する。AutoAdv自身の送りを次Presentation Epochの強い因果境界とし、HOOK到着時刻を実発声開始へ読み替えない。外部進行は開始境界不明として保持し、HOOK時刻等から開始を捏造しない。
- raw PCMの保持上限は資源制約であり意味的なUtterance開始境界ではない。長時間に必要なhuman-vocal activity、recurrence、境界情報等はcompact featureとして保持し、遅延HOOKで本文が判明した後もPresentation Epochの過去音響を参照できるようにする。
- target-conditioned forced alignmentは既知本文に対するlexical / phonetic progressと対象対応強度を返すobserverである。target association、target消化、phonetic alignment終了とhuman vocal event終了を同じ事実へ潰さない。自由ASRを同じcompletion責務の第二経路として並走させない。
- human-vocal observerは、通常発話・囁き・非言語発声を含む「人間発声らしい活動」を時系列観測する。VAD inactiveをendpoint gateにせず、BGM・SE・背景発話を含むprocess mix全体の無音も終了条件にしない。
- recurrenceはhuman-vocal relevanceを伴う音響表現のself-similarityとepochを跨ぐpersistenceとして観測する。raw mixの周期性だけでloopを決めず、有限の反復発声、persistent/looping vocal source、BGM loopを同一状態へ潰さない。過去にpersistentだったという事実だけで、後から始まった新しい有限vocal tailを終了扱いしない。
- 泣き・叫び・喘ぎ・笑い・息遣い等は本文alignmentの成否に従属させない。speaker identificationやカテゴリ文字列regexを成立条件にせず、有限human vocal eventが継続中かという共通表現へ統合する。
- `UtteranceProgress`は「確定不能」「有限発声継続」「終了へ移行中」「persistent/looping」「終了済み」を区別するstatefulなDomain状態とする。一つの観測の欠如から別状態を確定せず、session/token変更後の古い音響stateで次Utteranceを完成させない。
- Controllerは`UtteranceProgress`の終了だけをaudio-derived completionとして受け取る。推論不能時の本文時間フォールバックは別理由の利用者方針であり、audio-derived successへ変換しない。
- 恒久テストはHOOK到着時刻に依存しない意味、observerの入力非所有、alignment終了とUtterance終了の非同一性、finite/persistentの区別、本文量と原音durationの非相関、fallback理由の分離、境界確度と世代失効をproperty・状態モデル・Application契約として検証する。Windows実機では遅延HOOK、非言語tail、本文-duration非相関、vocal loop、BGM/SE/背景発話、原音なしを一続きの精度スイートで測る。
