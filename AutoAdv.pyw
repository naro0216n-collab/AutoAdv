"""Windows double-click GUI entrypoint. Uses the same application as python -m autoadv."""
from pathlib import Path
import ctypes
import os
import sys
import traceback


def main():
    os.chdir(Path(__file__).resolve().parent)
    from autoadv.__main__ import main as run_application
    return run_application()


if __name__ == '__main__':
    try:
        result = main()
        if result:
            raise RuntimeError('AutoAdv GUIの起動に失敗しました。Python 3.11とPySide6のインストールを確認してください。')
        raise SystemExit(0)
    except Exception:
        reason = traceback.format_exc()
        try:
            folder = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AutoAdv'
            folder.mkdir(parents=True, exist_ok=True)
            log = folder / 'startup-error.log'
            log.write_text(reason, encoding='utf-8')
            message = f'AutoAdvを起動できませんでした。\n{reason[-1500:]}\n\n詳細: {log}'
            ctypes.windll.user32.MessageBoxW(None, message, 'AutoAdv 起動エラー', 0x10)
        except Exception:
            print(reason, file=sys.stderr)
        raise SystemExit(1)
