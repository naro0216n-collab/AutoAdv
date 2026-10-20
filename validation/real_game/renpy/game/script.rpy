# AutoAdv real-engine validation game.
# The voice() statements are ordinary Ren'Py playback and are NOT connected to AutoAdv.
# AutoAdv must observe the actual game through LunaHook and Windows Process Loopback.
define a = Character("明日香")
define s = Character("栞")
define p = Character("主人公")

label start:
    scene black
    "AutoAdv検証用ゲームです。使用音声は、あみたろの声素材工房（https://amitaro.net/）によるものです。"
    "通常のゲームと同じように、台詞表示・音声再生・選択肢・送り入力が動作します。"
    "無音声の主人公は、AutoAdv側でIrodori TTSを指定して検証してください。"
    p "これは無音声の台詞です。"
    p "この文章はIrodori TTSの確認に使用します。"

    voice "audio/voice/normal_001.wav"
    a "えっ嘘でしょ。"
    voice "audio/voice/normal_017.wav"
    a "あっベルが鳴ってる。"
    voice "audio/voice/normal_022.wav"
    a "猫はにゃーにゃーと鳴く。"

    "ここからはBGMが流れる状態で、ゲーム内の原音を再生します。"
    play music "audio/music/original_synth_loop.wav" fadein 0.2
    voice "audio/voice/normal_026.wav"
    a "私はいつもミネラルウォーターを持ち歩いています。"
    voice "audio/voice/normal_028.wav"
    a "彼女はスタッフをまとめていけると思いますか？"
    stop music fadeout 0.2

    "同一の表示台詞を、異なる音声収録条件で連続して再生します。"
    voice "audio/voice/normal_001.wav"
    a "えっ嘘でしょ。"
    voice "audio/voice/whisper_001.wav"
    s "えっ嘘でしょ。"
    voice "audio/voice/whisper_017.wav"
    s "あっベルが鳴ってる。"
    voice "audio/voice/whisper_022.wav"
    s "猫はにゃーにゃーと鳴く。"

    "同じ取得本文が連続する区間です。二重送りと台詞取りこぼしに注意してください。"
    voice "audio/voice/normal_020.wav"
    a "ヒューズが飛んだ。"
    voice "audio/voice/normal_020.wav"
    a "ヒューズが飛んだ。"

    "次の選択肢は、ユーザー操作によって進めてください。"
    menu:
        "検証を続ける":
            jump continuation
        "検証を終了する":
            return

label continuation:
    voice "audio/voice/normal_010.wav"
    a "彼のあだ名は言い得て妙だよね。"
    voice "audio/voice/normal_012.wav"
    a "ゼロという概念は、ヒンドゥー文化に由来している。"
    voice "audio/voice/normal_021.wav"
    a "私はポピュラー音楽を聞きたい。"
    voice "audio/voice/normal_030.wav"
    a "あなたは流感になりかけているか、もっと重い病気かもしれません。"
    voice "audio/voice/normal_035.wav"
    a "彼女は息子に家で行儀よくするように言った。"
    "検証用シナリオを終了します。"
    return
