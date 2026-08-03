# Mini SQLite Engine 実装仕様書

## 1. 目的

本仕様書は、AIに **SQLite風のミニデータベースエンジン** を実装させるための仕様を定義する。

この課題の目的は、単にSQL文字列を受け取って結果を返すプログラムを作ることではない。
以下のような、データベースエンジンの中核要素を小さな範囲で実装させることにより、AIの設計力・実装力・テスト設計力・改善力を評価する。

- SQLパース
- 実行計画
- スキーマ管理
- レコードエンコード
- B+Treeによる行管理
- ページ単位の永続化
- ファイル再オープン後のデータ復元
- エラー処理
- 境界条件テスト
- 失敗分析と改善ループ

本仕様で作るものは、SQLite完全互換ではない。
ただし、SQLiteの設計思想のうち、以下の要素を参考にする。

- 通常テーブルをrowid付きテーブルとして扱う
- rowidをB-tree/B+Treeのキーとして保存する
- データベースを単一ファイルに保存する
- SQLでテーブル作成・挿入・検索・削除を行う
- テストを重視し、境界条件と破損ケースも検証する

---

## 2. 作成するプログラム

### 2.1 名称

仮称は `MiniSQLite Engine` とする。

### 2.2 概要

`MiniSQLite Engine` は、単一ファイルにデータを保存する小型RDBMSである。

ユーザーはSQLを実行し、テーブル作成、行挿入、行検索、行削除を行える。

対象言語はPythonとする。
ただし、仕様上の概念は他言語にも移植できるように記述する。

---

## 3. 実装範囲

### 3.1 必須実装範囲

以下を必須とする。

1. CLIからSQLを実行できること
2. Python APIからSQLを実行できること
3. `CREATE TABLE` を実装すること
4. `INSERT INTO` を実装すること
5. `SELECT` を実装すること
6. `DELETE` を実装すること
7. `INTEGER` と `TEXT` 型を扱えること
8. `rowid` を内部的に持つこと
9. B+Treeで行を保存すること
10. データベースを単一ファイルに永続化すること
11. プロセス終了後、再起動してもデータを読めること
12. テストコードを作成すること
13. READMEを作成すること
14. 初回実装、レビュー、改善後実装のループを行うこと

### 3.2 実装しない範囲

以下は実装しない。

- SQLite完全互換
- SQLiteの実ファイルフォーマット互換
- SQL標準完全準拠
- JOIN
- GROUP BY
- HAVING
- ORDER BYの一般実装
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

---

## 4. 実行方法

### 4.1 CLI起動

以下で対話モードを起動できること。

```bash
python -m minisqlite sample.db
```

または、単一SQLを指定して実行できること。

```bash
python -m minisqlite sample.db "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
python -m minisqlite sample.db "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
python -m minisqlite sample.db "SELECT * FROM users;"
```

### 4.2 対話モード

対話モードでは以下のように利用できること。

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

### 4.3 Python API

以下のように利用できること。

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

## 5. SQL仕様

### 5.1 対応する文

必須対応するSQL文は以下である。

- `CREATE TABLE`
- `INSERT INTO`
- `SELECT`
- `DELETE`

### 5.2 識別子

テーブル名、カラム名は以下を満たすこと。

```text
[A-Za-z_][A-Za-z0-9_]*
```

大文字・小文字の扱いは以下とする。

- SQLキーワードは大文字・小文字を区別しない
- テーブル名、カラム名は大文字・小文字を区別する
- ただし、実装が簡単になる場合は、テーブル名とカラム名も小文字正規化してよい
- 小文字正規化する場合はREADMEに明記すること

### 5.3 リテラル

以下のリテラルを扱う。

#### INTEGER

```sql
123
-10
0
```

64bit符号付き整数の範囲を想定する。

#### TEXT

```sql
'hello'
'Alice'
'It''s OK'
```

シングルクォートで囲む。
文字列中のシングルクォートは `''` で表す。

#### NULL

MVPでは `NULL` は任意実装とする。
実装する場合、内部型として扱うこと。

---

## 6. CREATE TABLE仕様

### 6.1 基本文法

```sql
CREATE TABLE table_name (
  column_name column_type [PRIMARY KEY],
  column_name column_type,
  ...
);
```

### 6.2 対応する型

| 型 | 内容 |
|---|---|
| INTEGER | 64bit符号付き整数 |
| TEXT | UTF-8文字列 |

