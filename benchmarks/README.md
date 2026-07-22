# Benchmarks

このディレクトリは、Local SDLC Agent の評価に使った仕様、生成成果物、実験メモを保存する場所です。
ここにあるコードはアプリ本体ではなく、Agent の挙動を検証するための evidence です。

## Layout

| パス | 用途 |
|---|---|
| `runs/<domain>/<run-id>/` | 現在の整理方針に沿った実行成果物 |
| `mini-sqlite-engine/` | Mini SQLite の基準ベンチマーク/仕様断片 |
| `process-redis-kvs/`, `redis-kvs/` | Redis KVS 系のベンチマーク成果物 |
| ルート直下の日時付きディレクトリ | 既存の legacy 実験。今後は新規追加を `runs/` に寄せる |

## Rules

- 成功・失敗の判断に使った要約は `RUN_SUMMARY.md` などの小さい文書として残す。
- `.sdlc-runner/` の大量ログは通常コミットしない。必要な場合は要約または抜粋を文書化する。
- 生成された benchmark code は Agent の能力検証用 evidence として扱い、`local_sdlc/` の製品コードと混同しない。
- 新規実験は `benchmarks/runs/<domain>/<model-or-purpose>-<date>/` に作る。

