# AutoAdv 正準文書

AutoAdvは個人利用のWindows GUIアプリである。目的は、**LunaHookによるゲーム台詞取得、話者・読み・Voiceを柔軟に指定できる選択的Irodori TTS、既存音声の有無にかかわらない会話自動進行**を一つの操作で実現すること。

文書の役割は次のとおり。

- [`integration-spec.md`](integration-spec.md)：完成時の**唯一の製品仕様**。機能、実行時の振舞い、状態所有、受入条件を定義する。
- [`development-principles.md`](development-principles.md)：設計・実装・検証の判断原則。実装の存在自体を要求する文書ではない。
- [`publish.md`](publish.md)：開発branch、ローカル作業環境、既存Publish Gatewayの運用手順。
- [`rebuild-transition.md`](rebuild-transition.md)：ゼロベース再構築へ移るための**作業計画**。旧コードの仕分けと移行順序を記載し、製品仕様の正本にはしない。

要件・仕様を判断するときは`integration-spec.md`を最優先する。現在のコード、既存テスト、fixture、過去の実装指示書、移行文書に書かれた旧構成は完成時仕様の根拠としない。矛盾時は現行コードを正当化するために仕様を読み替えず、仕様から実装・テストを再評価する。

既存機能を利用して製品の中核以外のことが実現可能でも、それ自体は廃止理由にならない。対象外機能を*不可能にすること*は要求ではなく、その不成立を保証するテストも設けない。

`main`とversion変更は明示承認後に限る。通常開発の共有正本は`develop`とし、作業・publishの規則は`publish.md`に従う。
