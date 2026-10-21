# AutoAdv real-engine validation game.
# The voice() statements are ordinary Ren'Py playback and are NOT connected to AutoAdv.
# AutoAdv must observe the actual game through LunaHook and Windows Process Loopback.
define a = Character("明日香")
define s = Character("栞")
define p = Character("主人公")

# The test runner selects an existing scene before the first dialogue.
# This controls the game fixture only; it never sends an advance or supplies
# audio/HOOK/decision evidence to AutoAdv.
init python:
    import os as _autoadv_fixture_os

label start:
    scene black
    # Optional fixture readiness barrier. Only the first unmeasured calibration
    # output is produced here. No test code advances any measured dialogue.
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_CONTROL_FILE"):
        $ _adv_gate = _autoadv_fixture_os.environ["AUTOADV_VALIDATION_CONTROL_FILE"]
        while not _autoadv_fixture_os.path.isfile(_adv_gate + ".attached"):
            $ renpy.pause(.1, hard=True)
        $ renpy.say(None, "AutoAdvフック校正専用", interact=False)
        while not _autoadv_fixture_os.path.isfile(_adv_gate + ".monitoring"):
            $ renpy.pause(.1, hard=True)
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_SCENARIO") == "stress":
        jump asr_stress
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_SCENARIO") == "native":
        # Start with existing plain foreground voice, then reach BGM by the
        # same Controller input. This isolates audio capture from mixing.
        jump standard_voice
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_SCENARIO") == "tts":
        jump standard_tts
    "AutoAdv検証用ゲームです。使用音声は、あみたろの声素材工房（https://amitaro.net/）によるものです。"
    "通常のゲームと同じように、台詞表示・音声再生・選択肢・送り入力が動作します。"
    "無音声の主人公は、AutoAdv側でIrodori TTSを指定して検証してください。"
label standard_tts:
    p "これは無音声の台詞です。"
    p "この文章はIrodori TTSの確認に使用します。"

label standard_voice:
    voice "audio/voice/normal_001.wav"
    a "えっ嘘でしょ。"
    voice "audio/voice/normal_017.wav"
    a "あっベルが鳴ってる。"
    voice "audio/voice/normal_022.wav"
    a "猫はにゃーにゃーと鳴く。"

    "ここからはBGMが流れる状態で、ゲーム内の原音を再生します。"
label bgm_voice:
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

    # Normal AutoAdv progression ends here in the test-run scene. The original
    # player-facing menu remains unchanged when no test scene is selected.
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_SCENARIO") == "mixed":
        "通常会話の検証を終了します。"
        return

    "次の選択肢は、ユーザー操作によって進めてください。"
    menu:
        "ASR非言語発声・背景音声テスト":
            jump asr_stress
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


# Separate, optional real-engine challenge branch. No in-game AutoAdv control
# channel is created; normal Ren'Py voice/sound/dialogue/input is used.
label asr_stress:
    if not renpy.loadable("audio/voice/stress_cry.wav") or not renpy.loadable("audio/voice/stress_scream.wav"):
        "CC0実録音の泣き声・叫び声が未配置です。ASR特殊音声の検収は実行できません。"
        return

    "ASR特殊音声検証です。背景発話と非言語音声の進行判断を確認します。"
    voice "audio/voice/normal_001.wav"
    a "えっ嘘でしょ。"

    # One short utterance repeats as an independent background channel
    # across multiple dialogue/advance boundaries. AutoAdv observes only
    # the same-PID mix, never a privileged Ren'Py voice-source signal.
    play sound "audio/voice/normal_020.wav" loop
    voice "audio/voice/normal_001.wav"
    a "えっ嘘でしょ。"
    # Leave the background loop running after the foreground has ended.
    voice "audio/voice/normal_001.wav"
    a "えっ嘘でしょ。"

    # Background voice persists with no foreground voice at all.
    p "これは表示されただけの文章です。"
    # Identical background speech is acoustically indistinguishable from
    # a foreground occurrence of this very line using mixed PCM + ASR.
    p "ヒューズが飛んだ。"
    stop sound

    voice "audio/voice/stress_cry.wav"
    a "うっ……うう……"
    voice "audio/voice/stress_scream.wav"
    s "あああっ！"
    play sound "audio/voice/normal_020.wav" loop
    voice "audio/voice/stress_cry.wav"
    a "うっ……うう……"
    stop sound
    "特殊音声検証を終了します。"
    return
