from __future__ import annotations

from bt_api_base.error import ErrorTranslator, UnifiedError, UnifiedErrorCode


class CTPErrorTranslator(ErrorTranslator):
    ERROR_MAP = {
        0: (None, "Success"),
    }