# AutoAdv real-engine validation game.
# The voice() statements are ordinary Ren'Py playback and are NOT connected to AutoAdv.
# AutoAdv must observe the actual game through LunaHook and Windows Process Loopback.
define a = Character("明日香")
define s = Character("栞")
define p = Character("主人公")
define b = Character("千夏")
define c = Character("蓮")

# The test runner selects an existing scene before the first dialogue.
# This controls the game fixture only; it never sends an advance or supplies
# audio/HOOK/decision evidence to AutoAdv.
init python:
    import os as _autoadv_fixture_os

# Independent game presentation channels used by the continuous accuracy scene.
# They are ordinary Ren'Py audio channels and are never visible to AutoAdv.
init -2 python:
    renpy.music.register_channel("adv_ambient", "sfx", loop=True)
    renpy.music.register_channel("adv_background", "sfx", loop=True)
    renpy.music.register_channel("adv_sfx", "sfx", loop=False)

image bg adv_room = Solid("#141922")
image bg adv_cafe = Solid("#302525")
image bg adv_street = Solid("#151b28")
image bg adv_incident = Solid("#271518")
image bg adv_machine = Solid("#17201d")
image bg adv_quiet = Solid("#101418")

screen adv_delayed_sfx(delay, filename, volume=1.0):
    timer delay action Play("adv_sfx", filename, relative_volume=volume)

label start:
    scene black
    # Optional fixture readiness barrier. Only the first unmeasured calibration
    # output is produced here. No test code advances any measured dialogue.
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_CONTROL_FILE"):
        $ _adv_gate = _autoadv_fixture_os.environ["AUTOADV_VALIDATION_CONTROL_FILE"]
        while not _autoadv_fixture_os.path.isfile(_adv_gate + ".attached"):
            $ renpy.pause(.1, hard=True)
        $ renpy.say(a, "AutoAdvフック校正専用", interact=False)
        while not _autoadv_fixture_os.path.isfile(_adv_gate + ".monitoring"):
            $ renpy.pause(.1, hard=True)
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_SCENARIO") == "stress":
        jump alignment_stress
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_SCENARIO") == "accuracy":
        # Dedicated Windows auto-advance accuracy scene. Runtime-only media is hydrated by
        # run_windows_validation.ps1 before the game starts; it is never part
        # of the prebuilt fixture or an AutoAdv input.
        jump advancement_accuracy
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_SCENARIO") == "tts":
        jump standard_tts
    "AutoAdv検証用ゲームです。使用音声は、あみたろの声素材工房（https://amitaro.net/）によるものです。"
    "通常のゲームと同じように、台詞表示・音声再生・選択肢・送り入力が動作します。"
    "無音声の主人公は、AutoAdv側でIrodori TTSを指定して検証してください。"
