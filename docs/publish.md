# AutoAdv Publish Gateway 運用

通常開発は`develop`。`main`の更新とversion変更はユーザーの明示承認後のみ。通常開発の中継に`temp`や追加worktreeを使用しない。今回のゼロベース再構築では、利用者指定の例外として旧実装を`temp`に固定保管する。`temp`を開発・公開経路へ転用しない。

## ローカル作業環境の初期構築と継続利用

**ローカルrepoが存在しない初回構築・作業環境喪失時に限り**、最新`develop`のFast CIによる`source-snapshot`（`repository.bundle`と`.source-*`）からrepoを復元し、repo-local identityを設定して`scripts/publish_request.py init`を実行する。

**通常の継続作業では、既存のローカルrepoをそのまま使う。** publishの成功、Fast CIの完了、次の実装開始を理由としてスナップショットを再取得・再展開したり、`init`を再実行したりしない。スナップショットは復旧用の成果物であり、毎回の同期・作業開始・publish後処理ではない。ローカルの成立済みcommitと未commitの変更を保持し、再取得・rebase・resetで作り直さない。

workflow保守専用経路で`scripts/workflow_maintenance.py record-update`を完了すると、現行のpublish helperは再復元を明示的に要求する。この**例外が発生した場合だけ**、最新`develop`の新しい`source-snapshot`から復元して`init`を行う。既存のローカル変更・commitを失わせず、保守変更の必要性と継続する作業状態を確認してから実施する。通常の`publish_request.py record`後にはこの例外を適用しない。

## 通常のpublishサイクル

実装・関連検証・ローカルcommit後は`scripts/publish_request.py prepare`を実行する。publish直前のremote `develop`と`publish` HEADを取得し、helperが出力するconnector-planと固定Launcherの手順を**一切改変せず**記載順に実行する。Gateway成功後、結果を`scripts/publish_request.py record`に渡して既存ローカルrepoのpublish基点を更新し、そのrepoで次の作業を続ける。次のpublish前に直前のFast CI結果を確認し、失敗原因を解消する。標準経路に失敗した場合は`DEVELOPMENT.md`の指定手順を参照し、独自経路を作らない。

同時進行で`prepare`後にremote `develop`が先へ進んだ場合は、成立済みlocal commitをrebase・reset・作り直しせず、最新`develop`を現在の`develop`へmergeして関連検証を行う。観測した`develop`/`publish` HEADを`scripts/publish_request.py refresh-base`へ渡して基点を前進させ、その後に新しく`prepare`する。connector-plan前は通常のstale transactionを破棄する。connector-plan後は、旧baseから`develop`がfast-forwardしており、かつその履歴に当該transactionの正確なpublish commitが含まれないことをhelperが検証できる場合だけ、到達不能なGit objectや失効したtransportを製品publishとはみなさずstale transactionを破棄できる。当該publish commitが`develop`履歴に入っている場合や、ref履歴を検証できない場合は破棄せず`record`または診断を行う。

Fast CIは継続検証であり設計妥当性の証明ではない。Lunaネイティブ取込み時は64bit Windows上でx86/x64ゲームプロセス向けNativeを実ビルドし、接続検証を別途実施する。x86 buildは32bit OS対応ではなく32bitゲームへの注入互換性を検証する。Windows実ゲーム検証をモック成功で代替しない。

## 継続CIと明示的な重量級検証

