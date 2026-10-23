# AutoAdv Publish Gateway 運用

通常開発は`develop`。`main`の更新とversion変更はユーザーの明示承認後のみ。通常開発の中継に`temp`や追加worktreeを使用しない。今回のゼロベース再構築では、利用者指定の例外として旧実装を`temp`に固定保管する。`temp`を開発・公開経路へ転用しない。

## ローカル作業環境の初期構築と継続利用

**ローカルrepoが存在しない初回構築・作業環境喪失時に限り**、最新`develop`のFast CIによる`source-snapshot`（`repository.bundle`と`.source-*`）からrepoを復元し、repo-local identityを設定して`scripts/publish_request.py init`を実行する。

**通常の継続作業では、既存のローカルrepoをそのまま使う。** publishの成功、Fast CIの完了、次の実装開始を理由としてスナップショットを再取得・再展開したり、`init`を再実行したりしない。スナップショットは復旧用の成果物であり、毎回の同期・作業開始・publish後処理ではない。ローカルの成立済みcommitと未commitの変更を保持し、再取得・rebase・resetで作り直さない。

workflow保守専用経路で`scripts/workflow_maintenance.py record-update`を完了すると、現行のpublish helperは再復元を明示的に要求する。この**例外が発生した場合だけ**、最新`develop`の新しい`source-snapshot`から復元して`init`を行う。既存のローカル変更・commitを失わせず、保守変更の必要性と継続する作業状態を確認してから実施する。通常の`publish_request.py record`後にはこの例外を適用しない。

## 通常のpublishサイクル

実装・関連検証・ローカルcommit後は`scripts/publish_request.py prepare`を実行する。publish直前のremote `develop`と`publish` HEADを取得し、helperが出力するconnector-planと固定Launcherの手順を**一切改変せず**記載順に実行する。Gateway成功後、結果を`scripts/publish_request.py record`に渡して既存ローカルrepoのpublish基点を更新し、そのrepoで次の作業を続ける。次のpublish前に直前のFast CI結果を確認し、失敗原因を解消する。標準経路に失敗した場合は`DEVELOPMENT.md`の指定手順を参照し、独自経路を作らない。

Fast CIは継続検証であり設計妥当性の証明ではない。Lunaネイティブ取込み時は64bit Windows上でx86/x64ゲームプロセス向けNativeを実ビルドし、接続検証を別途実施する。x86 buildは32bit OS対応ではなく32bitゲームへの注入互換性を検証する。Windows実ゲーム検証をモック成功で代替しない。

## 継続CIと明示的な重量級検証

- `develop` への通常publishでは Fast CI（単体契約・clean install・復元用source-snapshot）を実行する。Fast CIから別の重量級ワークフローを起動しない。
- Windows Adapter Validationは、検証専用のドラフトPR `develop` → `main` の **close → reopen** で実行する。PRの `pull_request: reopened` のみをトリガーにし、単なる `develop` のpushやPR作成では起動しない。
- Validation Fixture Runtimeは、`validation/real_game/renpy/**`等のfixture構築入力を変更したときに明示実行し、prebuilt Windows検証ゲームを専用pre-release `autoadv-validation-fixtures` へfixtureソースSHA-256付きassetとして保存する。通常publishやFast CIから重量級buildを自動連鎖させない。これはAutoAdv製品のversion/releaseではなく検証用binaryの保管であり、AutoAdvの合否検証でもない。AutoAdv Live Advancementは現行fixtureと一致するready-to-run assetだけを使用し、Live run内でRen'Py SDK取得・ゲーム配布物生成を行わない。
- AutoAdv Live Advancementは検証用draft PRのタイトルを`[RUN AUTOADV LIVE]`で始める編集時だけ実行する。Hosted Windowsではprebuilt fixtureを使い、実Hook→文字判断のController→Windows入力→次Hookだけを検収する。Process Loopback・ASR・NativeAudioは実行せず、RealAudioの合否には使わない。
- Luna Native Source and ABI Validationは通常開発CIから外す。Luna採用版の更新・Native patch変更を明示的に検収するときだけ、検証用ドラフトPRのタイトルへ`[RUN LUNA NATIVE]`を追加する編集で起動する。64bit Windows上でx86/x64ゲームプロセス向けに、AutoAdvがpatchするLunaHost DLLだけを双方実ビルドし、x86 buildは32bit OS対応ではなく32bitゲームへの注入互換性を検証する。未改変のLunaHook本体を通常検証で再ビルドせず、上流版が変わらないGUI・Domain変更で同じNative完全buildを反復しない。検収後はタイトルのマーカーを外す。
- RealAudio実ゲーム検収は`AutoAdv-Windows-Validate.cmd`からGit管理下の`run_windows_validation.ps1`を起動し、実際にAutoAdvを利用する対話可能なWindows実機で明示実行する。固定ASR runtimeの取得、Process Loopback helper build、NativeAudio/Controller→実入力→次Hookはこの実機経路だけが担当する。原音ケースは同一ゲーム・同一AutoAdvセッションで連続実行する。AutoAdvが保留・失敗して後続ケースへ到達できない場合だけ、検証driverは失敗を`validation_recovery_input`として確定記録した後に対象ゲームへ回復入力を一度送り、後続ケースのデータ収集を継続する。この入力をAutoAdvの成功や次Hook成立へ算入せず、回復入力が存在するrunは無介入の連続自動進行成功として扱わない。検証driverが強制入力を送る直前の既知境界だけは、同文反復を含む後続ケース収集のため`confirm_external_advance()`で製品へ通知してよいが、AutoAdv入力証拠や成功判定にはしない。ケース別再起動で失敗地点を隠さない。Fast CI、Windows Adapter Validation、AutoAdv Live Advancement、その他の一括／FullテストへRealAudioを追加しない。
- 実録音・実モデルASRの品質／速度測定もGitHub Actionsの重量級スイートから外し、必要時にWindows実機で明示実行する。CIランナーの計測値を実機性能やRealAudio成立の代理にしない。
- 検証用PRは **マージ禁止**。`main`を更新する権限や承認を意味しない。headが同じリポジトリの`develop`、baseが`main`の場合だけ重量級jobを実行する。追加の開発branch・worktreeは作らない。
- PR上の検証はGitHubのマージ用refで行う。`main`が`develop`の祖先である間はツリーが`develop`と一致するが、両者が分岐した場合は`develop`単独の検証結果と同一視せず、入力commitを記録する。
- 通常のFast CIとPR重量級検証は別の合否。Hosted LiveのHOOK→文字判断→入力→次HOOK、実モデルの品質・速度測定、Windows実機RealAudio検収も相互に代用しない。
- Luna Release Asset Adoptionは採用操作であり、PR検証スイートへ混ぜない。Publish Gatewayの経路・権限は変更しない。
