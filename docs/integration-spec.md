# LunaTranslatorベース 音声同期オートモード・選択的TTS補完 統合実装指示書

## 1. 目的

LunaTranslatorを原文取得基盤とし、市販ノベルゲームの通常台詞を、ゲーム本来のボイス・外部TTS・無音閲覧のいずれかで提示した後、自動進行させる。選択肢、マップ、メニュー、認識不能状態、ユーザーによる介入では自動進行を停止する。ゲーム本来のボイスが遅れて始まる場合にはTTSを止め、音声終了を待って進行する。

**完成要件は、単独のTTS読上げ・音声認識・入力送出ではなく、これらが混在する通常ゲーム進行が一つの制御系で成立すること。** ゲーム固有の実装を最小限にし、音声モデルとTTSバックエンドを交換可能な構造とする。

## 2. 適用範囲・基本制約

- Windowsのノベルゲームを対象とし、LunaTranslatorのHOOK取得を第一候補、OCRを代替とする。ゲーム固有のHOOK成功やエンジン仕様は未確認のまま前提化しない。
- 外部TTSは既存のIrodori-TTS-Serverへの接続を第一候補とし、別エンジンへ置換可能なインターフェースとする。
- 開発時の代表検証対象は『つよきす -Full Edition-』とするが、その固有挙動を汎用コアの固定条件にしない。
- ゲーム標準AutoおよびLunaTranslatorの`ttsautoforward`とControllerを同時に進行主体として動かさない。通常は両者をOFFとし、**進行入力の発行権限をControllerへ一元化**する。作品別に既存Autoを利用する場合でも単一進行主体を維持し、二つの送り経路は併用しない。
- ゲームのオーディオを外部TTSと分離して取得する。データ取得や入力に必要以上のゲーム本体改変を行わず、保護機構の回避を前提にしない。
- LLM/VLMは低頻度の分類補助には利用できるが、発話終了の確定、ブロッカー解除、進行キー送信の権限を持たせない。
- 既存機能の二重実装、暫定的な擬似ゲーム本体、常設する予備制御経路を作らない。差分実装は既存のLuna API・TTS・入力処理を調査したうえで必要最小限とする。

## 3. 判定モデル

**音声の存在、TTS割当て、台詞の表示性質、進行可能状態は独立した属性**とする。`VADで無音`を`ボイスなし`と同一視しない。`TTSをスキップ`を`ゲームの台詞をスキップ`と同一視しない。

### 3.1 状態の定義

| 属性 | 値 | 用途 |
|---|---|---|
| `content_kind` | `SPEAKABLE` / `SYMBOLIC` / `EMPTY_EVENT` / `NON_DIALOGUE` | 本文・記号のみ・実在する空台詞イベント・非台詞画面を区別 |
| `native_voice` | `PRESENT` / `ABSENT` / `UNRESOLVED` | **当該台詞に帰属するボイスの有無**。音声アセット情報・音響照合等により判断し、現時点で再生中か否かと混同しない |
| `native_playback` | `NOT_STARTED` / `ACTIVE` / `COMPLETE` / `UNKNOWN` | **当該台詞のゲームボイス再生進捗**。開始前・再生中・終了確認・認識不能を区別 |
| `native_evidence` | `METADATA` / `AUDIO_CONFIRMED` / `HEURISTIC` / `NONE` | 判定根拠を保持し、推定を確定情報として扱わない |
| `tts_policy` | `SELECTED` / `DEFAULT` / `SKIP` / `DISABLED` / `UNASSIGNED` / `ERROR` | 外部TTSを実行できるか。`DEFAULT`は具体的な利用可能音声がある場合のみ再生へ進む |
| `screen_state` | `DIALOGUE` / `CHOICE` / `MAP` / `TRANSITION` / `MENU` / `UNKNOWN` | 操作・演出停止条件 |
| `presentation_mode` | `NATIVE` / `TTS` / `SILENT` / `UNRESOLVED` | 実際に選択した提示経路 |
| `controller_state` | `WAIT_LINE` / `WAIT_TEXT_READY` / `RESOLVE_PRESENTATION` / `WAIT_NATIVE_END` / `WAIT_TTS_END` / `WAIT_SILENT_DWELL` / `READY_TO_ADVANCE` / `WAIT_TRANSITION_ACK` / `PAUSED` | 実行状態 |

