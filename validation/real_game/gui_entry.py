"""Validation-only launcher around the unmodified AutoAdv GUI entrypoint.

The normal product entrypoint never imports the validation driver.  This
launcher creates the ordinary Qt application, waits until its normal top-level
window exists, and then attaches the external validation driver.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    if not os.environ.get('AUTOADV_VALIDATION_SESSION'):
        raise RuntimeError('AUTOADV_VALIDATION_SESSION is required')
    if not os.environ.get('AUTOADV_VALIDATION_TRACE'):
        raise RuntimeError('automated validation requires a product trace')

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        print(f'PySide6 is required to start AutoAdv validation: {error}')
        return 2

    from autoadv.gui import run_gui
    from .gui_session import install

    app = QApplication.instance() or QApplication(sys.argv)

    def attach() -> None:
        window = next(
            (widget for widget in app.topLevelWidgets()
             if hasattr(widget, 'application') and hasattr(widget, 'validation_trace')),
            None,
        )
        if window is None:
            QTimer.singleShot(10, attach)
            return
        install(window, app)

    QTimer.singleShot(0, attach)
    return run_gui()


if __name__ == '__main__':
    raise SystemExit(main())