### 6.3 PRIMARY KEY

MVPでは以下のみ対応する。

```sql
id INTEGER PRIMARY KEY
```

この場合、`id` は内部rowidの別名として扱う。

例：

```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  name TEXT,
  age INTEGER
);
```

### 6.4 rowid

すべてのテーブルは内部的に `rowid` を持つ。

- `INTEGER PRIMARY KEY` がある場合、そのカラムをrowidとして使う
- `INTEGER PRIMARY KEY` がない場合、内部rowidを自動採番する
- 自動採番は、当該テーブルの最大rowid + 1 とする
- rowidはB+Treeのキーとして使う

### 6.5 制約

MVPでは以下は実装しない。

- UNIQUE
- NOT NULL
- CHECK
- DEFAULT
- FOREIGN KEY
- 複合PRIMARY KEY

---

## 7. INSERT仕様

### 7.1 基本文法

```sql
INSERT INTO table_name (column1, column2, ...) VALUES (value1, value2, ...);
```

カラムリストなしの形式も対応する。

```sql
INSERT INTO table_name VALUES (value1, value2, ...);
```

### 7.2 挙動

- 指定されたテーブルが存在しない場合はエラー
- カラム数と値の数が一致しない場合はエラー
- 存在しないカラムが指定された場合はエラー
- 型が不一致の場合はエラー
- rowidが重複する場合はエラー
- rowid未指定の場合は自動採番する

### 7.3 例

```sql
INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);
INSERT INTO users (name, age) VALUES ('Bob', 25);
```

---

## 8. SELECT仕様

### 8.1 基本文法

```sql
SELECT column_list FROM table_name [WHERE condition];
```

### 8.2 対応するcolumn_list

```sql
SELECT * FROM users;
SELECT id, name FROM users;
```

### 8.3 WHERE条件

MVPでは単一条件のみ対応する。

```sql
WHERE column_name = literal
WHERE column_name != literal
WHERE column_name > literal
WHERE column_name >= literal
WHERE column_name < literal
WHERE column_name <= literal
```

### 8.4 比較仕様

- INTEGER同士は数値比較
- TEXT同士は辞書順比較
- 型が異なる比較はエラー、または一致しないものとして扱う
- どちらを採用したかREADMEに明記すること

### 8.5 例

```sql
SELECT * FROM users;
SELECT name FROM users WHERE age >= 20;
SELECT id, name FROM users WHERE name = 'Alice';
```

### 8.6 出力順

MVPでは、SELECT結果はrowid昇順とする。

---

## 9. DELETE仕様

### 9.1 基本文法

```sql
DELETE FROM table_name WHERE condition;
```

### 9.2 制約

安全のため、MVPでは `WHERE` なしのDELETEは禁止する。

以下はエラーとする。

```sql
DELETE FROM users;
```

全件削除をしたい場合は、将来拡張として `DELETE FROM users WHERE rowid >= 0;` のような条件を使う。

### 9.3 挙動

- 条件に一致した行を削除する
- 削除後、同じrowidでGET/SELECTされないこと
- B+Treeのページマージは必須ではない
- 削除により空きができても再利用は任意
- 削除数を結果として返す

---

## 10. スキーマ管理

### 10.1 内部スキーマテーブル

内部的に以下のようなスキーマ情報を保持する。

| 項目 | 内容 |
|---|---|
| type | `table` |
| name | テーブル名 |
| root_page | 当該テーブルのB+Tree root page id |
| sql | CREATE TABLE文 |
| columns | カラム定義 |
| rowid_column | rowid別名カラム名、なければnull |

### 10.2 保存方法

スキーマ情報もデータベースファイル内に保存する。

実装方式は以下のどちらかを選べる。

#### 方式A: 固定ヘッダ領域にスキーマJSONを保存

MVPでは実装しやすい。
ただし、スキーマが大きくなりすぎる問題がある。

#### 方式B: スキーマ専用B+Treeに保存

より望ましい。
通常テーブルと同じ仕組みを使うため、設計の一貫性が高い。

推奨は方式B。
ただし、初回実装では方式A、改善後実装では方式Bとしてもよい。

---

## 11. ファイル形式

### 11.1 基本方針

本仕様では、SQLite本体のファイルフォーマットとは互換にしない。
ただし、以下の考え方は採用する。