`ABSENT`の根拠は、現在台詞への音声ファイル割当てがないという決定的証拠と、観測時間内にボイスらしい音声が出なかったという推定を区別する。一般的な話者ポリシーは補助情報であり、当該台詞の実際の音声割当てとは異なる。`native_voice=PRESENT`であっても`native_playback=COMPLETE`とは限らず、`native_playback=ACTIVE`でも前行の残響・持続音声が現在行のものとは限らない。音声の**当該台詞への帰属**と、発話の**進捗**を別々に確認する。

### 3.2 進行判定の不変条件

1. Controller以外は自動進行入力を発行しない。1台詞イベントにつき自動進行入力は高々1回とする。
2. すべての非同期処理（ASR、VAD判定、TTS生成・再生、タイマー、UI判定、入力完了通知）に`session_id`、`line_epoch`、`cancel_token`、単調増加時計の時刻を紐付ける。
3. 進行入力前に台詞世代と対象ウィンドウ、表示完了、画面状態、音声/TTS待機完了、操作可能状態を再検査する。
4. 旧世代イベント、キャンセル済みTTSの完了通知、旧タイマーの満了を現在の進行に適用しない。
5. 画面状態が`CHOICE`/`MAP`/`MENU`/`TRANSITION`/`UNKNOWN`の場合は自動入力しない。
6. 実際のゲームボイスが当該台詞に存在すると確定している場合、無音タイマーだけで進行させない。
7. 音声取得失敗を「音声なし」の根拠にしない。取得系の健全性を監視する。
8. 自動進行入力後は次の台詞・画面への遷移を確認するまで再入力しない。遷移未確認時は停止・診断し、盲目的な再送を行わない。

## 4. システム境界・モジュール

```text
ゲーム ─(HOOK/OCR)─> LunaTranslator ─(原文・行イベント)─> LineEventAdapter
  │                                                         │
  ├─(プロセス音声)─> AudioCapture ─> VAD/ASR/TargetMatcher ────┤
  ├─(画面観測)────> ScreenStateAdapter ───────────────────────┤
  └─(任意：ボイスメタデータ)─> VoiceMetadataAdapter ───────────┤
                                                            ↓
                         PresentationResolver → Controller(StateMachine)
                                  │                           │
                             IrodoriTTS＋Player               ↓
                                  │                     InputDriver
                                  └──再生完了・中断────────────┘
```

共通モジュールを`LineEventAdapter`、`LineNormalizer`、`AudioCapture`、`NativeVoiceDetector`、`StreamingASR`、`TargetMatcher`、`PresentationResolver`、`SilentDwellPolicy`、`TTSClient/Player`、`Controller`、`ScreenGuard`、`InputDriver`、`StructuredLogger`とする。

ゲーム別差分は原則として、HOOK/OCRと画面上の領域設定、話者ポリシー、送り入力、選択/マップ画面識別、任意のシナリオ音声メタデータAdapterに限定する。ゲーム固有の条件を共通Controllerへ直接埋め込まない。

## 5. LunaTranslator接続契約

### 5.1 最小経路と拡張判断

