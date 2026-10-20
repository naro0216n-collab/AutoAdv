# AutoAdv 正準文書

AutoAdvは個人利用のWindows GUIアプリである。LunaHookによるゲーム台詞取得、柔軟な話者別Irodori TTS、ゲームの音声収録状況にかかわらない会話自動進行を一つの操作体系で提供する。

- [`integration-spec.md`](integration-spec.md)：完成時の**製品仕様**。利用手順、機能、実行時の動作、設定、受入条件を定める。
- [`development-principles.md`](development-principles.md)：設計・実装・検証の判断原則。
- [`rebuild-transition.md`](rebuild-transition.md)：現行実装から完成時モデルへ移るための、**製品仕様とは別の**作業計画。
- [`publish.md`](publish.md)：ローカル作業環境、branch、Publish Gateway、CIの運用手順。

完成時の要件は`integration-spec.md`を正本とし、`development-principles.md`で実装の妥当性を判断する。移行計画・現行コード・既存テスト・過去の指示書は完成時の仕様を上書きしない。矛盾が見つかった場合は仕様と実装を照合し、実用上の結果を基準に是正する。

通常の共有開発branchは`develop`。`main`とversion変更は利用者の承認後に限る。