- 単一ファイル
- 固定長ページ
- ページ単位の読み書き
- B+Treeページ
- rowidをキーとするテーブル格納

### 11.2 ページサイズ

ページサイズは4096バイト固定とする。

```text
PAGE_SIZE = 4096
```

### 11.3 ファイルヘッダ

ファイル先頭のページ0をデータベースヘッダページとする。

#### ヘッダページ構造

| オフセット | サイズ | 内容 |
|---:|---:|---|
| 0 | 8 | magic bytes: `MSQLITE1` |
| 8 | 4 | page_size: 4096 |
| 12 | 4 | format_version: 1 |
| 16 | 4 | next_page_id |
| 20 | 4 | schema_root_page |
| 24 | 8 | reserved |
| 32 | 可変 | schema metadata または空き |

すべての整数はbig-endianで保存する。

### 11.4 ページID

- ページIDは0始まり
- page 0はヘッダ専用
- page 1以降をB+Treeページとして使う
- 新規ページは `next_page_id` から採番する

---

## 12. B+Tree仕様

### 12.1 目的

B+Treeは、テーブルの行をrowid順に管理するために使う。

MVPでは、各テーブルごとに1つのB+Treeを持つ。

### 12.2 キー

キーはrowidである。

```text
key = rowid: signed 64-bit integer
```

### 12.3 値

値はレコードペイロードである。

```text
value = encoded record bytes
```

### 12.4 ページ種別

以下のページ種別を持つ。

| 種別 | 値 | 内容 |
|---|---:|---|
| LEAF | 1 | rowidとレコードを保持する |
| INTERNAL | 2 | 子ページへの参照を保持する |

### 12.5 Leafページ

Leafページは実データを保持する。

#### Leafページヘッダ

| オフセット | サイズ | 内容 |
|---:|---:|---|
| 0 | 1 | page_type = 1 |
| 1 | 1 | is_root |
| 2 | 2 | cell_count |
| 4 | 4 | right_sibling_page_id |
| 8 | 4 | parent_page_id |
| 12 | 2 | cell_area_start |
| 14 | 2 | reserved |

ヘッダサイズは16バイトとする。

#### Leafセル

Leafセルは以下を持つ。

| フィールド | サイズ |
|---|---:|
| rowid | 8 bytes |
| payload_size | 4 bytes |
| payload | variable |

セルはrowid昇順に並べる。

### 12.6 Internalページ

Internalページは子ページへの参照を保持する。

#### Internalページヘッダ

| オフセット | サイズ | 内容 |
|---:|---:|---|
| 0 | 1 | page_type = 2 |
| 1 | 1 | is_root |
| 2 | 2 | key_count |
| 4 | 4 | parent_page_id |
| 8 | 4 | rightmost_child_page_id |
| 12 | 4 | reserved |

ヘッダサイズは16バイトとする。

#### Internalセル

| フィールド | サイズ |
|---|---:|
| child_page_id | 4 bytes |
| max_key_in_child | 8 bytes |

Internalページでは、各セルが左側の子ページとその最大キーを示す。

### 12.7 探索

rowidを検索するときは以下のように動く。

```text
root pageを読む
↓
Internalならkeyを比較して子ページへ進む
↓
Leafに到達する
↓
Leaf内を二分探索または線形探索する
```

### 12.8 挿入

挿入時は以下のように動く。

```text
挿入先Leafを探す
↓
rowid重複を確認する
↓
Leafに空きがあれば挿入する
↓
空きがなければLeafを分割する
↓
親Internalに分割情報を伝播する
↓
rootが分割された場合、新しいrootを作る
```

### 12.9 削除

MVPでは簡略化する。

- Leafから該当セルを削除する
- ページのマージや再分配は行わない
- Internalページのキー更新は必要に応じて行う
- 実装が難しい場合、削除後に当該テーブルのB+Treeを再構築してもよい
- 採用した方式をREADMEに明記する

### 12.10 制約

MVPでは以下を実装しない。

- Overflowページ
- Free list
- ページ再利用
- ページ圧縮
- WAL
- クラッシュリカバリ

---

## 13. レコード形式

### 13.1 基本方針

1行のデータは、カラム順に値を並べたバイナリレコードとして保存する。

### 13.2 レコード構造

```text
column_count: 2 bytes
value_1
value_2
...
value_n
```

