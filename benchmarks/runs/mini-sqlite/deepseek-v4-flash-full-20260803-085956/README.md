# MiniSQLite Engine

MiniSQLite Engine は、SQLite の設計思想を参考にした**学習用のミニデータベースエンジン**です。SQLite 本体や SQLite の実ファイルフォーマットとは互換性がありません。

このプロジェクトは、SQL パース、実行計画、スキーマ管理、レコードエンコード、B+Tree による行管理、ページ単位の永続化、ファイル再オープン後のデータ復元、エラー処理、境界条件テストといったデータベースエンジンの中核要素を小さな範囲で実装することを目的としています。

---

## 1. 要件

- CLI から SQL を実行できる
- Python API から SQL を実行できる
- `CREATE TABLE` を実装する
- `INSERT INTO` を実装する
- `SELECT` を実装する
- `DELETE` を実装する
- `INTEGER` と `TEXT` 型を扱える
- `rowid` を内部的に持つ
- B+Tree で行を保存する
- データベースを単一ファイルに永続化する
- プロセス終了後、再起動してもデータを読める
- テストコードを作成する
- README を作成する

---

## 2. 構成

```text
minisqlite/
├── __init__.py
├── __main__.py
├── cli.py
├── connection.py
├── result.py
├── errors.py
├── sql/
│   ├── __init__.py
│   ├── lexer.py
│   ├── parser.py
│   └── ast.py
├── engine/
│   ├── __init__.py
│   ├── executor.py
│   ├── schema.py
│   └── planner.py
├── storage/
│   ├── __init__.py
│   ├── pager.py
│   ├── btree.py
│   ├── record.py
│   └── file_format.py
├── tests/
│   ├── test_lexer.py
│   ├── test_parser.py
│   ├── test_record.py
│   ├── test_btree.py
│   ├── test_persistence.py
│   ├── test_sql_execution.py
│   └── test_cli.py
├── README.md
└── pyproject.toml
```

---

## 3. 独自ファイル形式

本実装は SQLite 本体のファイルフォーマットとは互換にしません。以下の考え方を採用します。

- 単一ファイル
- 固定長ページ
- ページ単位の読み書き
- B+Tree ページ
- rowid をキーとするテーブル格納

### ページサイズ

ページサイズは 4096 バイト固定です。

```text
PAGE_SIZE = 4096
```

### ファイルヘッダ

ファイル先頭のページ 0 をデータベースヘッダページとします。

| オフセット | サイズ | 内容 |
|---:|---:|---|
| 0 | 8 | magic bytes: `MSQLITE1` |
| 8 | 4 | page_size: 4096 |
| 12 | 4 | format_version: 1 |
| 16 | 4 | next_page_id |
| 20 | 4 | schema_root_page |
| 24 | 8 | reserved |
| 32 | 可変 | schema metadata または空き |

すべての整数は big-endian で保存します。

### ページ ID

- ページ ID は 0 始まり
- page 0 はヘッダ専用
- page 1 以降を B+Tree ページとして使う
- 新規ページは `next_page_id` から採番する

---

## 4. B+Tree

B+Tree は、テーブルの行を rowid 順に管理するために使います。各テーブルごとに 1 つの B+Tree を持ちます。

- キーは rowid（signed 64-bit integer）
- 値はレコードペイロード（encoded record bytes）

### ページ種別

| 種別 | 値 | 内容 |
|---|---:|---|
| LEAF | 1 | rowid とレコードを保持する |
| INTERNAL | 2 | 子ページへの参照を保持する |

### 探索

root ページを読み、Internal ならキーを比較して子ページへ進み、Leaf に到達したら Leaf 内を二分探索または線形探索します。

### 挿入

挿入先 Leaf を探し、rowid 重複を確認してから挿入します。Leaf に空きがなければ分割し、親 Internal に分割情報を伝播します。root が分割された場合は新しい root を作ります。

### 削除

MVP では簡略化します。Leaf から該当セルを削除し、ページのマージや再分配は行いません。Internal ページのキー更新は必要に応じて行います。

---

## 5. CLI 実行例

### 対話モード

```bash
python -m minisqlite sample.db
```

