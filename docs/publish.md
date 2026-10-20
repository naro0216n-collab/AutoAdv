# AutoAdv Publish Gateway 運用

通常開発は`develop`。`main`の更新とversion変更はユーザーの明示承認後のみ。通常開発の中継に`temp`や追加worktreeを使用しない。

最新`develop`のFast CIによる`source-snapshot`（`repository.bundle`と`.source-*`）からローカルrepoを復元し、repo-local identityを設定して`scripts/publish_request.py init`を実行する。

実装・関連検証・ローカルcommit後は`scripts/publish_request.py prepare`を実行する。publish直前のremote `develop`と`publish` HEADを取得し、helperが出力するconnector-planと固定Launcherの手順を**一切改変せず**記載順に実行する。Gateway成功後、結果を`scripts/publish_request.py record`に渡す。次のpublish前に直前のFast CI結果を確認し、失敗原因を解消する。標準経路に失敗した場合は`DEVELOPMENT.md`の指定手順を参照し、独自経路を作らない。

Fast CIは継続検証であり設計妥当性の証明ではない。Lunaネイティブ取込み時はx86/x64 Windows実ビルドと接続検証を別途実施し、既存のWindows CIが新規ソースで発火することを確認する。Windows実ゲーム検証をモック成功で代替しない。
