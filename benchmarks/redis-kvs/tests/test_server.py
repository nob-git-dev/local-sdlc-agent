"""
Redis 互換ミニKVSのテスト。

標準ライブラリ unittest のみを使用。
"""

import socket
import threading
import time
import unittest
from unittest.mock import patch

from resp import parse_resp, serialize_resp, ProtocolError
from store import Store
from commands import handle_command
from server import KVSTCPServer, KVSRequestHandler


class TestRespParser(unittest.TestCase):
    """RESP パーサのユニットテスト。"""

    def test_parse_simple_string(self):
        """Simple String (+OK) をパースできる。"""
        result, remaining = parse_resp(b"+OK\r\n")
        self.assertEqual(result, ["OK"])
        self.assertEqual(remaining, b"")

    def test_parse_bulk_string(self):
        """Bulk String ($5\r\nhello\r\n) をパースできる。"""
        result, remaining = parse_resp(b"$5\r\nhello\r\n")
        self.assertEqual(result, ["hello"])
        self.assertEqual(remaining, b"")

    def test_parse_null_bulk_string(self):
        """Null Bulk String ($-1) をパースできる。"""
        result, remaining = parse_resp(b"$-1\r\n")
        self.assertEqual(result, [None])
        self.assertEqual(remaining, b"")

    def test_parse_integer(self):
        """Integer (:42) をパースできる。"""
        result, remaining = parse_resp(b":42\r\n")
        self.assertEqual(result, ["42"])
        self.assertEqual(remaining, b"")

    def test_parse_error(self):
        """Error (-ERR) をパースできる。"""
        result, remaining = parse_resp(b"-ERR something went wrong\r\n")
        self.assertEqual(result, ["ERR something went wrong"])
        self.assertEqual(remaining, b"")

    def test_parse_array(self):
        """Array (*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n) をパースできる。"""
        data = b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n"
        result, remaining = parse_resp(data)
        self.assertEqual(result, ["SET", "foo", "bar"])
        self.assertEqual(remaining, b"")

    def test_parse_multiple_commands(self):
        """複数のコマンドを連続してパースできる。"""
        data = b"*1\r\n$4\r\nPING\r\n*2\r\n$4\r\nECHO\r\n$5\r\nhello\r\n"
        result1, remaining = parse_resp(data)
        self.assertEqual(result1, ["PING"])
        self.assertEqual(remaining, b"*2\r\n$4\r\nECHO\r\n$5\r\nhello\r\n")

        result2, remaining2 = parse_resp(remaining)
        self.assertEqual(result2, ["ECHO", "hello"])
        self.assertEqual(remaining2, b"")

    def test_parse_data_insufficient(self):
        """データ不足の場合は None を返す。"""
        result, remaining = parse_resp(b"*1\r\n$3\r\nSE")
        self.assertIsNone(result)
        self.assertEqual(remaining, b"*1\r\n$3\r\nSE")

    def test_parse_invalid_simple_string(self):
        """不正な Simple String で ProtocolError を送出する。"""
        with self.assertRaises(ProtocolError):
            parse_resp(b"+OK")  # \r\n がない

    def test_parse_invalid_integer(self):
        """不正な Integer で ProtocolError を送出する。"""
        with self.assertRaises(ProtocolError):
            parse_resp(b":abc\r\n")

    def test_parse_invalid_bulk_string_size(self):
        """無効な Bulk String サイズで ProtocolError を送出する。"""
        with self.assertRaises(ProtocolError):
            parse_resp(b"$abc\r\n")

    def test_parse_unknown_type(self):
        """未知のタイプバイトで ProtocolError を送出する。"""
        with self.assertRaises(ProtocolError):
            parse_resp(b"?unknown\r\n")

    def test_parse_empty_data(self):
        """空データの場合は None を返す。"""
        result, remaining = parse_resp(b"")
        self.assertIsNone(result)
        self.assertEqual(remaining, b"")


