# server.py
"""
Redis 互換ミニKVSのTCPサーバー。

標準ライブラリのみを使用。
commands.py の handle_command 関数を呼び出してコマンド実行を委譲する。
"""

import argparse
import logging
import socketserver
import sys
import threading
from typing import Optional

from resp import parse_resp, ProtocolError
from commands import handle_command
from store import Store

# グローバルログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class KVSRequestHandler(socketserver.BaseRequestHandler):
    """
    1クライアント接続ごとのリクエストハンドラ。

    接続ごとにスレッドが割り当てられ、recv でデータを受信し、
    commands.py の handle_command を呼び出して応答を返す。
    不正な入力でも接続ごとの例外をキャッチし、接続を閉じて次の接続へ。
    """

    def setup(self) -> None:
        """接続確立時の初期化。"""
        client_addr = self.client_address
        logger.info("Client connected: %s", client_addr)

    def handle(self) -> None:
        """
        メインループ。recv でデータを受信し、RESP パース → コマンド実行 → 応答送信を繰り返す。
        接続が閉じられた場合や例外が発生した場合はループを抜ける。
        """
        buffer = b""
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    # クライアントが接続を閉じた
                    logger.info("Client disconnected: %s", self.client_address)
                    break

                buffer += data

                # RESP パースを試みる
                while buffer:
                    try:
                        command_args, remaining = parse_resp(buffer)
                    except ProtocolError as e:
                        # 不正な RESP フォーマット
                        logger.warning(
                            "Protocol error from %s: %s",
                            self.client_address,
                            e,
                        )
                        # エラー応答を返す
                        error_resp = f"-ERR {str(e)}\r\n".encode("utf-8")
                        self.request.sendall(error_resp)
                        # パース不能なデータを破棄（次のコマンドへ）
                        buffer = b""
                        continue

                    if command_args is None:
                        # データ不足でパース不能。次の recv を待つ。
                        break

                    # コマンド実行
                    store = self.server.store  # type: ignore[attr-defined]
                    response = handle_command(store, command_args)
                    self.request.sendall(response)

                    # 未処理の残データを次のループで再パース
                    buffer = remaining

        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.warning(
                "Connection error with %s: %s",
                self.client_address,
                e,
            )
        except Exception as e:
            logger.error(
                "Unexpected error handling client %s: %s",
                self.client_address,
                e,
            )
        finally:
            try:
                self.request.close()
            except Exception:
                pass
            logger.info("Client finished: %s", self.client_address)


class KVSTCPServer(socketserver.ThreadingTCPServer):
    """
    スレッド毎に接続を処理するTCPサーバー。

    Store インスタンスを保持し、ハンドラに渡す。
    """

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[KVSRequestHandler],
        store: Store,
    ) -> None:
        self.store = store
        super().__init__(server_address, RequestHandlerClass)


def parse_args() -> argparse.Namespace:
    """
    コマンドライン引数をパース。

    Returns:
        --port: 待受ポート番号（既定: 6379）
    """
    parser = argparse.ArgumentParser(
        description="Redis 互換ミニKVSサーバー",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=6379,
        help="待受ポート番号（既定: 6379）",
    )
    return parser.parse_args()


def main() -> None:
    """
    サーバー起動エントリポイント。

    1. 引数パース
    2. Store 初期化
    3. TCP サーバー起動
    4. SIGINT/SIGTERM で graceful shutdown
    """
    args = parse_args()
    host = "127.0.0.1"
    port = args.port

    store = Store()
    server = KVSTCPServer((host, port), KVSRequestHandler, store)

    logger.info("Starting KVS server on %s:%d", host, port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
    finally:
        server.server_close()
        logger.info("Server stopped.")


if __name__ == "__main__":
    main()