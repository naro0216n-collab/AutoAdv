# Observation *inside the validation game*, never imported by AutoAdv.
# AUTOADV_GAME_ORACLE must be set BEFORE launching the actual Ren'Py process.
# This records independent game/source evidence only; none of it is exposed to
# AutoAdv or used by the product decision path.
init -1 python:
    import array as _adv_array
    import hashlib as _adv_hashlib
    import json as _adv_json
    import math as _adv_math
    import os as _adv_os
    import sys as _adv_sys
    import time as _adv_time
    import unicodedata as _adv_unicode
    import wave as _adv_wave

    _adv_oracle_path = _adv_os.environ.get("AUTOADV_GAME_ORACLE", "")
    _adv_oracle_line = 0
    _adv_oracle_current = None
    _adv_oracle_voice = None
    _adv_oracle_voice_line = None
    _adv_oracle_last_channel_active = None
    _adv_oracle_preline_voice = None
    _adv_oracle_source_cache = {}
    _adv_oracle_mix_channels = ("music", "adv_ambient", "adv_background", "adv_sfx")
    _adv_oracle_mix = {}

    # Test-only clean-source endpoint definition.  It intentionally operates on
    # the isolated foreground WAV, never on Process Loopback mix PCM.  The 10 ms
    # RMS frame and -54 dBFS floor (0.002 full scale) match the independently
    # reviewed fixture endpoint annotations while remaining one uniform rule for
    # ordinary, whispered and non-lexical WAV sources.  This is an evaluation
    # reference, not a product VAD or a claim about physical speaker audibility.
    _ADV_SOURCE_FRAME_MS = 10.0
    _ADV_SOURCE_RMS_FLOOR = 0.002

    def _adv_oracle_write(event, at_ns=None, **fields):
        if not _adv_oracle_path:
            return
        if at_ns is None:
            at_ns = _adv_time.monotonic_ns()
        item = dict(event=event, at_ns=at_ns, pid=_adv_os.getpid(), **fields)
        with open(_adv_oracle_path, "a", encoding="utf-8") as stream:
            stream.write(_adv_json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    if _adv_oracle_path:
        # Refuse to combine separate executions, including game reloads.
        with open(_adv_oracle_path, "x", encoding="utf-8") as _adv_f:
            _adv_f.write(_adv_json.dumps(dict(
                event="oracle_started", version=2, pid=_adv_os.getpid(),
                endpoint_contract="clean_source_pcm_rms_v1",
                at_ns=_adv_time.monotonic_ns())) + "\n")

    def _adv_oracle_playing():
        # get_playing() can report a queued filename even before the backend
        # starts it. A nonnegative audio-engine position is required here;
        # physical speaker/headphone audibility is deliberately outside the contract.
        filename = renpy.music.get_playing(channel="voice")
        position = renpy.music.get_pos(channel="voice")
        if not filename or not isinstance(position, (float, int)) or position < 0:
            return None, None
        return filename, float(position)

    def _adv_oracle_source_timing(filename):
        cached = _adv_oracle_source_cache.get(filename)
        if cached is not None:
            return cached
        result = None
        try:
            source = renpy.file(filename)
            wav = _adv_wave.open(source, "rb")
            try:
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                sample_rate = wav.getframerate()
                total_frames = wav.getnframes()
                compression = wav.getcomptype()
                if (channels <= 0 or sample_width != 2 or sample_rate <= 0 or
                        total_frames <= 0 or compression != "NONE"):
                    raise ValueError("unsupported foreground WAV format")
                frames_per_window = max(1, int(round(sample_rate * _ADV_SOURCE_FRAME_MS / 1000.0)))
                threshold = _ADV_SOURCE_RMS_FLOOR * 32768.0
                threshold_sq = threshold * threshold
                cursor = 0
                last_active_frame = 0
                active_found = False
                while cursor < total_frames:
                    raw = wav.readframes(min(frames_per_window, total_frames - cursor))
                    if not raw:
                        break
                    samples = _adv_array.array("h")
                    samples.frombytes(raw)
                    if _adv_sys.byteorder != "little":
                        samples.byteswap()
                    if not samples:
                        break
                    mean_square = sum(float(value) * float(value) for value in samples) / len(samples)
                    frame_count = len(samples) // channels
                    if mean_square >= threshold_sq:
                        active_found = True
                        last_active_frame = cursor + frame_count
                    cursor += frame_count
                duration_ms = total_frames * 1000.0 / sample_rate
                if not active_found:
                    raise ValueError("foreground WAV has no acoustic activity above oracle floor")
                acoustic_end_ms = last_active_frame * 1000.0 / sample_rate
                result = dict(
                    source_duration_ms=round(duration_ms, 3),
                    source_acoustic_end_ms=round(acoustic_end_ms, 3),
                    source_acoustic_uncertainty_ms=_ADV_SOURCE_FRAME_MS,
                    source_endpoint_basis="clean_source_pcm_rms_10ms_0.002fs",
                )
            finally:
                wav.close()
        except Exception as exc:
            result = dict(source_endpoint_error=str(exc))
        _adv_oracle_source_cache[filename] = result
        return result

    def _adv_oracle_voice_started(filename, position):
        global _adv_oracle_voice, _adv_oracle_voice_line, _adv_oracle_last_channel_active
        _adv_oracle_voice = filename
        _adv_oracle_voice_line = _adv_oracle_current
        timing = _adv_oracle_source_timing(filename)
        observed_at = _adv_time.monotonic_ns()
        fields = dict(line=_adv_oracle_voice_line, file=filename,
                      channel_position_ms=round(position * 1000.0, 3),
                      source_started_at_ns=observed_at - int(position * 1000000000.0))
        fields.update(timing)
        endpoint_ms = timing.get("source_acoustic_end_ms")
        duration_ms = timing.get("source_duration_ms")
        if isinstance(endpoint_ms, (float, int)):
            fields["source_acoustic_end_at_ns"] = observed_at + int(
                (endpoint_ms - position * 1000.0) * 1000000.0)
        if isinstance(duration_ms, (float, int)):
            fields["source_file_end_at_ns"] = observed_at + int(
                (duration_ms - position * 1000.0) * 1000000.0)
        _adv_oracle_write("voice_started", at_ns=observed_at, **fields)
        _adv_oracle_last_channel_active = observed_at


    def _adv_oracle_observe_preline_voice(filename, position):
        """Remember voice-channel playback that precedes the next dialogue HOOK.

        Delayed-HOOK fixtures intentionally start ordinary game voice before the
        Character statement.  This state is evaluation-only and is claimed by
        the next real dialogue; it is never visible to AutoAdv.
        """
        global _adv_oracle_preline_voice
        observed_at = _adv_time.monotonic_ns()
        if (_adv_oracle_preline_voice is not None and
                _adv_oracle_preline_voice.get("file") == filename):
            _adv_oracle_preline_voice["last_observed_at_ns"] = observed_at
            return
        timing = dict(_adv_oracle_source_timing(filename))
        fields = dict(file=filename,
                      channel_position_ms=round(position * 1000.0, 3),
                      source_started_at_ns=observed_at - int(position * 1000000000.0),
                      last_observed_at_ns=observed_at)
        fields.update(timing)
        endpoint_ms = timing.get("source_acoustic_end_ms")
        duration_ms = timing.get("source_duration_ms")
        if isinstance(endpoint_ms, (float, int)):
            fields["source_acoustic_end_at_ns"] = fields["source_started_at_ns"] + int(endpoint_ms * 1000000.0)
        if isinstance(duration_ms, (float, int)):
            fields["source_file_end_at_ns"] = fields["source_started_at_ns"] + int(duration_ms * 1000000.0)
        _adv_oracle_preline_voice = fields

    def _adv_oracle_claim_preline_voice():
        global _adv_oracle_preline_voice, _adv_oracle_voice, _adv_oracle_voice_line, _adv_oracle_last_channel_active
        if _adv_oracle_preline_voice is None or _adv_oracle_current is None:
            return
        fields = dict(_adv_oracle_preline_voice)
        last_observed = fields.pop("last_observed_at_ns", None)
        fields["line"] = _adv_oracle_current
        _adv_oracle_write("voice_started", at_ns=_adv_time.monotonic_ns(), **fields)
        _adv_oracle_voice = fields["file"]
        _adv_oracle_voice_line = _adv_oracle_current
        _adv_oracle_last_channel_active = last_observed or fields["source_started_at_ns"]
        _adv_oracle_preline_voice = None

    def _adv_oracle_mix_playing(channel):
        try:
            filename = renpy.music.get_playing(channel=channel)
            position = renpy.music.get_pos(channel=channel)
        except Exception:
            return None
        if not filename or not isinstance(position, (float, int)) or position < 0:
            return None
        return filename

    def _adv_oracle_sample_mix():
        if not _adv_oracle_path:
            return
        for channel in _adv_oracle_mix_channels:
            playing = _adv_oracle_mix_playing(channel)
            previous = _adv_oracle_mix.get(channel)
            if playing == previous:
                continue
            if previous is not None:
                _adv_oracle_write("mix_stopped", line=_adv_oracle_current,
                                  channel=channel, file=previous)
                _adv_oracle_mix.pop(channel, None)
            if playing is not None:
                _adv_oracle_mix[channel] = playing
                _adv_oracle_write("mix_started", line=_adv_oracle_current,
                                  channel=channel, file=playing)

    def _adv_oracle_mix_snapshot():
        _adv_oracle_sample_mix()
        if _adv_oracle_current is not None:
            _adv_oracle_write("mix_snapshot", line=_adv_oracle_current,
                              channels=dict(_adv_oracle_mix))

    def _adv_oracle_sample():
        global _adv_oracle_voice, _adv_oracle_voice_line, _adv_oracle_last_channel_active
        if not _adv_oracle_path:
            return
        playing, position = _adv_oracle_playing()
        if playing:
            if _adv_oracle_voice is None:
                if _adv_oracle_current is None:
                    _adv_oracle_observe_preline_voice(playing, position)
                else:
                    _adv_oracle_voice_started(playing, position)
            elif playing != _adv_oracle_voice:
                _adv_oracle_write("voice_interrupted", line=_adv_oracle_voice_line,
                                  file=_adv_oracle_voice, replacement=playing,
                                  last_channel_active_at_ns=_adv_oracle_last_channel_active)
                _adv_oracle_voice = None
                _adv_oracle_voice_line = None
                _adv_oracle_last_channel_active = None
                if _adv_oracle_current is None:
                    _adv_oracle_observe_preline_voice(playing, position)
                else:
                    _adv_oracle_voice_started(playing, position)
            else:
                _adv_oracle_last_channel_active = _adv_time.monotonic_ns()
        elif _adv_oracle_voice is not None:
            # Channel inactivity is retained only as playback-system diagnostics.
            # It is not the source acoustic endpoint and may itself have been
            # caused by AutoAdv's advance input.
            _adv_oracle_write("voice_channel_inactive", line=_adv_oracle_voice_line,
                              file=_adv_oracle_voice,
                              last_channel_active_at_ns=_adv_oracle_last_channel_active)
            _adv_oracle_voice = None
            _adv_oracle_voice_line = None
            _adv_oracle_last_channel_active = None
        _adv_oracle_sample_mix()

    def _adv_oracle_character(event, **kwargs):
        global _adv_oracle_line, _adv_oracle_current
        if not _adv_oracle_path:
            return
        if event == "begin":
            _adv_oracle_line += 1
            _adv_oracle_current = _adv_oracle_line
            what = kwargs.get("what", "")
            normalized = _adv_unicode.normalize("NFKC", what).strip()
            _adv_oracle_write("line_started", line=_adv_oracle_current,
                body_hash=_adv_hashlib.sha256(normalized.encode("utf-8")).hexdigest())
            _adv_oracle_claim_preline_voice()
            _adv_oracle_sample()
            _adv_oracle_mix_snapshot()
        elif event == "end" and _adv_oracle_current is not None:
            # End only closes attribution of *new* playback to this dialogue.
            # The already-owned channel lifecycle remains observable; no
            # dialogue-dismissal event is used as an advancement contract.
            _adv_oracle_sample()
            _adv_oracle_current = None

    if _adv_oracle_path:
        config.all_character_callbacks.append(_adv_oracle_character)
        config.periodic_callbacks.append(_adv_oracle_sample)