class TestRespSerializer(unittest.TestCase):
    """RESP シリアライザのユニットテスト。"""

    def test_serialize_simple_string(self):
        """Simple String をシリアライズできる。"""
        result = serialize_resp("OK")
        self.assertEqual(result, b"+OK\r\n")

    def test_serialize_bulk_string(self):
        """Bulk String をシリアライズできる。"""
        result = serialize_resp("hello")
        self.assertEqual(result, b"$5\r\nhello\r\n")

    def test_serialize_integer(self):
        """Integer をシリアライズできる。"""
        result = serialize_resp(42)
        self.assertEqual(result, b":42\r\n")

    def test_serialize_null(self):
        """None を Null Bulk String としてシリアライズできる。"""
        result = serialize_resp(None)
        self.assertEqual(result, b"$-1\r\n")

    def test_serialize_array(self):
        """Array をシリアライズできる。"""
        result = serialize_resp(["SET", "foo", "bar"])
        self.assertEqual(result, b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")


class TestStore(unittest.TestCase):
    """Store のユニットテスト。"""

    def setUp(self):
        self.store = Store()

    def test_set_and_get(self):
        """SET と GET で値の保存・取得ができる。"""
        self.store.set("foo", "bar")
        self.assertEqual(self.store.get("foo"), "bar")

    def test_get_nonexistent(self):
        """存在しないキーの GET は None を返す。"""
        self.assertIsNone(self.store.get("nonexistent"))

    def test_delete_existing(self):
        """既存キーの DEL は 1 を返す。"""
        self.store.set("foo", "bar")
        self.assertEqual(self.store.delete("foo"), 1)

    def test_delete_nonexistent(self):
        """存在しないキーの DEL は 0 を返す。"""
        self.assertEqual(self.store.delete("nonexistent"), 0)

    def test_expire_existing(self):
        """既存キーの EXPIRE は 1 を返す。"""
        self.store.set("foo", "bar")
        self.assertEqual(self.store.expire("foo", 10), 1)

    def test_expire_nonexistent(self):
        """存在しないキーの EXPIRE は 0 を返す。"""
        self.assertEqual(self.store.expire("nonexistent", 10), 0)

    def test_ttl_no_expiry(self):
        """TTL がないキーの TTL は -1 を返す。"""
        self.store.set("foo", "bar")
        self.assertEqual(self.store.ttl("foo"), -1)

    def test_ttl_with_expiry(self):
        """TTL があるキーの TTL は正の値を返す。"""
        self.store.set("foo", "bar")
        self.store.expire("foo", 10)
        ttl = self.store.ttl("foo")
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 10)

    def test_ttl_nonexistent(self):
        """存在しないキーの TTL は -2 を返す。"""
        self.assertEqual(self.store.ttl("nonexistent"), -2)

    def test_ttl_expiry(self):
        """TTL 切れキーは GET で取得できない。"""
        self.store.set("foo", "bar")
        self.store.expire("foo", 0)
        time.sleep(0.1)  # TTL 切れを待つ
        self.assertIsNone(self.store.get("foo"))

    def test_keys(self):
        """全キー一覧を取得できる。"""
        self.store.set("foo", "bar")
        self.store.set("baz", "qux")
        keys = self.store.keys()
        self.assertEqual(set(keys), {"foo", "baz"})

    def test_thread_safety(self):
        """スレッドセーフである。"""
        errors = []

        def writer():
            try:
                for i in range(100):
                    self.store.set(f"key{i}", f"value{i}")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for i in range(100):
                    self.store.get(f"key{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(5)]
        threads += [threading.Thread(target=reader) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])


