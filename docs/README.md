# AutoAdv 文書

AutoAdvは個人利用のWindows GUIアプリである。LunaHookによるゲーム台詞取得、柔軟な話者別Irodori TTS、ゲームの音声収録状況にかかわらない会話自動進行を一つの操作体系で提供する。

## 正準文書

- [`integration-spec.md`](integration-spec.md)：完成時に成立し続ける**製品仕様と不変契約**。利用者が得る結果、機能の目的、状態所有、受入条件を定める。特定のクラス構造、閾値、移行手順、現在の実装方法は正準化しない。
- [`development-principles.md`](development-principles.md)：設計・実装・検証で維持する不変原則。

現行コード、既存テスト、fixture、過去の指示書、移行計画、実装履歴は正準仕様を上書きしない。作業計画や一時的な設計メモを`docs`へ正準文書として蓄積せず、必要な設計変更は完成時にも残る目的・契約へ還元して上記文書へ反映する。

## 運用手順

- [`publish.md`](publish.md)：ローカル作業環境、branch、Publish Gateway、CIの運用手順。製品仕様ではなく、共有正本を安全に更新するための運用契約である。

通常の共有開発branchは`develop`。`main`とversion変更は利用者の承認後に限る。
