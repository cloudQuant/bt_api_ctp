"""Module-level docstring."""

from __future__ import annotations

from bt_api_base.error import ErrorTranslator


class CTPErrorTranslator(ErrorTranslator):
    """Class CTPErrorTranslator"""

    ERROR_MAP = {
        0: (None, 'Success'),
    }