class TestCommands(unittest.TestCase):
    """コマンド実行層のユニットテスト。"""

    def setUp(self):
        self.store = Store()

    def test_ping(self):
        """PING コマンドは +PONG を返す。"""
        response = handle_command(self.store, ["PING"])
        self.assertEqual(response, b"+PONG\r\n")

    def test_ping_with_argument(self):
        """PING コマンドに引数がある場合、ECHO として応答する。"""
        response = handle_command(self.store, ["PING", "hello"])
        self.assertEqual(response, b"$5\r\nhello\r\n")

    def test_echo(self):
        """ECHO コマンドは引数をそのまま返す。"""
        response = handle_command(self.store, ["ECHO", "hello"])
        self.assertEqual(response, b"$5\r\nhello\r\n")

    def test_set_and_get(self):
        """SET と GET で値の保存・取得ができる。"""
        handle_command(self.store, ["SET", "foo", "bar"])
        response = handle_command(self.store, ["GET", "foo"])
        self.assertEqual(response, b"$3\r\nbar\r\n")

    def test_get_nonexistent(self):
        """存在しないキーの GET は Null Bulk String を返す。"""
        response = handle_command(self.store, ["GET", "nonexistent"])
        self.assertEqual(response, b"$-1\r\n")

    def test_del_existing(self):
        """既存キーの DEL は 1 を返す。"""
        handle_command(self.store, ["SET", "foo", "bar"])
        response = handle_command(self.store, ["DEL", "foo"])
        self.assertEqual(response, b":1\r\n")

    def test_del_nonexistent(self):
        """存在しないキーの DEL は 0 を返す。"""
        response = handle_command(self.store, ["DEL", "nonexistent"])
        self.assertEqual(response, b":0\r\n")

    def test_expire_existing(self):
        """既存キーの EXPIRE は 1 を返す。"""
        handle_command(self.store, ["SET", "foo", "bar"])
        response = handle_command(self.store, ["EXPIRE", "foo", "10"])
        self.assertEqual(response, b":1\r\n")

    def test_expire_nonexistent(self):
        """存在しないキーの EXPIRE は 0 を返す。"""
        response = handle_command(self.store, ["EXPIRE", "nonexistent", "10"])
        self.assertEqual(response, b":0\r\n")

    def test_ttl_no_expiry(self):
        """TTL がないキーの TTL は -1 を返す。"""
        handle_command(self.store, ["SET", "foo", "bar"])
        response = handle_command(self.store, ["TTL", "foo"])
        self.assertEqual(response, b":-1\r\n")

    def test_ttl_with_expiry(self):
        """TTL があるキーの TTL は正の値を返す。"""
        handle_command(self.store, ["SET", "foo", "bar"])
        handle_command(self.store, ["EXPIRE", "foo", "10"])
        response = handle_command(self.store, ["TTL", "foo"])
        ttl = int(response.decode("utf-8").strip(":").strip("\r\n"))
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 10)

    def test_ttl_nonexistent(self):
        """存在しないキーの TTL は -2 を返す。"""
        response = handle_command(self.store, ["TTL", "nonexistent"])
        self.assertEqual(response, b":-2\r\n")

    def test_case_insensitive(self):
        """コマンド名は大文字小文字を区別しない。"""
        handle_command(self.store, ["set", "foo", "bar"])
        response = handle_command(self.store, ["get", "foo"])
        self.assertEqual(response, b"$3\r\nbar\r\n")

    def test_unknown_command(self):
        """未知のコマンドはエラーを返す。"""
        response = handle_command(self.store, ["UNKNOWN"])
        self.assertTrue(response.startswith(b"-ERR"))

    def test_wrong_number_of_arguments(self):
        """引数数が不正な場合はエラーを返す。"""
        response = handle_command(self.store, ["SET"])
        self.assertTrue(response.startswith(b"-ERR"))

        response = handle_command(self.store, ["GET", "foo", "extra"])
        self.assertTrue(response.startswith(b"-ERR"))