- 既存の原文WebSocket `/api/ws/text/origin` を第一候補として検証する。公式に保証されるのは抽出した原文の連続配信であり、安定した台詞ID・話者・フィルタ前イベント・画面完了フラグの提供は保証されない。
- Lunaの実コードにある空テキスト除外・重複排除・後処理によって、記号のみや空台詞イベント、同一台詞の連続が消え得る。通常の翻訳・読上げ用経路とは**別に、Controllerへ原文の行候補イベントを送る境界**を確保する。
- 必要なメタデータが公開APIで不足する場合は、LunaのHOOK/OCR取り込み箇所とフィルタ前の適切な境界に、外部イベント購読機構を最小追加する。原文WebSocketへ意味の異なる属性を非互換に混入させず、バージョン付きイベント契約として拡張する。
- 原文イベントを受け取ることだけで「表示完了」と推定しない。文字送りや段階表示があるゲームでは画面安定・エンジン由来フラグ等で確定する。
- スクリーンから観測した同一文字列であっても、新しい台詞イベントとして区別できる設計とする。テキストhashだけを恒久的IDにしない。

### 5.2 `LineEvent`契約

```json
{
  "schema_version": 1,
  "session_id": "uuid",
  "line_epoch": 42,
  "event_kind": "line_candidate",
  "source": "HOOK",
  "source_seq": 180,
  "observed_at_mono_ms": 123456,
  "raw_text": "……",
  "speaker": null,
  "text_ready": false,
  "screen_state_hint": "DIALOGUE",
  "native_voice_hint": null
}
```

このJSONは**Controller側で正規化された`LineEvent`の契約例**であり、Luna標準WebSocketが同形式を直接送る前提ではない。`source_seq`は取得イベントの順番を表し、`line_epoch`はControllerが一元的に所有する台詞世代を表す。Luna側Adapterから得た観測値、入力後の遷移ACK、可能ならゲーム由来の行IDを照合して世代を決める。テキスト受信1件ごとに世代を増やしてはならない。

`line_candidate`、`line_updated`、`line_ready`、`line_invalidated`、`screen_changed`等のイベント種別を定義し、同じ行が表示途中に何度更新されても`line_epoch`を維持する。新しい台詞への**観測可能な**移行でのみ世代を更新する。必要なら`source_line_id`やシナリオ上のIDを任意フィールドで追加できるようにする。同一文字列の連続行や画面遷移について、利用可能な情報では世代・表示完了を識別できない場合は、推測で補完せず、そのゲームのAdapter要件として明示する。

HOOKの出力だけで空台詞とシーン空白を区別できない場合には、`EMPTY_EVENT`を捏造せず、画面側またはゲーム固有Adapterが台詞イベントの存在を裏付けたときのみ採用する。

## 6. 音声取得・存在判定・終了判定

### 6.1 ゲーム音声の取得

- WindowsのApplication/Process Loopbackを第一候補とし、ゲーム対象プロセスツリーのPCMを取得する。対応OS・ゲーム側オーディオ出力との適合を検証する。
- ゲーム音声はTTSワーカープロセスから分離する。対象プロセスツリーにTTSが含まれる配置は禁止。分離できない環境では取得経路を選択可能にするが、自己TTSの混入を検出・抑止する。
- ステレオ原音と時刻を保持し、ASR用にはモデルに応じてリサンプリングする。**テキスト表示完了前から音声を継続監視**し、リングバッファで新規台詞検出直前・文字送り中の音声を参照できるようにする。`WAIT_TEXT_READY`は表示と送りの制御状態であり、音声取得・発話開始の監視を止める状態ではない。
- 捕捉の断絶、無音デバイス状態、デバイス切替、ゲームPID変更を検出し、音声が取得できないことを「ゲームボイスなし」に変換しない。

### 6.2 ボイス存在判定

当該台詞に対応するvoice-ID/音声アセットが取得可能ならそれを強い証拠とする。ただしファイル割当てと実再生は区別する。対応表がない場合は話者ポリシー、VAD、ステレオ中央成分、台詞との音響照合を組み合わせる。BGM・SEでVADが反応しても、当該台詞の音声開始を自動確定しない。**前行から持ち越された音声やボイスチャンネル状態を、新行の発話開始・終了証拠として再利用しない**。音声アセットID、開始時刻、境界情報または現在台詞との照合で帰属を判定し、帰属不能なら保留する。

