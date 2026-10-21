"""Normal application entrypoint; no alternate dialogue execution pipeline."""
from __future__ import annotations
import os

def main() -> int:
    try:
        from .gui import run_gui
    except ImportError as error:
        print(f'PySide6 is required to start AutoAdv: {error}')
        return 2
    # Opt-in, external test orchestration uses the ordinary GUI methods.
    # It is never loaded during standard AutoAdv usage.
    driver = None
    if os.environ.get('AUTOADV_VALIDATION_SESSION'):
        if not os.environ.get('AUTOADV_VALIDATION_TRACE'):
            raise RuntimeError('Automated validation requires a real product trace')
        from validation.real_game.gui_session import install
        driver = install
    return run_gui(session_driver=driver)

if __name__ == '__main__':
    raise SystemExit(main())
