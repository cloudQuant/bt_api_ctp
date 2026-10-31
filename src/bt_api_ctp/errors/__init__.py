from __future__ import annotations

from bt_api_ctp.errors.ctp_translator import CTPErrorTranslator


class CtpOrderSubmissionOutcomeUnknown(RuntimeError):
    """A native order call returned a value outside its declared integer contract.

    The request may already have reached CTP.  Callers must preserve the order
    identity and reconcile it; this exception is never a definite rejection.
    """

    code = "ctp_order_submission_outcome_unknown"
    execution_unknown = True
    definite_reject = False

    def __init__(
        self,
        *,
        client_order_id: str,
        request_id: int,
        native_return_type: str,
    ) -> None:
        if type(client_order_id) is not str or not client_order_id:
            raise ValueError("unknown CTP submission requires a client order id")
        if type(request_id) is not int or request_id <= 0:
            raise ValueError("unknown CTP submission requires a positive request id")
        if (
            type(native_return_type) is not str
            or not native_return_type
            or len(native_return_type) > 64
            or not native_return_type.isascii()
            or not all(character.isalnum() or character == "_" for character in native_return_type)
        ):
            raise ValueError("unknown CTP submission requires a bounded return type name")
        self.client_order_id = client_order_id
        self.request_id = request_id
        self.native_return_type = native_return_type
        super().__init__(self.code)


__all__ = ["CTPErrorTranslator", "CtpOrderSubmissionOutcomeUnknown"]