`native_voice=UNRESOLVED`で音声開始猶予`voice_onset_grace`を設ける。無音推定に移行できるのは、音声取得が正常、表示が完了、通常台詞画面、猶予満了、音声開始の積極的証拠がない、話者・シーンによる「ボイスあり」根拠が強くない場合のみ。根拠不足なら`PAUSED`へ遷移する。単一の固定待機時間で全台詞を「ボイスなし」と断定しない。**猶予満了後に初めて始まるゲームボイスを、既に送出した進行入力から遡って保護することは不可能**である。音響推定での無音進行にはこの限界があるため、作品別の実測開始遅延分布・voice metadata・誤送りログから猶予と許可基準を校正し、基準を満たせない作品では音声有無を判定不能として停止する。

### 6.3 発話終了判定

- 既知台詞を日本語正規化し、Streaming ASRの部分認識との単調な進捗照合（coverage、末尾一致、反復・逆戻り、信頼度）を行う。
- VADの発話末尾候補、末尾無音、利用可能ならCTC alignmentや音声ファイルdurationを併用する。
- VAD無音のみ、ASR全文一致のみを単独の終了条件にしない。短台詞・息・笑い・長い途中間・繰返し・人名等の扱いを校正する。
- 初期比較候補はSilero VAD、Vosk small-ja、sherpa-onnx日本語系、補助としてReazonSpeech Zipformerとし、実環境のFalse Advance Rate・判定遅延・負荷で選択する。ベンチマーク未実施の性能値は合格保証値にしない。
- ボイスありだが完了認識不能、取得不能、音声照合が明確に食い違う状態ではタイマーで進行せず`PAUSED`とする。

## 7. 外部TTSと遅延ゲームボイス

- `tts_policy=SELECTED`では指定voiceを使用し、`DEFAULT`では実際に有効なデフォルトvoiceを解決する。未解決のDEFAULTをTTS再生可能と扱わない。
- `SKIP`、`DISABLED`、`UNASSIGNED`ではゲームボイスがない限り`SILENT`を選択する。TTS未割当てをエラー待ち状態にしない。
- TTS生成完了と、オーディオデバイスにおける再生キューの実際の完了を区別する。正常終了・中断・エラーの結果を別イベントで返す。
- TTS生成エラーはログと通知を残し、ゲーム音声不在が確認できる場合に限り標準では無音閲覧へ退避する。アクセシビリティ要件としてTTS必須モードが有効なら停止する。
- TTS再生中もゲーム音声の監視を続ける。**当該台詞に対応する**遅延ゲームボイスを検出したら、再生・キュー・生成中ジョブをキャンセル可能な範囲で中断し、`WAIT_NATIVE_END`に移る。中断完了を「TTS正常完了」とみなして進行しない。
- ゲーム音声以外のSEやBGMをTTS停止の直接条件にしない。疑わしい検出では必要に応じて一時ミュート/保留と追加確認を挟み、単発誤検出で不可逆に状態を切り替えない。
- 外部TTSの音声をASRで再認識して再生完了を推定しない。
- キャッシュキーには正規化本文、voice、参照音声のバージョン、モデル、速度・音声スタイル等の出力に影響する設定を含める。分岐先不明の台詞を確定的に先読みしない。

## 8. 無音・記号台詞・演出区間の自動制御

### 8.1 無音処理を正式な提示経路とする

`SILENT`は異常時の逃げ道ではない。ゲームボイスが存在しない、TTSが未割当て、明示SKIP、記号のみ等に対し、表示内容を閲覧する時間を確保して進行する正式経路`WAIT_SILENT_DWELL`とする。

- `SPEAKABLE`かつTTSなし：表示の文字量と設定した閲覧速度に応じて待機。
- `SYMBOLIC`：原文の記号の種類・長さに応じた非発声の間を確保。読上げ文字列が空でも`LineEvent`は破棄しない。
- `EMPTY_EVENT`：ゲーム側で送り待ちの独立した空台詞イベントが確認できる場合のみ短い閲覧待機を適用。
- `NON_DIALOGUE`：無音台詞として処理しない。演出終了、画面遷移、ユーザー入力等の別条件を待つ。