### 13.3 値の型

各値は以下の形式で保存する。

#### NULL

```text
type: 1 byte = 0
```

#### INTEGER

```text
type: 1 byte = 1
value: 8 bytes signed big-endian
```

#### TEXT

```text
type: 1 byte = 2
length: 4 bytes
utf8 bytes: length bytes
```

### 13.4 型チェック

- INSERT時にスキーマ型と値型を検証する
- `INTEGER PRIMARY KEY` カラムは必ずINTEGERでなければならない
- TEXTに数値を自動変換しない
- INTEGERに文字列を自動変換しない

---

## 14. コンポーネント設計

### 14.1 推奨ファイル構成

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

### 14.2 `sql/lexer.py`

SQL文字列をトークン列に変換する。

担当範囲：

- キーワード
- 識別子
- 数値
- 文字列
- 記号
- 比較演算子
- セミコロン

### 14.3 `sql/parser.py`

トークン列をASTに変換する。

担当範囲：

- CREATE TABLE文
- INSERT文
- SELECT文
- DELETE文
- WHERE条件

### 14.4 `sql/ast.py`

ASTノードを定義する。

例：

- `CreateTable`
- `Insert`
- `Select`
- `Delete`
- `ColumnDef`
- `Condition`
- `Literal`

### 14.5 `engine/schema.py`

スキーマ情報を管理する。

担当範囲：

- テーブル定義の登録
- テーブル存在確認
- カラム存在確認
- rowidカラム判定
- スキーマの永続化・復元

### 14.6 `engine/executor.py`

ASTを実行する。

担当範囲：

- CREATE TABLE実行
- INSERT実行
- SELECT実行
- DELETE実行
- 型チェック
- WHERE評価
- Result生成

### 14.7 `storage/pager.py`

ページ単位のファイル読み書きを担当する。

担当範囲：

- DBファイル作成
- ヘッダ読み書き
- ページ読み込み
- ページ書き込み
- 新規ページ確保
- close時のflush

### 14.8 `storage/btree.py`

B+Treeを実装する。

担当範囲：

- search
- insert
- delete
- scan_all
- split leaf
- split internal
- root更新

### 14.9 `storage/record.py`

レコードのエンコード・デコードを担当する。

担当範囲：

- Python値からbytesへ変換
- bytesからPython値へ復元
- 型タグ管理
- 破損データ検出

### 14.10 `connection.py`

ユーザー向けAPIを提供する。

例：

```python
conn = connect("sample.db")
conn.execute("SELECT * FROM users;")
conn.close()
```

---

## 15. エラー仕様

### 15.1 エラー種別

以下の例外クラスを定義する。

```python
class MiniSQLiteError(Exception): ...
class SQLSyntaxError(MiniSQLiteError): ...
class SchemaError(MiniSQLiteError): ...
class TypeMismatchError(MiniSQLiteError): ...
class DuplicateKeyError(MiniSQLiteError): ...
class StorageError(MiniSQLiteError): ...
class CorruptDatabaseError(StorageError): ...
```

### 15.2 エラー方針

- SQL構文エラーは `SQLSyntaxError`
- 存在しないテーブルは `SchemaError`
- 存在しないカラムは `SchemaError`
- 型不一致は `TypeMismatchError`
- rowid重複は `DuplicateKeyError`
- ファイルヘッダ不正は `CorruptDatabaseError`
- ページ種別不正は `CorruptDatabaseError`

CLIでは、エラーを以下のように表示する。

```text
ERROR: <message>
```

プロセス全体が落ちないこと。

---

## 16. テスト仕様

### 16.1 必須テスト

以下のテストを必須とする。

#### SQL Lexer

- キーワードを認識できる
- 識別子を認識できる
- 数値リテラルを認識できる
- 文字列リテラルを認識できる
- `It''s OK` を正しく扱える
- 不正な文字列終端をエラーにできる

#### SQL Parser

- CREATE TABLEをパースできる
- INSERTをパースできる
- SELECT * をパースできる
- SELECT column list をパースできる
- WHERE条件をパースできる
- DELETEをパースできる
- 不正構文をエラーにできる

#### Record Codec

- INTEGERを保存・復元できる
- TEXTを保存・復元できる
- 複数カラムを保存・復元できる
- UTF-8日本語を保存・復元できる
- 破損したpayloadを検出できる

