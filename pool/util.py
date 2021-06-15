from chia.protocols.pool_protocol import PoolErrorCode, ErrorResponse
from chia.util.ints import uint16
from chia.util.json_util import obj_to_response


def error_response(code: PoolErrorCode, message: str):
    error: ErrorResponse = ErrorResponse(uint16(code.value), message)
    return obj_to_response(error)


def error_dict(code: PoolErrorCode, message: str):
    error: ErrorResponse = ErrorResponse(uint16(code.value), message)
    return error.to_json_dict()