「っ」「あっ」「ん」などの発声可能な短い台詞は記号のみと分類しない。「……」に実際の溜息・呻きが割り当てられた場合はnative音声待ちを優先する。ルビや括弧を消す前の原文、TTS用文、照合用文の3表現を保持する。

### 8.2 閲覧時間

音声なし文章の開始時刻は、本文が表示完了し閲覧できる状態となった単調時計時刻`text_ready_at`とする。初期ポリシー：

```text
T_dwell = max(T_min, T_base + N_effective / R_read + T_punct)
T_base = 0.7秒、T_min = 1.0秒、R_read = 12文字/秒
```

記号のみは別テーブルで「……」約1.5秒、「…………」約2～3秒、「！」等約1秒を初期値とする。これらは実測値ではなくユーザー調整・実機校正の初期値である。句読点、改行、ルビの扱いは正規化テストに固定する。長文の閲覧時間を画一的な短い上限で切らない。

`voice_onset_grace`と閲覧時間は直列加算せず並行して計測する。

```text
ready_at = max(text_ready_at + dwell_time,
               voice_grace_end_at,
               scene_release_at)
```

ただし`scene_release_at`はゲームから観測可能な解放時刻がある場合のみ用いる。全条件が満たされるまでに現在台詞のnative音声開始を検出したら無音タイマーによる進行を取り消す。`ready_at`到達後も進行入力直前の状態を再確認する。

### 8.3 空白と演出の区別

テキストが消失・空白化しただけではクリックしない。文字送り途中、画面遷移、背景切替、ムービー、CG演出、ロード、テキスト取得失敗を区別する。`EMPTY_EVENT`は台詞ウィンドウが送り待ち状態にある等、正の証拠がある場合にのみ生成する。台詞イベントがない状態は`WAIT_LINE`または`PAUSED`であり、タイムアウトで無条件送出しない。

## 9. Controllerのイベント処理

### 9.1 状態遷移

```text
WAIT_LINE
  → WAIT_TEXT_READY
  → RESOLVE_PRESENTATION
      ├─ NATIVE → WAIT_NATIVE_END ───────────┐
      ├─ TTS    → WAIT_TTS_END ──────────────┤
      │               └─ native onset → TTS取消 → WAIT_NATIVE_END
      ├─ SILENT → WAIT_SILENT_DWELL ─────────┤
      │               └─ native onset → 待機取消 → WAIT_NATIVE_END
      └─ UNRESOLVED → 追加判定／PAUSED         │
                                             ↓
                                   READY_TO_ADVANCE
                                             ↓
                                    進行入力1回のみ
                                             ↓
                                   WAIT_TRANSITION_ACK
                                       ├─ 次イベント確認 → WAIT_LINE
                                       └─ 未確認・異常 → PAUSED
```

### 9.2 進行許可条件

`autoplay_enabled && focused_target && screen_state==DIALOGUE && text_ready && current_epoch==decision_epoch && presentation_complete && !native_voice_active && !tts_playing && !transition_pending && !manual_override && !advance_already_sent && input_available`を全て満たすこと。入力前に最新の観測情報で再判定する。

`presentation_complete`は`NATIVE`なら発話末尾の証拠、`TTS`なら正常再生完了、`SILENT`なら閲覧時間・音声開始猶予の双方の完了を示す。`UNRESOLVED`を真に変換しない。ボイスが存在することを示す新証拠は既存の無音完了候補を無効化する。

### 9.3 手動操作・遷移失敗

ユーザー入力、ロード、スキップ、ゲームウィンドウ変更で現セッション/台詞世代を適切に失効させ、進行待機・TTS・音声照合をキャンセルする。手動介入時に意図せぬ自動送りをしない。次行が同じ原文である可能性があるため、テキスト差分だけをACKに使わない。ACK不明時に追加のEnterを送らない。