#### Pager

- 新規DBファイルを作成できる
- ヘッダを書き込める
- ページを書き込める
- ページを読み戻せる
- 不正magic bytesを検出できる

#### B+Tree

- 空のB+Treeから検索すると見つからない
- 1件挿入して検索できる
- 複数件をrowid順にscanできる
- rowid重複を拒否できる
- ページ容量を超える挿入でleaf splitできる
- root splitできる
- 削除できる
- 削除後に検索で見つからない
- 大量挿入後も全件検索できる

#### SQL Execution

- CREATE TABLEできる
- INSERTできる
- SELECTできる
- WHEREで絞り込める
- DELETEできる
- 型不一致でエラーになる
- 存在しないテーブルでエラーになる
- 存在しないカラムでエラーになる

#### Persistence

- INSERT後にcloseし、再openしてSELECTできる
- 複数テーブルのデータを再open後も読める
- B+Tree分割後のデータを再open後も読める
- 削除後に再openしても削除状態が維持される

#### CLI

- 単一SQLを実行できる
- 対話モードで複数SQLを実行できる
- `.tables` が動く
- `.schema table_name` が動く
- `.exit` で終了できる

### 16.2 テストデータ例

```sql
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);
INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25);
INSERT INTO users (id, name, age) VALUES (3, 'Carol', 41);
SELECT * FROM users;
SELECT name FROM users WHERE age >= 30;
DELETE FROM users WHERE id = 2;
SELECT * FROM users;
```

期待結果：

```text
SELECT * FROM users;
id|name|age
1|Alice|30
2|Bob|25
3|Carol|41

SELECT name FROM users WHERE age >= 30;
name
Alice
Carol

DELETE FROM users WHERE id = 2;
deleted: 1

SELECT * FROM users;
id|name|age
1|Alice|30
3|Carol|41
```

---

## 17. 品質要件

### 17.1 コード品質

- 責務を分離すること
- 型ヒントを付けること
- 例外を握りつぶさないこと
- テストしやすい設計にすること
- ファイルI/OとSQL実行を密結合させないこと
- READMEに設計判断を書くこと

### 17.2 実装上の禁止事項

以下は禁止する。

- Python標準の `sqlite3` モジュールを使うこと
- 既存のSQLパーサライブラリを使うこと
- SQLAlchemyなどのORMを使うこと
- dbm、shelveなどの既存KVSを内部ストレージとして使うこと
- JSONファイル全体を毎回丸ごと読み書きしてDBを実現すること
- B+Treeを実装せず、単なる辞書で永続化すること
- テストを省略すること
- READMEを省略すること
- 失敗や未実装を隠すこと

---

## 18. 開発プロセス要件

AIは以下の順序で作業すること。

```text
1. 仕様理解メモ
2. 実装範囲の確認
3. 初期設計
4. ファイル構成設計
5. SQL文法設計
6. ファイル形式設計
7. B+Tree設計
8. 初回実装
9. テスト設計
10. 初回レビュー
11. 失敗・リスク分析
12. 開発プロセス改善ルール作成
13. 改善後の実装
14. 最終品質レビュー
15. 残課題と次の改善ループ案
```

---

## 19. AIへの最終出力形式

AIは以下の順で出力すること。

```text
1. 仕様理解メモ
2. 初期設計
3. ファイル構成
4. SQL仕様の解釈
5. ファイル形式の設計
6. B+Tree設計
7. 初回実装
8. テスト設計
9. 初回レビュー
10. 開発プロセス改善ルール
11. 改善後の実装
12. 最終品質レビュー
13. 残課題と次の改善ループ案
```

コードブロックには必ずファイル名を付ける。

例：

```python
# minisqlite/storage/btree.py
...
```

---

## 20. 初回レビューで必ず確認する項目

初回実装後、以下の分類で自己レビューすること。

```text
A. 仕様理解の問題
B. SQLパース設計の問題
C. B+Tree設計の問題
D. ファイル永続化の問題
E. レコード形式の問題
F. エラー処理の問題
G. テスト不足
H. 保守性の問題
I. 性能・スケーラビリティの問題
```

各項目について、以下の形式で記述する。

```text
問題:
原因:
影響:
再発防止策:
修正方針:
```

---

## 21. 開発プロセス改善ルールの例

初回レビュー後、以下のような改善ルールを作成する。

