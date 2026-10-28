"""Normal AutoAdv application entrypoint."""
from __future__ import annotations


def main() -> int:
    from .crash_report import install_crash_reporting
    install_crash_reporting('gui')
    try:
        from .gui import run_gui
    except ImportError as error:
        print(f'PySide6 is required to start AutoAdv: {error}')
        return 2
    return run_gui()


if __name__ == '__main__':
    raise SystemExit(main())
