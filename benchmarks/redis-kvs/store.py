"""
Redis 互換ミニKVSのインメモリ保存層。

標準ライブラリのみを使用。
スレッドセーフな辞書と TTL 管理を提供する。
"""

import threading
import time
from typing import Dict, Optional


class Store:
    """
    スレッドセーフなインメモリ Key-Value Store。

    各キーは `{value: str, expires_at: float | None}` の辞書で管理される。
    expires_at は time.monotonic() からの経過秒数（秒単位）。
    操作前（get, delete, ttl）に TTL 切れチェックを行い、切れればキーを削除する。
    """

    def __init__(self) -> None:
        self._data: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: str) -> None:
        """
        キーと値を格納。既存キーの上書き時は TTL をリセット。

        Args:
            key: キー名
            value: 値
        """
        with self._lock:
            self._data[key] = {"value": value, "expires_at": None}

    def get(self, key: str) -> Optional[str]:
        """
        値を取得。TTL 切れなら None。存在しなければ None。

        Args:
            key: キー名

        Returns:
            値（文字列）または None
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None

            # TTL 切れチェック
            if self._is_expired(entry):
                del self._data[key]
                return None

            return entry["value"]

    def delete(self, key: str) -> int:
        """
        キーを削除。削除数 (0 or 1) を返す。

        Args:
            key: キー名

        Returns:
            削除したキー数
        """
        with self._lock:
            if key in self._data:
                # TTL 切れチェック（切れれば削除）
                if self._is_expired(self._data[key]):
                    del self._data[key]
                    return 0
                del self._data[key]
                return 1
            return 0

    def expire(self, key: str, seconds: int) -> int:
        """
        TTL を設定。成功すれば 1、キー不存在なら 0。

        Args:
            key: キー名
            seconds: TTL 秒数（正の整数）

        Returns:
            1 (成功) または 0 (キー不存在)

        Raises:
            TypeError: seconds が整数でない場合
            ValueError: seconds が負数または浮動小数点の場合
        """
        # 引数検証
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            raise TypeError(f"seconds must be an integer, got {type(seconds).__name__}")
        if seconds < 0:
            raise ValueError(f"seconds must be non-negative, got {seconds}")

        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return 0

            # TTL 切れチェック
            if self._is_expired(entry):
                del self._data[key]
                return 0

            # TTL 設定（time.monotonic() を使用）
            entry["expires_at"] = time.monotonic() + seconds
            return 1

    def ttl(self, key: str) -> int:
        """
        残り TTL 秒数を返す。

        Args:
            key: キー名

        Returns:
            -2: キー不存在
            -1: TTL なし
            0以上: 残り秒数
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return -2

            # TTL 切れチェック
            if self._is_expired(entry):
                del self._data[key]
                return -2

            if entry["expires_at"] is None:
                return -1

            remaining = entry["expires_at"] - time.monotonic()
            if remaining <= 0:
                del self._data[key]
                return -2

            return int(remaining)

    def keys(self) -> list[str]:
        """
        全キー一覧を返す（INFO/KEYS コマンド用）。

        Returns:
            生存中のキー一覧
        """
        with self._lock:
            # TTL 切れキーを除外
            expired_keys = [
                k for k, v in self._data.items()
                if self._is_expired(v)
            ]
            for k in expired_keys:
                del self._data[k]

            return list(self._data.keys())

    @staticmethod
    def _is_expired(entry: dict) -> bool:
        """
        エントリが TTL 切れかどうかを判定。

        Args:
            entry: {value, expires_at} の辞書

        Returns:
            True: TTL 切れ
            False: 生存中
        """
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return False
        return time.monotonic() >= expires_at