## 10. ゲーム別設定と運用

設定は共通パラメータと作品別profileに分ける。ゲーム内の本文フィルタや声の未割当てを、画面判定の設定で無理に補正しない。

```yaml
controller:
  autoplay: true
  input_authority: controller_only
  on_unknown: pause
  on_tts_error: silent_if_native_absent
  on_manual_input: cancel_current_and_pause
  on_transition_timeout: pause

audio:
  capture: process_loopback
  tts_separate_process: true
  voice_onset_grace_ms: 500       # 暫定。ゲーム実測で調整
  ordinary_tail_silence_ms: 300   # 暫定
  short_tail_silence_ms: 500      # 暫定

silent_dwell:
  base_ms: 700
  min_ms: 1000
  reading_chars_per_sec: 12
  symbolic:
    ellipsis_short_ms: 1500
    ellipsis_long_ms: 2500
    exclamation_ms: 1000
  text_and_grace_parallel: true

tts:
  backend: irodori
  on_late_native_voice: interrupt_and_wait_native
  require_playback_end_event: true
  required_for_accessibility: false

game_profile:
  game_id: example
  text_source: luna_hook_first
  native_voice_metadata_adapter: optional
  speaker_policy: {}
  screen_regions: {}
  advance_input: ENTER
  choice_and_map_require_user: true
```

値は初期校正値であり、固定的な作品横断の正解値とみなさない。無音の速度設定はユーザーが調整可能とし、通常設定で利用できるようにする。収集した音声・画面サンプルはユーザーの明示的な診断設定があるときのみ保存し、原則ローカルで処理する。

## 11. 障害ポリシー

| 条件 | 処理 |
|---|---|
| TTS未割当て、明示Skip、TTS無効 | native不在が確認できれば無音閲覧へ |
| 記号のみの本文 | native有無を先に評価し、なければ記号用無音閲覧へ |
| TTS生成・再生エラー | native不在なら通知・記録して無音閲覧。TTS必須設定なら停止 |
| ゲームボイスが遅れて開始 | 無音送り候補を破棄、TTSは中断しゲーム音声終了待ち |
| 音声ありだが発話終了を認識不能 | 停止。固定時間だけで進行しない |
| 音声取得喪失 | 音声有無を推定せず停止・通知 |
| 記号イベントがLuna表示フィルタで除去 | Controllerのフィルタ前イベントで維持 |
| 行イベントなしの空白画面 | 待機。無条件クリックを行わない |
| 選択肢、マップ、メニュー、画面判定不能 | 停止 |
| ゲーム非アクティブ、手動入力、ロード | 当該世代の処理を失効させ安全停止 |
| 進行入力後に遷移確認不能 | 再送せず停止・ログ |

`PAUSED`は無期限ブロッキング処理ではなく、停止理由、直前の観測値、復旧方法を確認できる明示状態とする。ユーザーによる通常の手動ゲーム操作を妨げない。

## 12. 実装実施順序と範囲

以下は前後関係を示すもので、各段階の試作だけを独立した完成成果にしない。最終的な成果は全経路が統合された一つの基盤とする。

1. **接続契約の確立**：Lunaの原文取得・前処理・HOOK/OCRイベント・既存TTS/送り処理を調査し、`LineEvent`と世代管理、入力権限の一元化を実装する。フィルタで捨てられる記号・空台詞の扱いを確認する。
2. **画面・入力・無音進行**：通常台詞、表示完了、選択肢、マップ、遷移を識別し、TTSなし・記号のみ・長文・同一台詞の進行条件を実装する。この段階の入力自動化は、**native不在が独立に保証されたテスト条件**で検証する。実ゲームでの無音推定による自動入力は、次段階の音声取得・開始猶予・帰属判定が接続されるまで有効化しない。
3. **音響・nativeボイス経路**：Process Loopback、音声健全性、VAD、ASR/台詞照合、終了判定、音声開始猶予を統合する。ゲーム側voice metadataがあれば同じ判定契約へ接続する。
4. **TTS統合と競合制御**：Irodoriへの発話要求、音声割当て、生成/再生結果、TTS中断、遅延nativeボイスへの譲渡、失敗時無音退避を実装する。
5. **連続進行・障害試験**：手動介入、二重入力、同一テキスト、ロード、部分表示、先行/後発音声、BGM/SE、選択停止、接続断、再起動を含む回帰試験を実施する。
6. **共通化評価**：作品別profile以外にゲーム固有条件が混入していないか、別作品またはゲーム振舞いを再生した検証fixtureで確認し、不要な補助経路・旧制御処理を整理する。

