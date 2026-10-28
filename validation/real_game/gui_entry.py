"""Validation-only launcher around the unmodified AutoAdv GUI entrypoint."""
from __future__ import annotations

import os
import sys


def main() -> int:
    if not os.environ.get('AUTOADV_VALIDATION_SESSION'):
        raise RuntimeError('AUTOADV_VALIDATION_SESSION is required')
    trace_path = os.environ.get('AUTOADV_VALIDATION_TRACE')
    if not trace_path:
        raise RuntimeError('automated validation requires a validation trace path')

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        print(f'PySide6 is required to start AutoAdv validation: {error}')
        return 2

    from autoadv.gui import run_gui
    from .gui_session import install
    from .trace import ProductTraceObserver, ValidationTrace

    app = QApplication.instance() or QApplication(sys.argv)
    trace = ValidationTrace(trace_path)

    def attach() -> None:
        window = next(
            (widget for widget in app.topLevelWidgets() if hasattr(widget, 'application')),
            None,
        )
        if window is None:
            QTimer.singleShot(10, attach)
            return
        observer = ProductTraceObserver(trace, window.application)
        observer.install()
        install(window, app, trace)

    QTimer.singleShot(0, attach)
    return run_gui()


if __name__ == '__main__':
    raise SystemExit(main())
