# Redis互換ミニKVS開発プロセス記録

## 1. 仕様読解

- RESP風プロトコルを用いた最小インメモリKVSサーバー
- 実装コマンド: PING, ECHO, SET, GET, DEL, EXPIRE, TTL
- 非実装: LIST/SET/HASH/SORTED SET, AUTH/SELECT, パーシステンス
- 大文字小文字非区別、不正入力でサーバー全体が落ちない
- 標準ライブラリのみ使用、Redis本体/ライブラリ不使用

## 2. Agentによる分割実装

- resp.py: RESPパーサ/シリアライザ
- store.py: スレッドセーフインメモリストレージ
- commands.py: コマンド実行ロジック
- server.py: TCPサーバー
- tests/test_server.py: テスト
- README.md: ドキュメント

## 3. 失敗修正ループ

- serialize_resp: OK
- EXPIRE 0 のRESP長が不正: 修正
- 同一接続テストのTCPチャンク依存: 修正

## 4. 基盤側改善

- no-op search-replace拒否
- nested artifact marker拒否
- TDD skillの失敗ログ駆動ルール

## 5. 最終検証

- timeout 30 python3 -m unittest discover -s tests: 63 tests OK
- redis-smoke PASS
- runner基盤: 41 tests OK

## 6. 標準ライブラリのみ

- socketserver, threading, time, unittest, io, logging, argparse, os, sys
- 外部依存なし