# AutoAdv 正準文書

- `integration-spec.md`：2026年10月20日付、LunaHook広域移植・統合後の完成時契約（ゲーム識別、HOOK Host、複数プロファイル、GUI、Irodori、音声同期、進行安全）。
- `development-principles.md`：開発、責務、検証の原則。完成時動作を優先する。
- `publish.md`：既存Publish Gateway、develop Fast CI source-snapshotからの復元・publish運用。

`docs/integration-spec.md`を完成条件の正本とし、現行コード、旧テスト、fixture、Luna公開WS運用を正準仕様へ読み替えない。Luna側公開WSへの依存は新仕様の通常経路には含まれない。`main`とversionの変更は明示承認後のみとする。
