from __future__ import annotations

import ctypes
import logging
import os
from contextlib import contextmanager
from collections.abc import Iterator


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def prevent_system_sleep(enabled: bool) -> Iterator[bool]:
    """Evita temporalmente la suspensión de Windows durante la captura.

    No cambia el plan de energía de forma permanente. Al salir del proceso se
    restaura el comportamiento normal del sistema.
    """
    active = False
    logger = logging.getLogger("system-guard")
    if enabled and os.name == "nt":
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        active = bool(result)
        if active:
            logger.info("Suspensión automática bloqueada temporalmente.")
        else:
            logger.warning(
                "Windows no permitió bloquear la suspensión automática."
            )
    elif enabled:
        logger.info(
            "La prevención de suspensión solo se aplica automáticamente en Windows."
        )
    try:
        yield active
    finally:
        if active:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            logger.info("Comportamiento de suspensión restaurado.")