```text
minisqlite> CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
OK
minisqlite> INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);
OK
minisqlite> SELECT * FROM users;
id|name|age
1|Alice|30
minisqlite> .tables
users
minisqlite> .schema users
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)
minisqlite> .exit
```

### 単一 SQL 実行

```bash
python -m minisqlite sample.db "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
python -m minisqlite sample.db "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
python -m minisqlite sample.db "SELECT * FROM users;"
```

---

## 6. Python API 実行例

```python
from minisqlite import connect

conn = connect("sample.db")
conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
conn.execute("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
result = conn.execute("SELECT * FROM users;")

assert result.columns == ["id", "name", "age"]
assert result.rows == [[1, "Alice", 30]]

conn.close()
```

---

## 7. 対応 SQL

- `CREATE TABLE`
- `INSERT INTO`
- `SELECT`
- `DELETE`

### 対応する型

| 型 | 内容 |
|---|---|
| INTEGER | 64bit 符号付き整数 |
| TEXT | UTF-8 文字列 |

### PRIMARY KEY

MVP では以下のみ対応します。

```sql
id INTEGER PRIMARY KEY
```

この場合、`id` は内部 rowid の別名として扱います。

### rowid

すべてのテーブルは内部的に `rowid` を持ちます。

- `INTEGER PRIMARY KEY` がある場合、そのカラムを rowid として使う
- `INTEGER PRIMARY KEY` がない場合、内部 rowid を自動採番する
- 自動採番は、当該テーブルの最大 rowid + 1 とする
- rowid は B+Tree のキーとして使う

### WHERE 条件

MVP では単一条件のみ対応します。

```sql
WHERE column_name = literal
WHERE column_name != literal
WHERE column_name > literal
WHERE column_name >= literal
WHERE column_name < literal
WHERE column_name <= literal
```

### 比較仕様

- INTEGER 同士は数値比較
- TEXT 同士は辞書順比較
- 型が異なる比較はエラー、または一致しないものとして扱う

### 出力順

MVP では、SELECT 結果は rowid 昇順とします。

### DELETE の制約

安全のため、MVP では `WHERE` なしの DELETE は禁止します。以下はエラーとなります。

```sql
DELETE FROM users;
```

全件削除をしたい場合は、将来拡張として `DELETE FROM users WHERE rowid >= 0;` のような条件を使います。

---

## 8. テスト方法

テストは `tests/` ディレクトリに配置します。テストは標準ライブラリの `unittest` を使用します。

```bash
python -m unittest discover tests
```

### テスト対象

- SQL Lexer
- SQL Parser
- Record Codec
- Pager
- B+Tree
- SQL Execution
- Persistence
- CLI

---

## 9. 禁止依存を使っていないこと

本実装では以下を使用していません。

- Python 標準の `sqlite3` モジュール
- 既存の SQL パーサライブラリ
- SQLAlchemy などの ORM
- dbm、shelve などの既存 KVS を内部ストレージとして使用
- JSON ファイル全体を毎回丸ごと読み書きして DB を実現
- B+Tree を実装せず、単なる辞書で永続化

---

## 10. 非対応機能と制限

以下は実装しません。

- SQLite 完全互換
- SQLite の実ファイルフォーマット互換
- SQL 標準完全準拠
- JOIN
- GROUP BY
- HAVING
- ORDER BY の一般実装
- サブクエリ
- トランザクション
- WAL
- インデックス
- 外部キー
- VIEW
- TRIGGER
- ALTER TABLE
- UPDATE
- VACUUM
- 複数プロセス同時書き込み
- ロック制御
- クエリオプティマイザ
- 型アフィニティの完全再現

### 制約

MVP では以下を実装しません。

- UNIQUE
- NOT NULL
- CHECK
- DEFAULT
- FOREIGN KEY
- 複合 PRIMARY KEY
- Overflow ページ
- Free list
- ページ再利用
- ページ圧縮
- WAL
- クラッシュリカバリ

---

## 11. 設計判断

### 識別子の大文字・小文字

- SQL キーワードは大文字・小文字を区別しない
- テーブル名、カラム名は大文字・小文字を区別する
- ただし、実装が簡単になる場合は、テーブル名