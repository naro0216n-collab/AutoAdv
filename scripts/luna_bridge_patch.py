#!/usr/bin/env python3
"""Optional, reversible patch at LunaTranslator.textgetmethod_1 before filtering.

No game-process hook injection, no OCR engine replacement, no default enable.
The patched Luna still owns text extraction. This bridge emits only raw
candidate events and has no right to advance a scene.
"""
from __future__ import annotations
import argparse
import pathlib
import shutil

START = "        # AUTOADV_LUNA_BRIDGE_V1_BEGIN\n"
END = "        # AUTOADV_LUNA_BRIDGE_V1_END\n"
BODY = (START +
        "        if is_auto_run and not isRefresh:\n"
        "            try:\n"
        "                from autoadv_luna_bridge import emit as _autoadv_emit\n"
        "                _autoadv_emit(text, from_hook=isFromHook)\n"
        "            except Exception:\n"
        "                pass\n" + END)


def patch_text(source: str,*,undo: bool=False) -> str:
    if undo:
        if source.count(BODY)!=1:
            raise ValueError("installed patch does not match expected exact block")
        return source.replace(BODY,"",1)
    if START in source or END in source:
        raise ValueError("existing/unrecognized bridge modification")
    method=source.find("    def textgetmethod_1(")
    if method<0:
        raise ValueError("missing textgetmethod_1; unsupported Luna version")
    boundary=source.find("        if not text:\n",method)
    next_def=source.find("\n    def ",method+1)
    if boundary<0 or (next_def>=0 and boundary>next_def) or boundary-method>1500:
        raise ValueError("unexpected Luna pre-filter boundary")
    return source[:boundary]+BODY+source[boundary:]


def main() -> int:
    p=argparse.ArgumentParser(description="Opt-in LunaTranslator pre-filter candidate bridge")
    p.add_argument("luna_python_directory",type=pathlib.Path,
                   help="directory containing LunaTranslator.py inside Luna's Python source")
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install",action="store_true")
    mode.add_argument("--remove",action="store_true")
    args=p.parse_args()
    root=args.luna_python_directory.resolve()
    target=root/"LunaTranslator.py"
    bridge_dst=root/"autoadv_luna_bridge.py"
    source=target.read_text(encoding="utf-8")
    updated=patch_text(source,undo=args.remove)
    bridge_src=pathlib.Path(__file__).resolve().parent.parent/"integrations/luna/autoadv_luna_bridge.py"
    backup=root/"LunaTranslator.py.autoadv-backup"
    if args.install and (bridge_dst.exists() or backup.exists()):
        raise FileExistsError("refusing overwrite of bridge or prior backup")
    if args.install:
        shutil.copy2(target,backup)
        try:
            bridge_dst.write_bytes(bridge_src.read_bytes())
            target.write_text(updated,encoding="utf-8")
        except Exception:
            # Restore the original source; do not leave a partially patched Luna.
            target.write_text(source,encoding="utf-8")
            bridge_dst.unlink(missing_ok=True)
            raise
    else:
        target.write_text(updated,encoding="utf-8")
    if args.remove:
        # Never remove a file that the user or Luna itself may have modified.
        print(f"Patch removed; review whether to retain {bridge_dst}")
    print(f"Updated {target}")
    return 0


if __name__=="__main__":raise SystemExit(main())