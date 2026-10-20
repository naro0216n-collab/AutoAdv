# AutoAdv 操作・検証ガイド

正準仕様は `docs/integration-spec.md`。ここでは新実装で利用できる操作と、実環境検証が必要な境界を説明する。

1. Python 3.11で `python -m pip install -e ".[gui,audio,ocr,native]"`、`python -m autoadv`。使わないOCR・ASRは導入不要であり、TTSだけなら `.[gui,audio]` で足りる。
2. Irodori-TTS-Serverの既存フォルダ・URLを入力して接続・自動起動・Voice ID取得を確認する。GUIのVoice試聴で再生を確認する。
3. ゲーム名・実行ファイルを登録し、必要ならGUIからゲームを起動する。登録済みの実行ファイルを基準にウィンドウ候補を表示でき、重複した場合は利用者が選ぶ。実行中PID/HWNDを手入力してもよい。
4. LunaHost.dllを指定して起動し、ゲームPIDへ接続する。探索結果からHOOKコードを挿入するか、受信した候補HOOKの原文プレビューを見て本文・話者用を選択する。別HOOKで話者と本文が届く場合は対応関係を登録する。手動挿入HOOKは作品プロファイルに保存する。
5. ゲーム別の話者構造の正規表現、読み辞書、ルビ、置換、話者別原音/TTS/自動選択/閲覧モード、Voice ID、送りキー・閲覧時間を設定する。プロファイルを複製・改名・削除して切り替えられる。Luna 5.3.1形式の保存データは内容を確認してから読み取り専用で一方向に取り込む。
6. HOOKが使えない作品は明示的にOCRを選び、Tesseractの既存実行ファイル・言語・ゲームクライアント内の領域を設定する。OCRを使わないゲームには設定を要求しない。
7. 原音と自動選択には、Windows Process Loopbackヘルパー、SenseVoice対応モデル、tokensのパスを設定する。ヘルパーは `dotnet build integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj -c Release` でビルドできる（.NET 9とWindowsが必要）。TTSと閲覧モードだけの利用にはASRが不要。
8. 監視開始後、原文・Utterance・使用Voiceと進行状況を確認し、必要な場合だけ別操作で自動送りを許可する。ゲームウィンドウがフォアグラウンドでない場合や手動介入があった場合は入力を送らない。一時停止・再許可・ゲーム切替で古い処理が復活しない。

原音は**その台詞に一致した音声**の検出と終了を条件とする。VADや無音だけでは完了や音声不存在を確定しない。自動選択は設定した待機時間の後にTTSへフォールバックし、後から一致した原音が確認されればTTSを取り消す。原音・TTS・閲覧時間の状態は同じUtterance/Controllerに報告され、入力を直接実行しない。

ローカルで `python -m unittest discover -s tests -v` を実行する。Windows CIではGUIの構築・Process Loopbackのビルド等を別途検証する。**実ゲームでの動作・音声一致率・ゲーム間の安定性は未検証であり、単体/CI成功と区別する。**