label advancement_accuracy:
    # One continuous ADV/novel-game playthrough. The game process, selected
    # LunaHook, AutoAdv session, NativeAudio and alignment remain alive from the first
    # measured line through the final recovery line. Scene transitions below
    # are ordinary presentation only; no validation boundary or product reset
    # is signalled to AutoAdv.
    python:
        _adv_runtime_assets = [
            "audio/runtime/voice/jvs001_VOICEACTRESS100_001.wav",
            "audio/runtime/voice/jvs002_VOICEACTRESS100_001.wav",
            "audio/runtime/voice/jvs003_VOICEACTRESS100_001.wav",
            "audio/runtime/music/Heavenly Loop_0.ogg",
            "audio/runtime/music/LofiLoop_2.ogg",
            "audio/runtime/ambient/AMBInd_RoomToneMachineRoom_LOOP_01.ogg",
            "audio/runtime/ambient/CRWDConv_Restaurant_LOOP_01.ogg",
            "audio/runtime/sfx/DOORWood_FrontDoorOpen_OS_01.ogg",
            "audio/runtime/sfx/DOORMetl_GarageDoorClose_OS_01.ogg",
        ]
        _adv_missing_runtime_assets = [x for x in _adv_runtime_assets if not renpy.loadable(x)]
    if _adv_missing_runtime_assets:
        "自動進行精度計測用素材が不足しています。検証runnerから起動してください。"
        return

    # Scene 1: quiet room. Establish clean voiced and unvoiced behavior before
    # any persistent mix exists.
    scene bg adv_room with fade
    $ _adv_evidence_case("room-clean-short", "えっ嘘でしょ。", "audio/voice/normal_001.wav")
    voice "audio/voice/normal_001.wav"
    $ _adv_evidence_voice_commanded()
    a "えっ嘘でしょ。"
    $ _adv_evidence_case("room-clean-jvs001", "また、東寺のように、五大明王と呼ばれる、主要な明王の中央に配されることも多い。", "audio/runtime/voice/jvs001_VOICEACTRESS100_001.wav")
    voice "audio/runtime/voice/jvs001_VOICEACTRESS100_001.wav"
    $ _adv_evidence_voice_commanded()
    a "また、東寺のように、五大明王と呼ばれる、主要な明王の中央に配されることも多い。"
    $ _adv_evidence_case("room-clean-unvoiced", "これは音声のない台詞です。", None)
    p "これは音声のない台詞です。"

    # Delayed-HOOK equivalence classes. The ordinary voice channel starts before
    # the dialogue statement, so Luna receives the real HOOK only after audio
    # has already progressed. No product-side HOOK is injected.
    $ _adv_evidence_case("hook-delay-moderate", "猫はにゃーにゃーと鳴く。", "audio/voice/normal_022.wav")
    play voice "audio/voice/normal_022.wav"
    $ _adv_evidence_voice_commanded()
    $ renpy.pause(.55, hard=True)
    a "猫はにゃーにゃーと鳴く。"
    $ _adv_evidence_case("hook-delay-late", "あなたは流感になりかけているか、もっと重い病気かもしれません。", "audio/voice/normal_030.wav")
    play voice "audio/voice/normal_030.wav"
    $ _adv_evidence_voice_commanded()
    $ renpy.pause(4.10, hard=True)
    a "あなたは流感になりかけているか、もっと重い病気かもしれません。"
    $ _adv_evidence_case("hook-delay-after-end", "えっ嘘でしょ。", "audio/voice/normal_001.wav")
    play voice "audio/voice/normal_001.wav"
    $ _adv_evidence_voice_commanded()
    $ renpy.pause(2.10, hard=True)
    a "えっ嘘でしょ。"
    $ _adv_evidence_case("hook-delay-nonverbal", "うっ……うう……", "audio/voice/stress_cry.wav")
    play voice "audio/voice/stress_cry.wav"
    $ _adv_evidence_voice_commanded()
    $ renpy.pause(1.25, hard=True)
    a "うっ……うう……"

    # Text amount is deliberately decoupled from source duration. Derived WAVs
    # are deterministic fixture-only single sources built from the licensed
    # human-vocal clips before Ren'Py distribution.
    $ _adv_evidence_case("duration-same-text-long-tail", "えっ嘘でしょ。", "audio/voice/derived_same_text_long_tail.wav")
    voice "audio/voice/derived_same_text_long_tail.wav"
    $ _adv_evidence_voice_commanded()
    a "えっ嘘でしょ。"
    $ _adv_evidence_case("duration-long-text-fast", "あなたは流感になりかけているか、もっと重い病気かもしれません。", "audio/voice/derived_normal_030_fast.wav")
    voice "audio/voice/derived_normal_030_fast.wav"
    $ _adv_evidence_voice_commanded()
    a "あなたは流感になりかけているか、もっと重い病気かもしれません。"
    $ _adv_evidence_case("nonverbal-panting-temporal", "はっ……はっ……はっ……", "audio/voice/derived_panting_temporal.wav")
    voice "audio/voice/derived_panting_temporal.wav"
    $ _adv_evidence_voice_commanded()
    a "はっ……はっ……はっ……"
    $ _adv_evidence_case("nonverbal-breath-internal-silence", "……ふぅ……", "audio/voice/derived_breath_pause.wav")
    voice "audio/voice/derived_breath_pause.wav"
    $ _adv_evidence_voice_commanded()
    s "……ふぅ……"

    # Scene 2: the same conversation continues while BGM fades in and remains
    # across voiced, unvoiced and whispered lines.
    play music "audio/runtime/music/Heavenly Loop_0.ogg" loop fadein 0.8 volume 0.24
    $ _adv_evidence_case("room-bgm-fadein", "あっベルが鳴ってる。", "audio/voice/normal_017.wav")
    voice "audio/voice/normal_017.wav"
    $ _adv_evidence_voice_commanded()
    a "あっベルが鳴ってる。"
    $ _adv_evidence_case("room-bgm-unvoiced", "返事をする前に、少し考えた。", None)
    p "返事をする前に、少し考えた。"
    $ _adv_evidence_case("room-bgm-whisper", "えっ嘘でしょ。", "audio/voice/whisper_001.wav")
    voice "audio/voice/whisper_001.wav"
    $ _adv_evidence_voice_commanded()
    s "えっ嘘でしょ。"

    # Scene 3: enter a busy cafe without resetting the session. BGM continues;
    # a door one-shot and crowd ambience overlap the first line.
    scene bg adv_cafe with dissolve
    play adv_sfx "audio/runtime/sfx/DOORWood_FrontDoorOpen_OS_01.ogg" volume 0.34
    play adv_ambient "audio/runtime/ambient/CRWDConv_Restaurant_LOOP_01.ogg" loop fadein 0.4 volume 0.22
    $ _adv_evidence_case("cafe-entry-door", "猫はにゃーにゃーと鳴く。", "audio/voice/normal_022.wav")
    voice "audio/voice/normal_022.wav"
    $ _adv_evidence_voice_commanded()
    a "猫はにゃーにゃーと鳴く。"
    $ _adv_evidence_case("cafe-crowd-local", "彼女はスタッフをまとめていけると思いますか？", "audio/voice/normal_028.wav")
    voice "audio/voice/normal_028.wav"
    $ _adv_evidence_voice_commanded()
    a "彼女はスタッフをまとめていけると思いますか？"
    $ _adv_evidence_case("cafe-crowd-jvs002", "また、東寺のように、五大明王と呼ばれる、主要な明王の中央に配されることも多い。", "audio/runtime/voice/jvs002_VOICEACTRESS100_001.wav")
    voice "audio/runtime/voice/jvs002_VOICEACTRESS100_001.wav"
    $ _adv_evidence_voice_commanded()
    b "また、東寺のように、五大明王と呼ばれる、主要な明王の中央に配されることも多い。"
    $ _adv_evidence_case("cafe-crowd-unvoiced", "人の多い店内で、主人公は黙って頷いた。", None)
    p "人の多い店内で、主人公は黙って頷いた。"

    # Scene 4: leave the crowd and change BGM as a normal scene transition.
    # A Japanese voice then persists on an independent game channel, first
    # behind a voiced line and then behind an unvoiced protagonist line.
    stop adv_ambient fadeout 0.6
    scene bg adv_street with fade
    play music "audio/runtime/music/LofiLoop_2.ogg" loop fadeout 0.8 fadein 0.8 volume 0.28
    $ _adv_evidence_case("street-bgm-transition", "私はいつもミネラルウォーターを持ち歩いています。", "audio/voice/normal_026.wav")
    voice "audio/voice/normal_026.wav"
    $ _adv_evidence_voice_commanded()
    a "私はいつもミネラルウォーターを持ち歩いています。"
    play adv_background "audio/runtime/voice/jvs001_VOICEACTRESS100_001.wav" loop volume 0.18
    $ _adv_evidence_case("street-background-japanese", "彼のあだ名は言い得て妙だよね。", "audio/voice/normal_010.wav")
    voice "audio/voice/normal_010.wav"
    $ _adv_evidence_voice_commanded()
    a "彼のあだ名は言い得て妙だよね。"
    $ _adv_evidence_case("street-background-unvoiced", "この場面の主人公にも音声は付いていない。", None)
    p "この場面の主人公にも音声は付いていない。"
    stop adv_background fadeout 0.2

    # Identical successive foreground text remains inside the same BGM and
    # product session. No test-only utterance boundary is inserted.
    $ _adv_evidence_case("street-repeat-first", "ヒューズが飛んだ。", "audio/voice/normal_020.wav")
    voice "audio/voice/normal_020.wav"
    $ _adv_evidence_voice_commanded()
    a "ヒューズが飛んだ。"
    $ _adv_evidence_case("street-repeat-second", "ヒューズが飛んだ。", "audio/voice/normal_020.wav")
    voice "audio/voice/normal_020.wav"
    $ _adv_evidence_voice_commanded()
    a "ヒューズが飛んだ。"

    # Human-vocal recurrence classes. The loop starts after the first case's
    # epoch boundary, then persists across later AutoAdv boundaries. A finite
    # target overlaps it, an unvoiced line must not wait forever for it, and a
    # highly recurrent but finite foreground event follows after the loop stops.
    play adv_background "audio/voice/normal_020.wav" loop volume 0.18
    $ _adv_evidence_case("vocal-loop-started-in-epoch-target", "猫はにゃーにゃーと鳴く。", "audio/voice/normal_022.wav")
    voice "audio/voice/normal_022.wav"
    $ _adv_evidence_voice_commanded()
    a "猫はにゃーにゃーと鳴く。"
    $ _adv_evidence_case("vocal-loop-cross-epoch-target", "あっベルが鳴ってる。", "audio/voice/normal_017.wav")
    voice "audio/voice/normal_017.wav"
    $ _adv_evidence_voice_commanded()
    a "あっベルが鳴ってる。"
    $ _adv_evidence_case("vocal-loop-cross-epoch-unvoiced", "繰り返す声は背景のままで、主人公には音声がない。", None)
    p "繰り返す声は背景のままで、主人公には音声がない。"
    stop adv_background fadeout 0.1
    $ _adv_evidence_case("finite-vocal-natural-repetition", "ヒューズが飛んだ。ヒューズが飛んだ。ヒューズが飛んだ。", "audio/voice/derived_finite_repetition.wav")
    voice "audio/voice/derived_finite_repetition.wav"
    $ _adv_evidence_voice_commanded()
    a "ヒューズが飛んだ。ヒューズが飛んだ。ヒューズが飛んだ。"

    # Scene 5: emotional/incident sequence. Non-lexical voice is followed by a
    # long normal line. A long one-shot effect starts *during* that voice on a
    # separate channel and is intentionally allowed to remain into the next
    # unvoiced narration line.
    scene bg adv_incident with dissolve
    $ _adv_evidence_case("incident-nonlexical-cry", "うっ……うう……", "audio/voice/stress_cry.wav")
    voice "audio/voice/stress_cry.wav"
    $ _adv_evidence_voice_commanded()
    a "うっ……うう……"
    show screen adv_delayed_sfx(0.8, "audio/runtime/sfx/DOORMetl_GarageDoorClose_OS_01.ogg", 0.32)
    $ _adv_evidence_case("incident-midvoice-sfx", "あなたは流感になりかけているか、もっと重い病気かもしれません。", "audio/voice/normal_030.wav")
    voice "audio/voice/normal_030.wav"
    $ _adv_evidence_voice_commanded()
    a "あなたは流感になりかけているか、もっと重い病気かもしれません。"
    hide screen adv_delayed_sfx
    $ _adv_evidence_case("incident-sfx-tail-unvoiced", "大きな音の余韻だけが、その場に残った。", None)
    p "大きな音の余韻だけが、その場に残った。"
    stop adv_sfx
    $ _adv_evidence_case("incident-nonlexical-scream", "あああっ！", "audio/voice/stress_scream.wav")
    voice "audio/voice/stress_scream.wav"
    $ _adv_evidence_voice_commanded()
    s "あああっ！"

    # Scene 6: move into a machine room while the old BGM remains. The next
    # line therefore sees old BGM + new ambience; the following whisper occurs
    # while BGM fades out. Ambient noise then persists through unvoiced/voiced
    # lines without any session reset.
    scene bg adv_machine with fade
    play adv_ambient "audio/runtime/ambient/AMBInd_RoomToneMachineRoom_LOOP_01.ogg" loop fadein 0.4 volume 0.25
    $ _adv_evidence_case("machine-transition-jvs003", "また、東寺のように、五大明王と呼ばれる、主要な明王の中央に配されることも多い。", "audio/runtime/voice/jvs003_VOICEACTRESS100_001.wav")
    voice "audio/runtime/voice/jvs003_VOICEACTRESS100_001.wav"
    $ _adv_evidence_voice_commanded()
    c "また、東寺のように、五大明王と呼ばれる、主要な明王の中央に配されることも多い。"
    stop music fadeout 1.2
    $ _adv_evidence_case("machine-fading-bgm-whisper", "あっベルが鳴ってる。", "audio/voice/whisper_017.wav")
    voice "audio/voice/whisper_017.wav"
    $ _adv_evidence_voice_commanded()
    s "あっベルが鳴ってる。"
    $ _adv_evidence_case("machine-unvoiced", "機械音の中で、ナレーションだけが表示される。", None)
    p "機械音の中で、ナレーションだけが表示される。"
    $ _adv_evidence_case("machine-local", "私はポピュラー音楽を聞きたい。", "audio/voice/normal_021.wav")
    voice "audio/voice/normal_021.wav"
    $ _adv_evidence_voice_commanded()
    a "私はポピュラー音楽を聞きたい。"

    # Scene 7: ordinary quiet transition and final clean recovery. The same
    # process/session must prove no stale PCM/alignment/background state leaks.
    stop adv_ambient fadeout 0.3
    scene bg adv_quiet with fade
    $ _adv_evidence_case("quiet-recovery", "彼女は息子に家で行儀よくするように言った。", "audio/voice/normal_035.wav")
    voice "audio/voice/normal_035.wav"
    $ _adv_evidence_voice_commanded()
    a "彼女は息子に家で行儀よくするように言った。"
    return

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

    # Human-vocal recurrence classes. The loop starts after the first case's
    # epoch boundary, then persists across later AutoAdv boundaries. A finite
    # target overlaps it, an unvoiced line must not wait forever for it, and a
    # highly recurrent but finite foreground event follows after the loop stops.
    play adv_background "audio/voice/normal_020.wav" loop volume 0.18
    voice "audio/voice/normal_022.wav"
    a "猫はにゃーにゃーと鳴く。"
    voice "audio/voice/normal_017.wav"
    a "あっベルが鳴ってる。"
    p "繰り返す声は背景のままで、主人公には音声がない。"
    stop adv_background fadeout 0.1
    voice "audio/voice/derived_finite_repetition.wav"
    a "ヒューズが飛んだ。ヒューズが飛んだ。ヒューズが飛んだ。"

    # Normal AutoAdv progression ends here in the test-run scene. The original
    # player-facing menu remains unchanged when no test scene is selected.
    if _autoadv_fixture_os.environ.get("AUTOADV_VALIDATION_SCENARIO") == "mixed":
        "通常会話の検証を終了します。"
        return

    "次の選択肢は、ユーザー操作によって進めてください。"
    menu:
        "音響整列・非言語発声・背景音声テスト":
            jump alignment_stress
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
label alignment_stress:
    if not renpy.loadable("audio/voice/stress_cry.wav") or not renpy.loadable("audio/voice/stress_scream.wav"):
        "CC0実録音の泣き声・叫び声が未配置です。音響整列の特殊音声検収は実行できません。"
        return

    "音響整列の特殊音声検証です。背景発話と非言語音声の進行判断を確認します。"
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
    # a foreground occurrence of this very line using mixed PCM + target-conditioned alignment.
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