## 13. 検証データ・必須受入試験

### 13.1 テストケース

| ID | 入力・状態 | 期待結果 |
|---|---|---|
| A01 | TTS未割当て・native不在の主人公台詞 | 閲覧時間終了で1回だけ進行 |
| A02 | TTS=SKIP、native不在 | 読上げはせず閲覧時間で進行 |
| A03 | デフォルトvoiceが未設定 | TTSを待ち続けず無音閲覧へ |
| A04 | `……`、`――`、`！`のみ | 原文イベントを維持し、記号専用時間後に進行 |
| A05 | `……`に実ゲームボイスあり | 記号時間で飛ばさずボイス終了まで待つ |
| A06 | `あっ`・`っ`・`ん` | 記号ではなく発声可能本文として扱う |
| A07 | 本文なし・行イベントなし | 自動入力しない |
| A08 | 表示完了が確認された空台詞イベント | 無音待機後に1回だけ進行 |
| A09 | 文字送り途中の連続HOOK出力 | 1つの台詞世代で処理し、完成前に送らない |
| A10 | 同一原文の連続した別台詞 | 両方を別々の台詞として処理 |
| A11 | 未収録長文 | 文字数に比例した待機を確保 |
| A12 | 無音待機中にnative音声開始 | 待機を取消し、ボイス末尾まで待つ |
| A13 | TTS途中で当該台詞のnative開始 | TTSを中断し、ゲーム音声終了を待つ |
| A14 | TTS生成失敗・native不在 | 通知後、無音閲覧で進行 |
| A15 | nativeあり・ASRが末尾未確認 | 進行せず停止 |
| A16 | BGM・SEのみ、音声なしが十分確認可能 | 音声と誤認せず無音閲覧で進行 |
| A17 | 音声キャプチャ失敗 | ボイスなしと誤認せず停止 |
| A18 | 選択肢・マップ・メニュー・不明画面 | 自動入力停止 |
| A19 | 手動クリックと自動送り候補が競合 | 自動入力が重複しない |
| A20 | TTS/ASR/タイマーの旧世代通知が遅延到着 | 現在世代へ影響しない |
| A21 | 送り入力後にテキストが変わらない | 無条件再送しない |
| A22 | ロードで同一台詞が再登場 | 新しい世代として処理 |
| A23 | 取得テキストは空白、画面はトランジション | 入力せず遷移を待つ |
| A24 | 台詞末尾に長い無音・途中間がある | 発話途中の誤送りをしない |
| A25 | 前行の音声が持続したまま次の無音台詞が表示される | 前行の音声を新行の音声と誤帰属せず、現在行の条件で進行可否を判断 |
| A26 | 同一文字列の連続表示で行境界を観測できない | 行世代を捏造して送信せず、Adapterで解決するか明示停止 |

### 13.2 評価指標

- `False Advance Rate`：提示完了前の誤送り率（最優先、原因を全件解析）。
- `Silent Completion Rate`：自動進行可能な無音行を正しく進めた率。
- `Unnecessary Pause Rate`：正常行での不要停止率。
- `Native End Latency p50/p95/p99`：実発話終了から進行許可までの時間。
- `TTS/Native Overlap Duration`：音声競合の重複時間。
- `Choice/Map Stop Recall`：必要な手動操作画面での停止率。
- `Duplicate Input Count`、`Stale Event Applied Count`：いずれも0を必須条件とする。
- 追加でASR推論負荷、音声キャプチャ断、タイムアウト件数を記録する。

