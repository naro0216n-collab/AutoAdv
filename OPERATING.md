# AutoAdv 実行・校正手順

## 前提

対象はWindows 10 version 2004（build 19041）以上。Python 3.11以上、.NET SDK 9、LunaTranslator、Irodori-TTS-Server（TTS使用時）、登録済みVoice ID、sherpa-onnx対応日本語モデルを準備する。Luna側HOOK/OCRの取得成功や対象ゲームの音声アセットは事前には保証しない。ゲームとAutoAdv/Irodori/loopback helperは同一プロセスツリーにしない。LunaのゲームAutoと `ttsautoforward` は両方OFFとし、入力権限はControllerへ一元化する。

```powershell
py -3.11 -m pip install -e ".[live]"
dotnet build integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj -c Release
py -3.11 scripts/luna_bridge_patch.py <LunaTranslator.pyのあるフォルダ> --install
py -3.11 -m autoadv bridge-monitor
```

上記のLunaパッチは`textgetmethod_1`でのフィルタ前候補だけを送信する。表示完了・台詞世代・ボイス割当てを送信するものではない。パッチは任意で、戻すときは `--remove`。ゲーム固有のHOOK/OCRより上流で空台詞が失われる場合、アプリ側だけで復元しない。

## プロファイル

`examples/live-profile.example.json`と`examples/screen-profile.example.json`を作品用に複製し、ゲームのPID/HWND、画面内テキスト領域、各スクリーン状態の**正の視覚マーカー**のROI画像、モデルパス、Irodori voiceを登録する。サンプルの座標と数値は実ゲーム用校正データではない。`cv2`比較は画面解像度/拡大率に依存するため、対象ウィンドウの実クライアント領域を使う。`text_ready`は文末の送り待ちマーカーなど表示完了が確実な時だけ出現する画像とする。

画面はDialogue、Choice、Map、Menu、Transitionを個別に検出する。衝突または未検出はUnknownとなる。さらに画面本文を日本語OCRで読み、Luna原文と照合する。OCR失敗は送信停止であり、未確認のText Readyを自動補完しない。

ゲーム側から現在台詞のボイスアセット割当てを**実際に検証できるAdapter**を用意した作品だけ、`game_metadata_port`に`bridge_port`とは異なるローカルUDPポート番号を設定できる。無効時は`null`。接続時は実行前に環境変数`AUTOADV_GAME_METADATA_KEY`へ16バイト以上の共有鍵を登録し、Adapter側は`autoadv.game_metadata.signed_asset_packet`で確証付きメッセージにHMAC署名する。報告には`session_id`、`epoch`、`token`、`source_line_id`、連番、`assigned`、`asset_id`、`proof="game_current_line_asset_lookup"`が必要である。Controllerの`line_state`に現れる世代識別子と結び付かない報告、古い報告、未認証の報告は採用しない。これは任意の作品別Adapterを接続する境界であり、音声アセットの存在を一般化して推測する機能ではない。Adapter未校正時は`null`のままとする。

音声キャプチャはWASAPI process-loopbackで対象ゲームPIDと子プロセスだけを取得し、外部TTSは取り込まない。PC内で別デバイスのシステム音を混ぜる代替経路は設けない。音声が無音でもhelperはheartbeatを出すが、それは「ボイスなし」の証明ではない。サンプル保存は行わず、JSONLにはイベント種別・epoch・判定根拠・停止理由等を記録する。各行の`line_state`/`line_summary`には原文のSHA-256、音声根拠、提示経路、Ready時刻、native/TTS開始・終了、input時刻、遷移ACK、停止理由とモデル識別子を含める。ASR分析時のcoverage、tail confidence、VAD末尾無音は`asr_progress`で記録し、最終`line_summary`にも統合する。実際の誤送り率や不要停止率の確定には、ゲーム実測と独立に正解ラベルを付与した評価が必要である。

**未校正の作品では `allow_heuristic_absence:false`を維持する。** ゲーム内の音声割当て情報が取得できない無音台詞は、検出不能なら停止する。trueは音声開始遅延・SE/BGM・誤送りログを作品別に実測した場合のみに設定する。trueであってもVAD等で曖昧な音声が検出された場合は解決不能として停止する。確率的に絶対的な誤送りゼロは保証できない。

ゲーム別の進行入力は`advance_input`で`{"mode":"keyboard","virtual_key":13}`（Enter）、`{"mode":"keyboard","virtual_key":32}`（Space）、`{"mode":"mouse_left","virtual_key":13}`（現在のカーソル位置で左クリック）などを選択する。入力はController直下の単一ドライバのみが行い、SendInputには固有の発生元タグを付ける。Windows低レベルフックが物理/他アプリ由来の操作を検出したら停止し、フック導入失敗時も入力を許可しない。マウス操作モードでは、選択肢表示を含む画面停止判定を十分に校正すること。

TTSの割当ては`tts.policy`および`speaker_rules`で管理する。`SKIP`は台詞自体を飛ばさず、native不在を確認したうえで無音閲覧する。DEFAULTは実際に利用できるデフォルトvoiceがあるときだけ使用する。`name_readings`は固有作品の読みを登録し、複合名に勝手に置換しない。`京(みやこ)`のようなルビはベース漢字を読み上げず読みを残す。

## 起動

まず**入力なし診断モード**で実行する。

```powershell
py -3.11 -m autoadv run-windows --config examples/live-profile.json
```

Lunaフィルタ前パッチとサーバの接続、画面判定、音声、ASR、TTS再生、JSONL停止理由を検証し、作品別校正を完了させた後だけ次を実行する。

```powershell
py -3.11 -m autoadv run-windows --config examples/live-profile.json --enable-input
```

対象ウィンドウ以外への入力、手動介入、選択肢・地図・メニュー・不明画面、capture断、表示未完了、native終了不明、ACK欠落は送信停止。送信後の黙示的な再送はない。中断したらログを確認し、ゲーム画面状態を手動で回復したうえで新セッションとして再実行する。`GameProfile`の汎用Controllerにゲーム固有ルールは追加しない。

## 実機検証未達の項目

このリポジトリのLinux単体テストはWindows COM/NAudio/.NETビルド、実ゲーム、LunaのHOOK、有効な日本語OCR/ASRモデル、TTSデバイスdrain、実SE/BGM混合を検証しない。代表作品における指示書 A01～A26 の該当受入と、連続100行以上の音声/無音混在運用はWindows実測による別途確認が必須である。

## 監査ログの定量評価

```powershell
py -3.11 -m autoadv audit-evaluate logs/autoadv.jsonl
py -3.11 -m autoadv audit-evaluate logs/autoadv.jsonl --labels logs/verified-trials.csv
```

正解ラベルCSVは `session_id,line_epoch,false_advance,silent_eligible,should_advance,choice_map_blocker` を列とし、実画面・音声の独立確認によって真偽を入力する。ラベルなしの集計は注入回数、重複、停止理由、**認識器が判定した**音声終了から入力までの遅延などに限定する。実発話終了時刻をラベル化していない場合、実測Native End LatencyやTTS/Nativeの実再生重複時間を計測済みと表示しない。
