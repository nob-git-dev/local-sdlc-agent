"""
RESP（Redis Serialization Protocol）風プロトコルのパース・シリアライズ部品。

標準ライブラリのみを使用。
TCP 接続管理は含まない。
"""

from typing import Tuple, List, Optional


class ProtocolError(Exception):
    """RESP プロトコルのフォーマットエラー"""
    pass


def parse_resp(data: bytes) -> Tuple[Optional[List[str]], bytes]:
    """
    RESP 形式のバイト列から1コマンド分をパースする。

    Args:
        data: 受信したバイト列

    Returns:
        (コマンド引数リスト, 未処理の残データ)
        コマンド引数リストが None の場合、データ不足でパース不能。
        残データは次の呼び出しで再パースされる。

    Raises:
        ProtocolError: 不正なフォーマットの場合。
    """
    if not data:
        return None, b""

    # 先頭バイトでタイプを判定
    if data[0:1] == b"*":
        return _parse_array(data)
    elif data[0:1] == b"$":
        return _parse_bulk_string(data)
    elif data[0:1] == b"+":
        return _parse_simple_string(data)
    elif data[0:1] == b"-":
        return _parse_error(data)
    elif data[0:1] == b":":
        return _parse_integer(data)
    else:
        raise ProtocolError(f"Unknown RESP type byte: {data[0:1]!r}")


def _parse_simple_string(data: bytes) -> Tuple[List[str], bytes]:
    """Simple String (+OK) をパースする。"""
    crlf_pos = _find_crlf(data, 1)
    if crlf_pos == -1:
        raise ProtocolError("Simple String missing \\r\\n")
    value = data[1:crlf_pos].decode("utf-8")
    return [value], data[crlf_pos + 2:]


def _parse_error(data: bytes) -> Tuple[List[str], bytes]:
    """Error (-ERR) をパースする。"""
    crlf_pos = _find_crlf(data, 1)
    if crlf_pos == -1:
        raise ProtocolError("Error missing \\r\\n")
    value = data[1:crlf_pos].decode("utf-8")
    return [value], data[crlf_pos + 2:]


def _parse_integer(data: bytes) -> Tuple[List[str], bytes]:
    """Integer (:42) をパースする。"""
    crlf_pos = _find_crlf(data, 1)
    if crlf_pos == -1:
        raise ProtocolError("Integer missing \\r\\n")
    value = data[1:crlf_pos].decode("utf-8")
    try:
        int(value)
    except ValueError:
        raise ProtocolError(f"Invalid integer: {value!r}")
    return [value], data[crlf_pos + 2:]


def _parse_bulk_string(data: bytes) -> Tuple[List[str], bytes]:
    """Bulk String ($N) または Null Bulk String ($-1) をパースする。"""
    crlf_pos = _find_crlf(data, 1)
    if crlf_pos == -1:
        raise ProtocolError("Bulk String missing \\r\\n")
    size_str = data[1:crlf_pos].decode("utf-8")
    try:
        size = int(size_str)
    except ValueError:
        raise ProtocolError(f"Invalid bulk string size: {size_str!r}")

    if size == -1:
        return [None], data[crlf_pos + 2:]

    if size < -1:
        raise ProtocolError(f"Invalid bulk string size: {size}")

    data_start = crlf_pos + 2
    data_end = data_start + size
    if data_end + 2 > len(data):
        raise ProtocolError("Bulk String data truncated")

    value = data[data_start:data_end].decode("utf-8")
    return [value], data[data_end + 2:]


def _find_crlf(data: bytes, start: int) -> int:
    """start 以降で最初の \\r\\n の位置を返す。見つからない場合は -1。"""
    i = start
    while i < len(data) - 1:
        if data[i:i+2] == b"\r\n":
            return i
        i += 1
    return -1


def _parse_array(data: bytes) -> Tuple[Optional[List[str]], bytes]:
    """Array (*N) をパースする。"""
    # *N\r\n を探す
    crlf_pos = _find_crlf(data, 1)
    if crlf_pos == -1:
        return None, data  # データ不足

    count_str = data[1:crlf_pos].decode("utf-8")
    try:
        count = int(count_str)
    except ValueError:
        raise ProtocolError(f"Invalid array count: {count_str!r}")

    if count < 0:
        raise ProtocolError(f"Negative array count: {count}")

    # 配列要素をパース
    pos = crlf_pos + 2  # \r\n の次
    elements: List[str] = []

    for _ in range(count):
        if pos >= len(data):
            return None, data  # データ不足

        elem, consumed = _parse_resp_element(data[pos:])
        if elem is None:
            return None, data  # データ不足
        elements.append(elem)
        pos += consumed

    # 残りのデータ（次のコマンド等）
    remaining = data[pos:]
    return elements, remaining