- `develop` への通常publishでは Fast CI（単体契約・clean install・復元用source-snapshot）を実行する。Fast CIから別の重量級ワークフローを起動しない。
- Windows Adapter Validationは、検証専用のドラフトPR `develop` → `main` の **close → reopen** で実行する。PRの `pull_request: reopened` のみをトリガーにし、単なる `develop` のpushやPR作成では起動しない。
- Validation Fixture Runtimeは、`validation/real_game/renpy/**`またはfixture配布契約が`develop`へpublishされた場合だけPublish Gatewayが明示dispatchし、prebuilt Windows検証ゲームを専用pre-release `autoadv-validation-fixtures` へfixtureソースSHA-256付きassetとして保存する。GitHub Actionsの`GITHUB_TOKEN`による`develop` pushが別workflowを暗黙起動することには依存しない。通常の製品publishやFast CIから無条件に重量級buildを連鎖させず、`workflow_dispatch`は欠損asset再生成用の回復経路として残す。Windows実機の自動進行精度計測とHosted Live Advancementは共通の`fixture_runtime stage`でrelease名・asset名・取得URLを導出し、publish直後で対応assetが生成中なら同一assetを一定時間待ってから取得・展開・manifest照合する。consumer側のPowerShell/workflowにrelease登録情報やURL組立を重複保持しない。これはAutoAdv製品のversion/releaseではなく検証用binaryの保管であり、AutoAdvの合否検証でもない。Live run内でRen'Py SDK取得・ゲーム配布物生成は行わない。
- AutoAdv Live Advancementは検証用draft PRのタイトルを`[RUN AUTOADV LIVE]`で始める編集時だけ実行する。Hosted Windowsではprebuilt fixtureを使い、実Hook→文字判断のController→Windows入力→次Hookだけを検収する。ゲーム原音の音声判断経路は実行せず、自動進行精度実機計測の成立証拠には使わない。
- Luna Native Source and ABI Validationは通常開発CIから外す。Luna採用版の更新・Native patch変更を明示的に検収するときだけ、検証用ドラフトPRのタイトルへ`[RUN LUNA NATIVE]`を追加する編集で起動する。64bit Windows上でx86/x64ゲームプロセス向けに、AutoAdvがpatchするLunaHost DLLだけを双方実ビルドし、x86 buildは32bit OS対応ではなく32bitゲームへの注入互換性を検証する。未改変のLunaHook本体を通常検証で再ビルドせず、上流版が変わらないGUI・Domain変更で同じNative完全buildを反復しない。検収後はタイトルのマーカーを外す。
- 自動進行精度実機計測は`AutoAdv-Windows-Validate.cmd`からGit管理下の`run_windows_validation.ps1`を起動し、実際にAutoAdvを利用する対話可能なWindows実機で明示実行する。通常起動と同じmanaged Windows runtimeのG2P・Process Loopback・音響observer・forced alignmentを使い、検証専用venv・検証専用model配置・runごとのhelper再buildを作らない。Hosted `reading`は重複実行せず、`accuracy` phaseではケース件数を固定契約にせず、HOOK遅延、本文-duration非相関、非言語発声、vocal loop/persistence、BGM/SE/背景発話、原音なし等の製品上異なる入力同値クラスを、一つのゲーム・選択HOOK・AutoAdvセッションで連続計測する。accuracy phaseはフォールバックを常にONにして要求ケース全件の収集を続け、音声判断完了、明示fallback、独立source acoustic endpointより前の入力、endpoint後遅延、誤帰属、非言語中の早送り、反復音による過剰保留、有限発声のloop誤認、HOOK遅延・同一本文異durationとの品質関係を区別して記録する。forced alignment coverage、VAD、recurrence等のobserver単体値を製品精度の合否へ置換しない。早送り・保留・誤帰属・次Hook未取得等を品質値として記録しても、それ自体ではrunを停止しない。AutoAdvが自力で後続ケースへ到達できない場合、検証driverは現在ケースの結果を保持したまま`validation_recovery_input`を記録して対象ゲームへ回復入力を一度送り、後続ケースのデータ収集を継続する。この入力をAutoAdvの成功や次Hook成立へ算入しない。回復後の次HOOKは通常の作品別同文規則と外部進行処理で収集し、検証専用の製品状態変更APIは使用しない。trace/oracle破損、fixture・実音声経路の未成立、要求ケースの収集未完了等の計測不能だけをrunner異常とする。Issue更新は成立済みローカル計測を共有する付随処理であり、常時共有するのはsummary、offline report、fixture evidenceと、全Presentationを覆う解析用product evidenceとする。共有product evidenceはLuna上流raw出力、選択Hook事実、Utterance、外部入力・Controller操作、統合音声判断のstate遷移、遷移時とPresentation終端時の音響観測、observerのPresentation集計、alignmentの状態変化・性能集計を保持し、同一状態の周期観測を送信単位にしない。送信量は観測polling頻度ではなくPresentation数・実際の状態変化・異常事実に主として比例させる。runner/launcher transcriptはその異常時だけ共有し、孤立WAVのobserver診断はdigestと要約を常時共有して、未共有digestまたは診断収集自体の異常時だけ詳細を載せる。送信量削減のために失敗ケース前後だけへ切り詰めず、完全な周期traceを含むrun証拠はローカルrun directoryに耐障害保存する。送信失敗だけで計測結果を失敗扱いしない。run終了時は一時checkoutと展開fixtureを削除し、完了済み旧runに残る検証専用venv・展開fixtureも証拠ファイルを残したまま回収し、計測証拠だけを保持する。Fast CI、Windows Adapter Validation、AutoAdv Live Advancement、その他の一括／Fullテストへ自動進行精度実機計測を追加しない。
- 実録音による音響observer・forced alignmentの品質／速度測定もGitHub Actionsの重量級スイートから外し、必要時にWindows実機で明示実行する。CIランナーの計測値を実機性能や自動進行精度実機計測の成立の代理にしない。
- 検証用PRは **マージ禁止**。`main`を更新する権限や承認を意味しない。headが同じリポジトリの`develop`、baseが`main`の場合だけ重量級jobを実行する。追加の開発branch・worktreeは作らない。
- PR上の検証はGitHubのマージ用refで行う。`main`が`develop`の祖先である間はツリーが`develop`と一致するが、両者が分岐した場合は`develop`単独の検証結果と同一視せず、入力commitを記録する。
- 通常のFast CIとPR重量級検証は別の合否。Hosted LiveのHOOK→文字判断→入力→次HOOK、実モデルの品質・速度測定、Windows実機の自動進行精度計測も相互に代用しない。
- Luna Release Asset Adoptionは採用操作であり、PR検証スイートへ混ぜない。Publish Gatewayの経路・権限は変更しない。
