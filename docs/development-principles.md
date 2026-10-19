# AutoAdv 開発原則

1. 完成後の責務・状態所有・イベント順を先に確定する。`Controller`以外に進行権限を与えない。
2. LunaTranslatorとゲーム、Windows音声/画面、Irodori、入力ドライバは交換可能なAdapterとする。
3. 原文受信・音声VAD・無音時間の単体観測から台詞境界や終了を捏造しない。未知は停止させる。
4. `session_id`、`line_epoch`、`cancel_token`、monotonic timestampを非同期イベントに付与する。
5. 正準仕様の不変条件・状態遷移・責務境界をテストし、実機未検証をfixture成立で代替しない。
6. 通常開発の対象branchは`develop`。`main`は明示承認まで不変更。Space-Idle由来Publish Gatewayを使うには、AutoAdv専用publish/CIの正規bootstrapとsource-snapshotが必要。
7. 局所修正で旧経路を残さず、責務を縦断する共通契約へ統合する。