```text
1. SQLパーサは実装前にEBNFを書いてから実装する
2. ファイル形式は先に表として固定し、コード中に定数化する
3. B+Treeのsplitは図解してから実装する
4. 永続化テストは必ずclose/reopenを含める
5. 境界条件テストを実装前に列挙する
6. DELETEはページマージなしでよいが、探索不整合を起こさないことをテストする
7. rowidの扱いは全コンポーネントで統一する
8. エラーを曖昧なExceptionではなく専用例外に分類する
9. READMEには未実装範囲を明記する
10. 改善後実装では、初回レビューの問題に対応した差分を説明する
```

---

## 22. 最終品質レビュー形式

最後に、以下の形式で自己評価する。

| 評価項目 | 自己評価 | 根拠 | 残課題 |
|---|---|---|---|
| SQLパース |  |  |  |
| CREATE TABLE |  |  |  |
| INSERT |  |  |  |
| SELECT |  |  |  |
| DELETE |  |  |  |
| 型チェック |  |  |  |
| rowid管理 |  |  |  |
| B+Tree探索 |  |  |  |
| B+Tree挿入 |  |  |  |
| B+Tree分割 |  |  |  |
| 永続化 |  |  |  |
| 再オープン後復元 |  |  |  |
| エラー処理 |  |  |  |
| テスト網羅性 |  |  |  |
| README品質 |  |  |  |
| 保守性 |  |  |  |

---

## 23. 受け入れ条件

以下を満たした場合、MVP完了とする。

1. `CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);` が成功する
2. `INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);` が成功する
3. `SELECT * FROM users;` で挿入した行が返る
4. `SELECT name FROM users WHERE age >= 30;` が正しく動く
5. `DELETE FROM users WHERE id = 1;` が成功する
6. 削除後にSELECTしても対象行が返らない
7. DBファイルを閉じて再度開いてもデータが残っている
8. 100件以上のINSERTでB+Treeのページ分割が発生し、全件検索できる
9. 不正SQLで適切なエラーが出る
10. 不正なDBファイルを開いた場合に破損エラーを出せる
11. テストコードが存在する
12. READMEが存在する
13. 初回レビューと改善後実装が存在する

---

## 24. 追加の望ましい要件

必須ではないが、実装できると望ましい。

- `UPDATE`
- `ORDER BY rowid`
- `LIMIT`
- `COUNT(*)`
- `CREATE INDEX`
- Secondary index
- Free list
- Page cache
- Transaction
- Rollback journal
- WAL
- File lock
- Fuzz test
- Property-based test
- Benchmark
- GitHub Actions CI
- Dockerfile

---

## 25. 参考資料

本実装はSQLite完全互換ではないが、以下の公式資料を背景知識として参考にする。

- SQLite Database File Format
  https://sqlite.org/fileformat.html

- SQLite CREATE TABLE
  https://www.sqlite.org/lang_createtable.html

- SQLite INSERT
  https://sqlite.org/lang_insert.html

- SQLite SELECT
  https://www.sqlite.org/lang_select.html

- SQLite Rowid Tables
  https://www.sqlite.org/rowidtable.html

- How SQLite Is Tested
  https://sqlite.org/testing.html

---

## 26. 本仕様の狙い

この課題は、Redis互換ミニサーバーよりも難度が高い。

Redis互換ミニサーバーでは、主に以下を評価できる。

- TCPサーバー
- プロトコル処理
- KVS
- TTL
- 並行接続

一方、Mini SQLite Engineでは、さらに以下を評価できる。

- SQLという高レベル言語の解釈
- AST設計
- 型システム
- スキーマ管理
- ディスク永続化
- ページ設計
- B+Treeの構造理解
- 再起動後の整合性
- 複雑なテスト設計
- 長期的な保守性

したがって、この仕様の本質は、単に「小さなSQLiteを作る」ことではない。

本質的な評価対象は、AIが以下を実行できるかである。

- 複雑な仕様を分解する
- 実装範囲を現実的に絞る
- ファイル形式を明文化する
- SQLパーサと実行系を分離する
- B+Treeの不変条件を守る
- 永続化の境界条件をテストする
- 自分の失敗を分類する
- 開発手順を改善する
- 改善内容を次の実装に反映する

この仕様では、最終的なコードだけでなく、
**開発プロセスそのものを成果物として扱う。**