最初に正常・短文・無音・ボイス遅延を含む100台詞程度で経路成立を測り、300～500台詞で閾値を校正する。一般化した誤進行率の低さを主張する場合には、それに見合う独立試行数を確保する（例：3,000件で0件観測なら二項近似による95%上限は約0.1%）。試験行数を達成しただけで完了とはしない。

### 13.3 ログ契約

1台詞ごとに`session_id`、`line_epoch`、原文hash、音声判定根拠、TTS設定と実選択、画面状態、表示完了時刻、native onset/end、ASR coverage/tail confidence、TTS再生結果、無音閲覧開始・終了、最終判断理由、入力時刻、遷移ACK、停止理由、使用モデル・設定バージョンを構造化記録する。通常は軽量なメタデータのみ保存し、音声・画面の原データ保存は診断時の任意設定とする。

## 14. 実装完了条件

以下を**全て**満たして初めて完了とする。

1. ゲームボイスあり、TTSあり、TTS未割当て、明示Skip、記号のみ、空台詞イベント、演出・選択画面が単一Controllerを通して処理される。
2. ゲーム音声が遅れて開始してもTTS中断、待機取消、進行入力抑止が連動し、旧非同期結果による二重送りがない。
3. 音声が本来存在しない通常台詞で、ASR完了待ちやTTS完了待ちに陥らず、自動進行できる。
4. 音声存在不明・取得障害・発話終了未確認を、無音タイマーによる強制進行で隠蔽しない。
5. 進行に必要な台詞イベントをLunaの翻訳・読上げフィルタで失わない。特に記号のみ・重複本文・空台詞の再現テストを通過する。
6. 選択肢・マップ・メニュー・不明画面で自動入力しない。手動操作と共存できる。
7. ゲーム固有処理がAdapter/profileへ収まり、モデル・TTS差替え時に進行状態機械を変更する必要がない。
8. 再現fixtureによる状態遷移・競合・障害検証に加え、**代表実ゲームで**少なくとも上記必須試験の該当ケースと有音/無音混在の連続進行を検証する。実ゲーム環境を利用できなかった場合は共通基盤の検証結果のみを報告し、実ゲーム運用については完了と判定しない。作品ごとに未確認のHOOK・音声・画面挙動も明記する。
9. 実装した機能の操作方法、初期設定、診断ログ、接続失敗時の復旧方法を提供する。使われない旧経路・重複ロジックを放置しない。

## 15. 参照資料・確認対象

- 添付調査：『市販ノベルゲームの「台詞テキスト × ゲーム音声」リアルタイム照合による自動進行基盤の調査』
- 添付調査：『市販ノベルゲームにおける選択的TTS補完と自動進行の実現可能性調査』
- LunaTranslator公式ネットワークAPI：https://docs.lunatranslator.org/ja/apiservice
- LunaTranslatorソース（取得・前処理・TTS自動送りの接続点）：https://github.com/HIllya51/LunaTranslator/blob/main/src/LunaTranslator/LunaTranslator.py
- LunaTranslatorリポジトリ：https://github.com/HIllya51/LunaTranslator
- HoyoVoice（ゲームボイス検出とlate-VO yieldの参考）：https://github.com/ebbyw/HoyoVoice
- Irodori-TTS-Server：https://github.com/Aratako/Irodori-TTS-Server

**確認の扱い**：公開資料から明らかなAPI名と挙動は実装の出発点とする。ただし、現行LunaのWebSocketだけで必要な全情報が揃うこと、対象ゲームのHOOK/voice-IDが取得できること、VAD/ASRの特定閾値で目的精度が出ることは未検証事項であり、コード調査・実機検証により確定する。基準に不足が見つかった場合も、進行権限の一元化と無音を正式な提示経路とする基本構造は維持したうえで修正する。