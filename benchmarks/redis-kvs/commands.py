"""
Redis 互換ミニKVSのコマンド実行層。

RESP形式でパースされたコマンド引数を受け取り、store.py の KVStore インスタンスを介して
データ操作を委譲し、RESP形式の bytes を返す。

標準ライブラリのみを使用。
"""

from typing import List
from store import Store


def handle_command(store: Store, args: List[str]) -> bytes:
    """
    コマンド引数リストを受け取り、RESP形式の bytes を返す。

    Args:
        store: KVStore インスタンス
        args: パース済みのコマンド引数リスト（コマンド名含む）

    Returns:
        RESP形式の bytes 応答
    """
    if not args:
        return b"-ERR empty command\r\n"

    command = args[0].upper()

    try:
        if command == "PING":
            return _handle_ping(args)
        elif command == "ECHO":
            return _handle_echo(args)
        elif command == "SET":
            return _handle_set(store, args)
        elif command == "GET":
            return _handle_get(store, args)
        elif command == "DEL":
            return _handle_del(store, args)
        elif command == "EXPIRE":
            return _handle_expire(store, args)
        elif command == "TTL":
            return _handle_ttl(store, args)
        else:
            return f"-ERR unknown command '{args[0]}'\r\n".encode("utf-8")
    except Exception as e:
        return f"-ERR {str(e)}\r\n".encode("utf-8")


def _handle_ping(args: List[str]) -> bytes:
    """
    PING コマンド。

    引数なし: +PONG
    引数あり: Bulk String として応答
    """
    if len(args) == 1:
        return b"+PONG\r\n"
    elif len(args) == 2:
        message = args[1]
        encoded = message.encode("utf-8")
        return f"${len(encoded)}\r\n{message}\r\n".encode("utf-8")
    else:
        return b"-ERR wrong number of arguments for 'ping' command\r\n"


def _handle_echo(args: List[str]) -> bytes:
    """
    ECHO コマンド。

    引数: 1 (メッセージ)
    応答: Bulk String
    """
    if len(args) != 2:
        return b"-ERR wrong number of arguments for 'echo' command\r\n"

    message = args[1]
    encoded = message.encode("utf-8")
    return f"${len(encoded)}\r\n{message}\r\n".encode("utf-8")


def _handle_set(store: Store, args: List[str]) -> bytes:
    """
    SET コマンド。

    引数: 2 (key, value)
    応答: +OK
    """
    if len(args) != 3:
        return b"-ERR wrong number of arguments for 'set' command\r\n"

    key = args[1]
    value = args[2]

    store.set(key, value)
    return b"+OK\r\n"


def _handle_get(store: Store, args: List[str]) -> bytes:
    """
    GET コマンド。

    引数: 1 (key)
    応答: Bulk String または Null Bulk String
    """
    if len(args) != 2:
        return b"-ERR wrong number of arguments for 'get' command\r\n"

    key = args[1]
    value = store.get(key)

    if value is None:
        return b"$-1\r\n"

    encoded = value.encode("utf-8")
    return f"${len(encoded)}\r\n{value}\r\n".encode("utf-8")


def _handle_del(store: Store, args: List[str]) -> bytes:
    """
    DEL コマンド。

    引数: 1 (key)
    応答: Integer (削除数)
    """
    if len(args) != 2:
        return b"-ERR wrong number of arguments for 'del' command\r\n"

    key = args[1]
    count = store.delete(key)

    return f":{count}\r\n".encode("utf-8")


def _handle_expire(store: Store, args: List[str]) -> bytes:
    """
    EXPIRE コマンド。

    引数: 2 (key, seconds)
    応答: Integer (1: 成功, 0: キー不存在)
    """
    if len(args) != 3:
        return b"-ERR wrong number of arguments for 'expire' command\r\n"

    key = args[1]

    try:
        seconds = int(args[2])
    except ValueError:
        return b"-ERR value is not an integer or out of range\r\n"

    result = store.expire(key, seconds)
    return f":{result}\r\n".encode("utf-8")


def _handle_ttl(store: Store, args: List[str]) -> bytes:
    """
    TTL コマンド。

    引数: 1 (key)
    応答: Integer (-2: 不存在, -1: TTLなし, 0以上: 残り秒数)
    """
    if len(args) != 2:
        return b"-ERR wrong number of arguments for 'ttl' command\r\n"

    key = args[1]
    ttl = store.ttl(key)

    return f":{ttl}\r\n".encode("utf-8")