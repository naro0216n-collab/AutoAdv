"""Normal AutoAdv application entrypoint."""
from __future__ import annotations


def main() -> int:
    try:
        from .gui import run_gui
    except ImportError as error:
        print(f'PySide6 is required to start AutoAdv: {error}')
        return 2
    return run_gui()


if __name__ == '__main__':
    raise SystemExit(main())