def _parse_resp_element(data: bytes) -> Tuple[Optional[str], int]:
    """
    1つの RESP 要素をパースする。

    Returns:
        (要素の文字列値, 消費したバイト数)
        要素が None の場合、データ不足。
    """
    if not data:
        return None, 0

    if data[0:1] == b"*":
        # 再帰的に配列をパース（ネスト配列はコマンド引数として想定外だが対応）
        result, consumed = _parse_array(data)
        if result is None:
            return None, 0
        # ネスト配列は文字列化して返す（本来はリストだが、コマンド引数としては来ない）
        return str(result), consumed

    elif data[0:1] == b"$":
        # Bulk String または Null Bulk String
        crlf_pos = _find_crlf(data, 1)
        if crlf_pos == -1:
            return None, 0  # データ不足

        size_str = data[1:crlf_pos].decode("utf-8")
        try:
            size = int(size_str)
        except ValueError:
            raise ProtocolError(f"Invalid bulk string size: {size_str!r}")

        if size == -1:
            # Null Bulk String
            return None, crlf_pos + 2

        if size < -1:
            raise ProtocolError(f"Invalid bulk string size: {size}")

        # $N\r\n<data>\r\n
        data_start = crlf_pos + 2
        data_end = data_start + size
        if data_end + 2 > len(data):
            return None, 0  # データ不足

        value = data[data_start:data_end].decode("utf-8")
        return value, data_end + 2

    elif data[0:1] == b"+":
        # Simple String
        crlf_pos = _find_crlf(data, 1)
        if crlf_pos == -1:
            return None, 0  # データ不足
        value = data[1:crlf_pos].decode("utf-8")
        return value, crlf_pos + 2

    elif data[0:1] == b"-":
        # Error
        crlf_pos = _find_crlf(data, 1)
        if crlf_pos == -1:
            return None, 0  # データ不足
        value = data[1:crlf_pos].decode("utf-8")
        return value, crlf_pos + 2

    elif data[0:1] == b":":
        # Integer
        crlf_pos = _find_crlf(data, 1)
        if crlf_pos == -1:
            return None, 0  # データ不足
        value = data[1:crlf_pos].decode("utf-8")
        try:
            int(value)  # 数値として有効か検証
        except ValueError:
            raise ProtocolError(f"Invalid integer: {value!r}")
        return value, crlf_pos + 2

    else:
        raise ProtocolError(f"Unknown RESP type byte: {data[0:1]!r}")


def serialize_resp(value) -> bytes:
    """
    Python の値を RESP 形式のバイト列に変換する。

    Args:
        value:
            - str: Simple String または Bulk String として返す
            - int: Integer として返す
            - None: Null Bulk String として返す
            - list: Array として返す（要素は str または int）

    Returns:
        RESP 形式のバイト列
    """
    if value is None:
        return b"$-1\r\n"

    if isinstance(value, int):
        return f":{value}\r\n".encode("utf-8")

    if isinstance(value, str):
        # Simple String 応答（"+OK", "+PONG" 等）と Bulk String 応答を区別
        # 正確に "OK" または "PONG" の場合は Simple String として返す
        if value in ("OK", "PONG"):
            return f"+{value}\r\n".encode("utf-8")
        # 先頭が "+" で始まる場合は Simple String として返す
        if value.startswith("+"):
            return f"{value}\r\n".encode("utf-8")
        # 先頭が "$" で始まる場合は Bulk String として返す
        if value.startswith("$"):
            return f"{value}\r\n".encode("utf-8")
        # 通常文字列は Bulk String として返す
        encoded = value.encode("utf-8")
        return f"${len(encoded)}\r\n{value}\r\n".encode("utf-8")

    if isinstance(value, list):
        parts = [f"*{len(value)}\r\n"]
        for item in value:
            parts.append(serialize_resp(item).decode("utf-8"))
        return "".join(parts).encode("utf-8")

    raise TypeError(f"Unsupported type for RESP serialization: {type(value)}")