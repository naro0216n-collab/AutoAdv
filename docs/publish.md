# Publish Gateway の導入境界

正準仕様は `docs/integration-spec.md`。Publishの管理手順にはSpace-Idleの標準経路を流用する。

## コピー対象

`python scripts/port_publish_gateway.py --write` はSpace-Idleの固定develop commitから次のcontrol planeを取得する：`publish_request.py`、`publish_gateway_validate.py`、`publish_connector_launch.js`、`publish_connector_executor.js`、`workflow_maintenance.py`、`publish_control_maintenance.py`、`publish-gateway.yml`。転送スロット・manifest生成・再送/record手順は変更しない。

`AutoAdv`のリポジトリ識別子と `refs/autoadv/` に限って置換し、Fast CIは本プロジェクトのPython test / clean install用に置き換える。

## 初回導入

リモートが空の場合は`main`、`develop`、`publish`のcommitもFast CI `source-snapshot`も存在しない。通常の `publish_request.py init` はこの状態では動作しない。**既存のSpace-IdleのsnapshotをAutoAdvの初期snapshotと偽って使ってはならない。**

まず正式な初期セットアップにより`develop`/`publish`とGitHub Actionsを成立させ、成功した`develop` Fast CIの `source-snapshot` artifactを生成すること。`main`の既存承認条件は変更しない。その後は通常通りartifactをgit bundleとして復元し、`scripts/publish_request.py init`、必要な実装・検証・commit、`prepare`、最新両HEAD取得、`connector-plan`、固定Launcher、成功runの`record`を厳守する。

現在この環境からのGitHubへの初回branch生成・Gateway経由publishは未成立である。ローカルの実装やテストをFast CI成功と読み替えない。