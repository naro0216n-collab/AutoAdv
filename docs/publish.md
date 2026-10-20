# AutoAdv Publish Gateway 運用

通常開発は`develop`。`main`の更新とversion変更はユーザーの明示承認後のみ。通常開発の中継に`temp`や追加worktreeを使用しない。今回のゼロベース再構築では、利用者指定の例外として旧実装を`temp`に固定保管する。`temp`を開発・公開経路へ転用しない。

## ローカル作業環境の初期構築と継続利用

**ローカルrepoが存在しない初回構築・作業環境喪失時に限り**、最新`develop`のFast CIによる`source-snapshot`（`repository.bundle`と`.source-*`）からrepoを復元し、repo-local identityを設定して`scripts/publish_request.py init`を実行する。

**通常の継続作業では、既存のローカルrepoをそのまま使う。** publishの成功、Fast CIの完了、次の実装開始を理由としてスナップショットを再取得・再展開したり、`init`を再実行したりしない。スナップショットは復旧用の成果物であり、毎回の同期・作業開始・publish後処理ではない。ローカルの成立済みcommitと未commitの変更を保持し、再取得・rebase・resetで作り直さない。

workflow保守専用経路で`scripts/workflow_maintenance.py record-update`を完了すると、現行のpublish helperは再復元を明示的に要求する。この**例外が発生した場合だけ**、最新`develop`の新しい`source-snapshot`から復元して`init`を行う。既存のローカル変更・commitを失わせず、保守変更の必要性と継続する作業状態を確認してから実施する。通常の`publish_request.py record`後にはこの例外を適用しない。

## 通常のpublishサイクル

実装・関連検証・ローカルcommit後は`scripts/publish_request.py prepare`を実行する。publish直前のremote `develop`と`publish` HEADを取得し、helperが出力するconnector-planと固定Launcherの手順を**一切改変せず**記載順に実行する。Gateway成功後、結果を`scripts/publish_request.py record`に渡して既存ローカルrepoのpublish基点を更新し、そのrepoで次の作業を続ける。次のpublish前に直前のFast CI結果を確認し、失敗原因を解消する。標準経路に失敗した場合は`DEVELOPMENT.md`の指定手順を参照し、独自経路を作らない。

Fast CIは継続検証であり設計妥当性の証明ではない。Lunaネイティブ取込み時はx86/x64 Windows実ビルドと接続検証を別途実施し、既存のWindows CIが新規ソースで発火することを確認する。Windows実ゲーム検証をモック成功で代替しない。
