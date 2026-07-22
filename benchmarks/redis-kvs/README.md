# Redis 互換ミニKVSサーバー

Redis Serialization Protocol (RESP) を実装した、最小構成のインメモリ Key-Value Store サーバーです。

## 概要

- Python 標準ライブラリのみで実装
- TCP サーバーとして動作
- RESP 風プロトコルに対応
- 複数クライアントからの同時接続に対応
- TTL（Time To Live）機能付き

## ファイル構成

```
.
├── server.py          # TCP サーバー本体
├── resp.py            # RESP プロトコル パーサ・シリアライザ
├── store.py           # インメモリ Key-Value ストレージ
├── commands.py        # コマンド実行ロジック
├── tests/
│   └── test_server.py # テストコード
└── README.md          # このファイル
```

## 起動方法

```bash
python3 server.py --port 6379
```

デフォルトポート: `6379`

## 対応コマンド

| コマンド | 説明 | 応答例 |
|---------|------|--------|
| `PING` | サーバー応答確認 | `+PONG` |
| `ECHO <message>` | メッセージ返却 | `$5\r\nhello\r\n` |
| `SET <key> <value>` | キー値設定 | `+OK` |
| `GET <key>` | キー値取得 | `$3\r\nbar\r\n` / `$-1` |
| `DEL <key>` | キー削除 | `:1` / `:0` |
| `EXPIRE <key> <seconds>` | TTL 設定 | `:1` / `:0` |
| `TTL <key>` | 残り TTL 秒数取得 | `:-1` / `:-2` / `:N` |

## テスト実行

```bash
python3 -m unittest discover -s tests
```

## 仕様準拠

- SPEC.md に準拠
- 標準ライブラリのみを使用
- Redis 本体・Redis 互換ライブラリは使用していない