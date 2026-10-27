# 権利・使用条件と検証範囲

音声素材：あみたろの声素材工房（https://amitaro.net/）
公式規約：https://amitaro.net/voice/voice_rule/
取得元（作者公式）：https://amitaro.net/voice/corpus-list/itazip/

- 通常音声：「あみたろITAコーパス読み上げ音声 Ver.2.2」
- ささやきA：「あみたろITAコーパス読み上げ音声・ささやきA Ver.1.0」
- 文章：ITAコーパス（パブリックドメイン）
  https://github.com/mmorise/ita-corpus
- 配布fixtureに同梱するBGM相当の音：この検証ゲーム向けに数値合成した単純な独自ループ

これは単体音声素材集ではなく、台詞・音声・送り・選択肢を結合した
Ren'Pyゲームのソースです。配布ZIP内の音声を切り離して素材集として
再公開・販売してはいけません。著作者の権利は保持されます。

作者の「Readme.txt」を含む日本語版利用規約を同梱しています。
公開／利用開始から約1ヶ月以内の事後報告は、公開主体による対応が必要です。
検証ゲームのソースおよびprebuilt fixtureをGitHub上で配布する場合もこの条件を引き継ぎます。
このリポジトリ内の記録は作者への事後報告完了を示すものではありません。公開主体が別途対応状況を管理してください。
年齢制限・政治／宗教活動・なりすまし等、公式規約の禁止用途に使わないでください。

明日香（通常録音）と栞（ささやき）は *同じ録音者* の異なる収録スタイルです。
別の実話者を使った互換性の検証にはなりません。

AutoAdvはゲームのscript.rpyや下記manifestを実行時には読まず、
LunaHookとWindows Process Loopbackで観測する必要があります。
manifestは検証結果の照合用です。

## Windows実機自動進行精度計測の外部メディア

自動進行精度計測のaccuracy専用sceneでは、既存fixtureへの過適合を避けるため、Gitへ同梱しない外部メディアも使用する。対象と取得先・SHA-256・runtime配置先は`ADVANCEMENT_ACCURACY_ASSETS.json`を正本とする。これらはWindows実機で自動進行精度計測を明示実行したときだけ個別取得してローカルcacheへ保存し、staged validation gameへ一時配置する。prebuilt fixtureやGitHub artifactへ再配布しない。

- JVS corpus: 公式ページが公開する`sample_jvs001` / `sample_jvs002`の個別WAVだけを使用する。3.5 GBの全corpus ZIPは取得しない。利用条件上、音声の再配布は行わない。
- OpenGameArt: `Lofi Hip Hop Loop` (omfgdude) と `Heavenly Loop` (isaiah658) の個別OGG。各素材ページでCC0とされているものをSHA-256固定で取得する。
- Meta Open Sound / Oculus Audio Pack 1: 個別OGGのmachine-room ambience、restaurant conversation、front-door open、garage-door closeをcommit SHAとファイルSHA-256で固定する。CC BY 4.0。Credit: "Oculus Audio Pack 01" by Oculus VR, LLC (https://developers.meta.com/vr/downloads/package/oculus-audio-pack-1/), licensed under CC BY 4.0. Files renamed to the UCS naming convention; OGG previews transcoded from the original WAVs.

外部素材のURLやライセンス情報をGitへ記録することと、素材データ自体を再配布することを区別する。cacheは検証専用で、AutoAdv製品の通常ゲーム資産・ユーザー設定・配布物にはしない。
