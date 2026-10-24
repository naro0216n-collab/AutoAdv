# Validation fixture evidence only.
#
# This file does not inspect Ren'Py playback state, infer dialogue completion,
# or classify AutoAdv behavior.  It records only fixture stimuli authored by the
# validation scenario so the external run can later be reconstructed.
init -1 python:
    import json as _adv_json
    import os as _adv_os
    import time as _adv_time

    _adv_evidence_path = _adv_os.environ.get("AUTOADV_GAME_EVIDENCE", "")
    _adv_evidence_seq = 0
    _adv_evidence_current_case = None
    _adv_evidence_current_voice = None

    def _adv_evidence_write(event, **fields):
        global _adv_evidence_seq
        if not _adv_evidence_path:
            return
        _adv_evidence_seq += 1
        item = dict(
            event=event,
            seq=_adv_evidence_seq,
            at_ns=_adv_time.monotonic_ns(),
            pid=_adv_os.getpid(),
            **fields
        )
        with open(_adv_evidence_path, "a", encoding="utf-8") as stream:
            stream.write(_adv_json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    if _adv_evidence_path:
        with open(_adv_evidence_path, "x", encoding="utf-8") as _adv_stream:
            _adv_stream.write(_adv_json.dumps(dict(
                event="fixture_started",
                version=1,
                seq=0,
                at_ns=_adv_time.monotonic_ns(),
                pid=_adv_os.getpid(),
            ), ensure_ascii=False, separators=(",", ":")) + "\n")

    def _adv_evidence_case(case_id, text, voice_file=None):
        global _adv_evidence_current_case, _adv_evidence_current_voice
        _adv_evidence_current_case = case_id
        _adv_evidence_current_voice = voice_file
        _adv_evidence_write(
            "fixture_case",
            case=case_id,
            text=text,
            voice_file=voice_file,
        )

    def _adv_evidence_voice_commanded():
        # Called immediately after the fixture's ordinary ``voice``/``play voice``
        # statement.  This records only what Ren'Py itself can report at that
        # point: which authored case issued the command and the source duration.
        # It does not inspect product state or infer whether playback has ended.
        if not _adv_evidence_current_case or not _adv_evidence_current_voice:
            return
        duration = renpy.music.get_duration(channel="voice")
        _adv_evidence_write(
            "fixture_voice_commanded",
            case=_adv_evidence_current_case,
            voice_file=_adv_evidence_current_voice,
            duration_ms=(round(float(duration) * 1000.0, 3) if duration else None),
        )
