# Observation *inside the validation game*, never imported by AutoAdv.
# AUTOADV_GAME_ORACLE must be set BEFORE launching the actual Ren'Py process.
# This records Ren'Py/Windows audio-system playback state, not acoustic audibility, mock PCM, HOOK notifications or input.
init -1 python:
    import hashlib as _adv_hashlib
    import json as _adv_json
    import os as _adv_os
    import time as _adv_time
    import unicodedata as _adv_unicode

    _adv_oracle_path = _adv_os.environ.get("AUTOADV_GAME_ORACLE", "")
    _adv_oracle_line = 0
    _adv_oracle_current = None
    _adv_oracle_voice = None
    _adv_oracle_voice_line = None
    _adv_oracle_last_active = None

    def _adv_oracle_write(event, **fields):
        if not _adv_oracle_path:
            return
        item = dict(event=event, at_ns=_adv_time.monotonic_ns(), pid=_adv_os.getpid(), **fields)
        with open(_adv_oracle_path, "a", encoding="utf-8") as stream:
            stream.write(_adv_json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    if _adv_oracle_path:
        # Refuse to combine separate executions, including game reloads.
        with open(_adv_oracle_path, "x", encoding="utf-8") as _adv_f:
            _adv_f.write(_adv_json.dumps(dict(event="oracle_started", version=1,
                pid=_adv_os.getpid(), at_ns=_adv_time.monotonic_ns())) + "\n")

    def _adv_oracle_playing():
        # get_playing() can report a queued filename even before the backend
        # starts it. A nonnegative audio-engine position is required here;
        # physical speaker/headphone audibility is deliberately outside the contract.
        filename = renpy.music.get_playing(channel="voice")
        position = renpy.music.get_pos(channel="voice")
        if not filename or not isinstance(position, (float, int)) or position < 0:
            return None
        return filename

    def _adv_oracle_sample():
        global _adv_oracle_voice, _adv_oracle_voice_line, _adv_oracle_last_active
        if not _adv_oracle_path:
            return
        playing = _adv_oracle_playing()
        if playing:
            if _adv_oracle_voice is None:
                # A new voice can only be attributed while a dialogue is
                # current.  Once started, its ownership survives line changes
                # until the audio engine reports an actual end/replacement.
                if _adv_oracle_current is None:
                    return
                _adv_oracle_voice = playing
                _adv_oracle_voice_line = _adv_oracle_current
                _adv_oracle_write("voice_started", line=_adv_oracle_voice_line, file=playing)
            elif playing != _adv_oracle_voice:
                _adv_oracle_write("voice_interrupted", line=_adv_oracle_voice_line,
                                  file=_adv_oracle_voice, replacement=playing)
                if _adv_oracle_current is None:
                    _adv_oracle_voice = None
                    _adv_oracle_voice_line = None
                    _adv_oracle_last_active = None
                    return
                _adv_oracle_voice = playing
                _adv_oracle_voice_line = _adv_oracle_current
                _adv_oracle_write("voice_started", line=_adv_oracle_voice_line, file=playing)
            _adv_oracle_last_active = _adv_time.monotonic_ns()
        elif _adv_oracle_voice is not None:
            _adv_oracle_write("voice_ended", line=_adv_oracle_voice_line,
                              file=_adv_oracle_voice,
                              last_active_at_ns=_adv_oracle_last_active)
            _adv_oracle_voice = None
            _adv_oracle_voice_line = None
            _adv_oracle_last_active = None

    def _adv_oracle_character(event, **kwargs):
        global _adv_oracle_line, _adv_oracle_current
        global _adv_oracle_voice, _adv_oracle_voice_line, _adv_oracle_last_active
        if not _adv_oracle_path:
            return
        if event == "begin":
            _adv_oracle_line += 1
            _adv_oracle_current = _adv_oracle_line
            what = kwargs.get("what", "")
            normalized = _adv_unicode.normalize("NFKC", what).strip()
            _adv_oracle_write("line_started", line=_adv_oracle_current,
                body_hash=_adv_hashlib.sha256(normalized.encode("utf-8")).hexdigest())
            _adv_oracle_sample()
        elif event == "end" and _adv_oracle_current is not None:
            # End only closes attribution of *new* playback to this dialogue.
            # The already-owned voice lifecycle remains observable until the
            # audio engine reports end/replacement; no dialogue-dismissal event
            # is recorded or used as an independent advancement contract.
            _adv_oracle_sample()
            _adv_oracle_current = None

    if _adv_oracle_path:
        config.all_character_callbacks.append(_adv_oracle_character)
        config.periodic_callbacks.append(_adv_oracle_sample)