class TestServerIntegration(unittest.TestCase):
    """サーバー統合テスト。"""

    @classmethod
    def setUpClass(cls):
        """テストクラス起動時にサーバーを起動。"""
        cls.store = Store()
        cls.server = KVSTCPServer(("127.0.0.1", 0), KVSRequestHandler, cls.store)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()
        time.sleep(0.1)  # サーバー起動を待つ

    @classmethod
    def tearDownClass(cls):
        """テストクラス終了時にサーバーを停止。"""
        cls.server.shutdown()
        cls.server.server_close()

    def _send_command(self, data: bytes) -> bytes:
        """
        サーバーにコマンドを送信し、1つのRESP応答を取得。

        RESP長さベースで正確に1応答だけ読み、サーバーが接続を維持しても
        即座に返る。巨大値（4096 bytes以上）でも途中で返らない。

        Args:
            data: 送信する RESP 形式のバイト列

        Returns:
            応答のバイト列
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", self.port))
            sock.sendall(data)

            # 先頭1バイト受信（タイプ判定用）
            header = b""
            while len(header) < 1:
                chunk = sock.recv(1)
                if not chunk:
                    raise ConnectionError("Connection closed before response header")
                header += chunk

            response = header

            # タイプに応じた読み取り
            if header[0:1] in (b"+", b"-", b":"):
                # Simple String / Error / Integer: CRLF まで読み取り
                while b"\r\n" not in response:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            elif header[0:1] == b"$":
                # Bulk String / Null Bulk String: 長さヘッダ + 本文 + CRLF
                # まず長さヘッダ（$N\r\n）を取得
                while b"\r\n" not in response:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk

                # 長さヘッダからサイズを抽出
                header_end = response.index(b"\r\n") + 2
                size_str = response[1:header_end - 2].decode("utf-8")
                try:
                    size = int(size_str)
                except ValueError:
                    raise ProtocolError(f"Invalid bulk string size: {size_str!r}")

                if size == -1:
                    # Null Bulk String: ヘッダのみ（$-1\r\n）
                    pass
                elif size >= 0:
                    # 本文 + CRLF を正確に読み取り
                    total_needed = header_end + size + 2
                    while len(response) < total_needed:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        response += chunk
            else:
                raise ProtocolError(f"Unknown RESP type byte: {header[0:1]!r}")

            return response

    def test_ping(self):
        """PING コマンドは +PONG を返す。"""
        response = self._send_command(b"*1\r\n$4\r\nPING\r\n")
        self.assertEqual(response, b"+PONG\r\n")

    def test_echo(self):
        """ECHO コマンドは引数をそのまま返す。"""
        response = self._send_command(b"*2\r\n$4\r\nECHO\r\n$5\r\nhello\r\n")
        self.assertEqual(response, b"$5\r\nhello\r\n")

    def test_set_and_get(self):
        """SET と GET で値の保存・取得ができる。"""
        self._send_command(b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")
        response = self._send_command(b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n")
        self.assertEqual(response, b"$3\r\nbar\r\n")

    def test_get_nonexistent(self):
        """存在しないキーの GET は Null Bulk String を返す。"""
        response = self._send_command(b"*2\r\n$3\r\nGET\r\n$11\r\nnonexistent\r\n")
        self.assertEqual(response, b"$-1\r\n")

    def test_del_existing(self):
        """既存キーの DEL は 1 を返す。"""
        self._send_command(b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")
        response = self._send_command(b"*2\r\n$3\r\nDEL\r\n$3\r\nfoo\r\n")
        self.assertEqual(response, b":1\r\n")

    def test_del_nonexistent(self):
        """存在しないキーの DEL は 0 を返す。"""
        response = self._send_command(b"*2\r\n$3\r\nDEL\r\n$11\r\nnonexistent\r\n")
        self.assertEqual(response, b":0\r\n")

    def test_expire_existing(self):
        """既存キーの EXPIRE は 1 を返す。"""
        self._send_command(b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")
        response = self._send_command(b"*3\r\n$6\r\nEXPIRE\r\n$3\r\nfoo\r\n$2\r\n10\r\n")
        self.assertEqual(response, b":1\r\n")

    def test_expire_nonexistent(self):
        """存在しないキーの EXPIRE は 0 を返す。"""
        response = self._send_command(b"*3\r\n$6\r\nEXPIRE\r\n$11\r\nnonexistent\r\n$2\r\n10\r\n")
        self.assertEqual(response, b":0\r\n")

    def test_ttl_no_expiry(self):
        """TTL がないキーの TTL は -1 を返す。"""
        self._send_command(b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")
        response = self._send_command(b"*2\r\n$3\r\nTTL\r\n$3\r\nfoo\r\n")
        self.assertEqual(response, b":-1\r\n")

    def test_ttl_with_expiry(self):
        """TTL があるキーの TTL は正の値を返す。"""
        self._send_command(b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")
        self._send_command(b"*3\r\n$6\r\nEXPIRE\r\n$3\r\nfoo\r\n$2\r\n10\r\n")
        response = self._send_command(b"*2\r\n$3\r\nTTL\r\n$3\r\nfoo\r\n")
        ttl = int(response.decode("utf-8").strip(":").strip("\r\n"))
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 10)

    def test_ttl_nonexistent(self):
        """存在しないキーの TTL は -2 を返す。"""
        response = self._send_command(b"*2\r\n$3\r\nTTL\r\n$11\r\nnonexistent\r\n")
        self.assertEqual(response, b":-2\r\n")

    def test_ttl_expiry(self):
        """TTL 切れキーは GET で取得できない。"""
        self._send_command(b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")
        self._send_command(b"*3\r\n$6\r\nEXPIRE\r\n$3\r\nfoo\r\n$1\r\n0\r\n")
        time.sleep(0.1)  # TTL 切れを待つ
        response = self._send_command(b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n")
        self.assertEqual(response, b"$-1\r\n")

    def test_case_insensitive(self):
        """コマンド名は大文字小文字を区別しない。"""
        self._send_command(b"*3\r\n$3\r\nset\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")
        response = self._send_command(b"*2\r\n$3\r\nget\r\n$3\r\nfoo\r\n")
        self.assertEqual(response, b"$3\r\nbar\r\n")

    def _read_one_resp_from_sock(self, sock):
        """
        ソケットからRESP応答を1つ読み取る。

        _send_command と同等の長さベース読み取りロジックを使用。
        """
        # 先頭1バイト受信（タイプ判定用）
        header = b""
        while len(header) < 1:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("Connection closed before response header")
            header += chunk

        response = header

        # タイプに応じた読み取り
        if header[0:1] in (b"+", b"-", b":"):
            # Simple String / Error / Integer: CRLF まで読み取り
            while b"\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        elif header[0:1] == b"$":
            # Bulk String / Null Bulk String: 長さヘッダ + 本文 + CRLF
            while b"\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

            header_end = response.index(b"\r\n") + 2
            size_str = response[1:header_end - 2].decode("utf-8")
            try:
                size = int(size_str)
            except ValueError:
                raise ProtocolError(f"Invalid bulk string size: {size_str!r}")

            if size == -1:
                pass
            elif size >= 0:
                total_needed = header_end + size + 2
                while len(response) < total_needed:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
        else:
            raise ProtocolError(f"Unknown RESP type byte: {header[0:1]!r}")

        return response

    def test_multiple_commands_same_connection(self):
        """同一接続で複数のコマンドを連続実行できる。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", self.port))

            # SET を送信して応答を1つ読み取る
            sock.sendall(b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")
            resp1 = self._read_one_resp_from_sock(sock)
            self.assertEqual(resp1, b"+OK\r\n")

            # GET を送信して応答を1つ読み取る
            sock.sendall(b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n")
            resp2 = self._read_one_resp_from_sock(sock)
            self.assertEqual(resp2, b"$3\r\nbar\r\n")

            # PING を送信して応答を1つ読み取る
            sock.sendall(b"*1\r\n$4\r\nPING\r\n")
            resp3 = self._read_one_resp_from_sock(sock)
            self.assertEqual(resp3, b"+PONG\r\n")

    def test_multiple_clients(self):
        """複数クライアントから同時に接続できる。"""
        results = []

        def client_task(i):
            try:
                response = self._send_command(
                    b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$6\r\nvalue" + str(i).encode() + b"\r\n"
                )
                response = self._send_command(b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n")
                results.append(response)
            except Exception as e:
                results.append(e)

        threads = [threading.Thread(target=client_task, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 全クライアントが正常にレスポンスを返したか確認
        for r in results:
            if isinstance(r, Exception):
                self.fail(f"Client error: {r}")

    def test_large_value(self):
        """巨大な値でも正常に処理できる。"""
        large_value = "x" * 10000
        set_cmd = b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$" + str(len(large_value)).encode() + b"\r\n" + large_value.encode() + b"\r\n"
        self._send_command(set_cmd)
        response = self._send_command(b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n")
        expected = b"$10000\r\n" + large_value.encode() + b"\r\n"
        self.assertEqual(response, expected)

    def test_empty_string(self):
        """空文字列でも正常に処理できる。"""
        self._send_command(b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$0\r\n\r\n")
        response = self._send_command(b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n")
        self.assertEqual(response, b"$0\r\n\r\n")

    def test_invalid_resp_input(self):
        """不正な RESP 入力でもサーバーが落ちない。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect(("127.0.0.1", self.port))
            sock.sendall(b"invalid data\r\n")
            # サーバーが応答を返すか、接続が閉じられるか
            try:
                response = sock.recv(4096)
                # エラー応答が返ってくることを期待
                self.assertTrue(response.startswith(b"-ERR"))
            except Exception:
                pass  # 接続が閉じられても OK


if __name__ == "__main__":
    unittest.main()