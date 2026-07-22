# local_sdlc.py Redis Benchmark Notes

Date: 2026-07-04

## Summary

`ai_coding_process_benchmark_spec.md` を題材に、`local_sdlc.py agent` が実装、テスト、失敗観測、修正、文書化まで進められるかを確認した。

対象成果物は `benchmarks/process-redis-kvs/` の Redis 互換ミニ KVS。

最終状態:

- `server.py`, `resp.py`, `store.py`, `commands.py` を agent 経由で作成・修正
- `tests/test_server.py` を agent 経由で作成・修正
- `README.md`, `PROCESS.md` を agent 経由で作成
- `python3 -m py_compile resp.py store.py commands.py server.py tests/test_server.py`: PASS
- `python3 -W error::ResourceWarning -m unittest discover -s tests`: 15 tests OK
- runner 内蔵 `redis-smoke`: PASS

## Runner Improvements Validated

今回のベンチで有効だった改善:

- `--context`: 読み取り専用 context を渡し、書き込み対象を `--include` / `--new-file` に限定できる。
- `--context-slice`: 大きいファイルや失敗ログの一部だけを渡せる。
- `--precheck`: Coder の前に失敗証拠を生成し、最初の実装に観測結果を渡せる。
- `--small-patch`: 既存ファイルの全文再生成ではなく、局所的な差分修正を促せる。
- `--no-replace-file`: 既存ファイルの丸ごと置換を拒否し、検索置換や diff へ誘導できる。
- `--resume-worktree`: 失敗 run の未反映 worktree を次 run の出発点として引き継げる。
- JSON artifact repair: `"type":"type":"search_replace"` のような軽微な重複を修復できる。

## Failure Patterns

### 1. Test Harness Bug Was Not Product Bug

`tests/test_server.py` の `_read_response` が固定長 `recv()` で bulk string を読むため、ヘッダと payload が混ざった。

対策:

- RESP のテスト読み取りは CRLF まで 1 byte ずつ読む。
- bulk string は header line で length を決め、payload と末尾 CRLF を分けて読む。
- product smoke が PASS している場合、テストハーネス側の不具合も疑う。

### 2. Invalid RESP Test Needed Protocol-Aware Observation

不正 RESP 送信後にすぐ `PING` を送ると、先に返る `-ERR` 応答を読み飛ばす。

対策:

- invalid input の後はまず `-ERR` を読む。
- その後、同一接続または新規接続で server survival を確認する。

### 3. Compound Test Commands Are Unsafe Without Shell Mode

`--test-command "python3 -m unittest discover -s tests && python3 -c ..."` は、runner が shell 経由ではなく `shlex.split()` で実行するため失敗した。

対策:

- 複数チェックは複数の `--test-command` に分ける。
- runner は shell 演算子を検出し、複数の `--test-command` に分けるよう明示エラーを返す。

### 4. Large Process Documents Cause Slow Local LLM Calls

PROCESS.md 作成時、run logs を多く渡すと API 死活は OK でも単一生成が長くなった。

対策:

- 大きい文書生成では context を要約または `--context-slice` で絞る。
- 失敗箇所だけを小さい agent run に分ける。
- 見出し修正のような作業は `--small-patch --no-replace-file` を使う。

## Operational Rules

- Code artifact generation and modification should be done through `local_sdlc.py agent`, not by manual editing of the target app.
- Runner, tests for runner, and docs may be edited directly by Codex when improving the agent foundation.
- Always keep writable targets narrow.
- Use read-only context for architecture files and prior run logs.
- Prefer multiple `--test-command` entries over shell composition.
- When a worktree run fails after producing a useful artifact, resume it with `--resume-worktree`.
- If the agent emits malformed or overlarge artifacts, reduce scope before retrying.

## Prompt Review Notes

2026-07-04 のプロンプト見直しでは、「変更すること」を目的にせず、Redis ベンチで実害が確認された点だけを反映した。

反映した点:

- `spec`, `architect`, `tdd`, `review`, `deploy`, `sdlc` の自動コミット指示を、コミット候補提示に変更した。git add / git commit はユーザーが明示的に依頼した場合のみ実行する。
- `tdd` に失敗原因分類を追加した。分類は `product_code`, `test_harness`, `runner_command`, `environment`, `missing_context`。
- `review` に実行証拠優先と失敗原因分類を追加した。Coder の自己評価は主張として扱い、テストログ・smoke・差分・SPEC.md と照合する。

変更しなかった点:

- DDD / UI / Observe / Security / Refactor の中核プロンプトは、今回の Redis ベンチで直接の失敗原因になっていないため変更しない。
- 元スキルセットの SPEC.md 駆動、固定要件、フェーズ分離、文書ベース引き継ぎの思想は維持する。

## Resulting Benchmark Project

Project path:

`benchmarks/process-redis-kvs/`

Important run dirs:

- `.sdlc-runner/runs/process-redis-resp-incomplete-none`
- `.sdlc-runner/runs/process-redis-tests-fix-harness`
- `.sdlc-runner/runs/process-redis-readme-approve`
- `.sdlc-runner/runs/process-redis-process-doc-fix-heading`
- `.sdlc-runner/runs/process-redis-tests-close-pipes`

---

## Mini SQLite Engine Benchmark Notes

Date: 2026-07-04

`mini_sqlite_engine_implementation_spec.md` を題材に、同じ agent 基盤でより大きい永続化エンジンを段階実装した。

対象成果物:

`benchmarks/mini-sqlite-engine/`

最終状態:

- Lexer / parser / record codec / pager / B+Tree / connection API / CLI / README を agent 経由で作成・修正
- `python3 -m compileall -q minisqlite tests`: PASS
- `python3 -m unittest discover -s tests`: 84 tests OK
- 実 CLI smoke:
  - `python3 -m minisqlite sample.db "CREATE TABLE ..."`: `OK`
  - `python3 -m minisqlite sample.db "INSERT ..."`: `OK`
  - `python3 -m minisqlite sample.db "SELECT * FROM users;"`: pipe-delimited rows
  - interactive `.tables`, `.schema users`, `.exit`: PASS

### Effective Decomposition

一括実装は 600 秒 timeout になったため、以下のように段階分解した。

- Stage 1A: SQL lexer
- Stage 1B: SQL parser
- Stage 1C: record codec
- Stage 2A: pager
- Stage 2B1: single-leaf BTree
- Stage 2B2: multi-leaf split/internal root
- Stage 3A: `connect()` + `CREATE TABLE` + schema catalog
- Stage 3B: `INSERT` / `SELECT` / `DELETE`
- Stage 4: CLI + README

この粒度ならローカル LLM でも実装・テスト・修正ループが成立した。

### Failure Patterns Found

#### 1. Large Stage Prompts Hang or Become Too Slow

Full SQLite spec, full B+Tree, full connection executor, CLI+README の一括生成は長時間化した。

対策:

- supervisor は大きな仕様を自動 stage 分割するべき。
- 1 stage は「新規ファイル1-3個、検証可能なテスト1群」程度に制限する。
- timeout だけでなく、過去 run の長時間化を次回 planning に反映する。

#### 2. False Approved When Acceptance Was Too Weak

Stage 2B2 では実装だけが変わり、split テストが追加されないまま既存テストで approved になった。

対策:

- `--require-path` だけでなく、成果物内容を確認する command gate を追加する。
- 新機能 stage では「テスト名/証拠文字列が存在すること」も command で検証する。
- Judge-less `command-only` では、acceptance command が仕様要求を直接観測している必要がある。

#### 3. Artifact Format Drift

LLM が `BEGIN_FILE` の後に path を別行で出し、さらに Markdown code fence を挟んだため runner が抽出できなかった。

対策:

- File artifact は `BEGIN_FILE: path` を command gate と prompt で強く要求する。
- `--require-path` を使い、ファイル未作成で approved にならないようにする。
- malformed artifact 発生時は、同じ大きい出力を再試行せず、対象ファイルをさらに分ける。

#### 4. Long JSON search_replace Is Fragile

BTree 修正で、正しい修正内容を含む JSON artifact が長すぎて JSON として壊れた。

対策:

- `--artifact-format legacy` を追加・活用し、長い multi-line patch では JSON を避ける。
- `local_sdlc.py` の legacy mode では JSON を推奨しない output contract に分岐した。
- small patch prompt に「300文字超の multi-line search/replace は BEGIN_SEARCH_REPLACE または unified diff」を追加した。

#### 5. Interrupted Runs Can Contain Useful Work But No run.json

長時間 run を Ctrl-C した場合、worktree と artifact は残るが `run.json` が無く、`--resume-worktree` できない。

対策:

- runner は中断時にも partial manifest を書くべき。
- `--resume-worktree-path` のような明示的な worktree 再開オプションが必要。
- 今回は agent 生成済み artifact を main に取り込み、直後に agent repair で承認状態まで持っていった。

### Agent Foundation Improvements Suggested

- Auto-slicer: 仕様と timeout/失敗履歴から stage を自動生成する。
- Acceptance-gate synthesizer: stage spec から `--test-command` と content gate を自動提案する。
- Partial-run manifest: interrupt/timeout 時も worktree path と artifact list を保存する。
- Artifact linter: LLM 出力直後に `BEGIN_FILE` 形式や JSON 長大化を検出し、同じ API call 内または次 round で形式修正を要求する。
- False-approved detector: `new-file` / `require-path` / spec required tests が未達なら command が pass しても approved にしない。
- Run-size advisor: 1 API call が長時間化した stage を docs に記録し、次回同種タスクで自動分割する。

### Implemented Runner Improvements After This Benchmark

2026-07-04 に、Mini SQLite で実際に発生した false approved / 中断復旧問題へ直接効く runner 改善を追加した。

- `--new-file` を自動的に required path gate に含める。
  - これにより、CLI/README stage のように新規ファイルが生成されないまま既存テストだけで approved になる問題を防ぐ。
  - manifest には `required_paths`, `explicit_required_paths`, `auto_required_paths` を記録する。
- `run.partial.json` を agent run 中に継続保存する。
  - 初期化、PM準備、round開始、coder output、apply、check、失敗分類、承認時に更新する。
  - Ctrl-C / timeout 時でも worktree path、changed paths、証拠、現在 round を復旧材料として残せる。
- `--resume-worktree-path` を追加した。
  - `run.json` が無い中断 worktree でも、明示 path から `--worktree-mode copy` で再開できる。
- `--resume` は `run.json` が無い場合に `run.partial.json` も読める。

追加テスト:

- `test_agent_new_file_is_automatically_required`
- `test_agent_writes_partial_manifest_on_patch_extraction_failure`
- `test_agent_resume_worktree_path_without_run_manifest`

確認:

- `python3 -m py_compile local_sdlc.py tests/test_local_sdlc.py`
- `python3 -m unittest discover -s tests`: 69 tests OK

### LLM / vLLM Doctor Improvements

2026-07-04 に Ornith-1.0-35B + OpenAI-compatible local API の推奨設定を再確認した。

External references checked:

- https://huggingface.co/deepreinforce-ai/Ornith-1.0-35B
  - Ornith-1.0-35B is documented as a reasoning model.
  - The model card recommends recent runtimes and vLLM serving with `--reasoning-parser qwen3`, `--enable-auto-tool-choice`, and `--tool-call-parser qwen3_xml` for OpenAI-style reasoning/tool-call extraction.
- https://docs.vllm.ai/en/latest/features/reasoning_outputs/
  - vLLM documents that Qwen3-series reasoning is enabled by default and can be disabled per request with `chat_template_kwargs.enable_thinking=false`.
- https://qwen.readthedocs.io/en/latest/deployment/vllm.html
  - Qwen/vLLM docs note the `qwen3` reasoning parser behavior and older incompatibilities around `enable_thinking=false`.

Observed local behavior:

- `python3 local_sdlc.py doctor --probe-timeout 30`
- Current model: `Ornith-1.0-35B`
- `chat_template_kwargs.enable_thinking=false` returns normal `message.content`.
- A default request without the thinking override can return reasoning-only output with empty `content`.
- JSON artifact generation with thinking disabled passed a compact JSON probe.

Implemented:

- `doctor` now runs short `/chat/completions` capability probes by default:
  - `no_thinking_content`
  - `default_thinking_behavior`
  - `json_artifact_content`
- `doctor --skip-probes` skips these probes.
- `doctor --probe-timeout N` controls each probe timeout.
- Ornith/Qwen-specific recommendations are printed when the selected model name matches.
- API call settings are now role-profiled:
  - PM / supervisor / spec: `temperature=0.2`, `max_tokens=8192`
  - coder: `temperature=0.1`, `max_tokens=65536`
  - judge: `temperature=0`, `max_tokens=8192`
- Per-role CLI overrides:
  - `--pm-max-tokens`, `--coder-max-tokens`, `--judge-max-tokens`
  - `--pm-temperature`, `--coder-temperature`, `--judge-temperature`
  - `--pm-thinking`, `--coder-thinking`, `--judge-thinking`
- `run.json` and `run.partial.json` record `llm_settings`.
- Mixed legacy artifacts are now applied together:
  - A coder output may contain both `BEGIN_SEARCH_REPLACE` and `BEGIN_FILE`.
  - The runner previously applied only search/replace artifacts in that case.
  - This was found during the fresh Mini SQLite retest and fixed.

Operational guidance:

- Keep `chat_template_kwargs.enable_thinking=false` for strict file/JSON artifact calls.
- Treat default reasoning-only responses as a warning, not proof that the API is dead.
- If enabling reasoning intentionally, the runner must consume `reasoning` / `reasoning_content` separately and still require final `content` for artifacts.
- Use coder-specific `--coder-max-tokens` for large implementation stages instead of globally increasing PM/judge output budgets.

Fresh Mini SQLite retest:

- Detailed report: `docs/mini_sqlite_fresh_retest_20260704.md`
- Result: one-shot full implementation still stalled; very small stages worked; missing-artifact and partial-manifest improvements were effective.
- Runner validation after mixed-artifact fix: `python3 -m unittest discover -s tests`: 70 tests OK

### Remaining Supervisor/Agent Issues To Consider

次に検討すべき課題:

- Spec-driven auto-slicing:
  - 仕様から stage spec を自動生成し、1 stage の新規ファイル数・受け入れ条件数・最大推定トークン数を制限する。
- Timeout-aware replanning:
  - API timeout / 長時間無応答を `task_too_large` として分類し、同じ prompt を再試行せず自動分割する。
- Content gates:
  - required path の存在だけでなく、`test_cli.py` に `test_...` がある、README に実行例がある、などの内容ゲートを自動生成する。
- Artifact linter:
  - `BEGIN_FILE` 形式崩れ、Markdown code fence 混入、長すぎる JSON search_replace を、apply 前に明示分類する。
- Stage queue:
  - supervisor が pending/running/passed/failed の stage queue を持ち、各 stage の run dir と証拠を管理する。
- Budget policy:
  - API call 秒数、max_tokens、context chars、round 数から「この stage は重い」と判断して次回計画へ反映する。
- Objective judge:
  - command-only だけでは仕様の観測範囲が弱い場合、review/judge が「acceptance gap」として差し戻す。

Important run dirs:

- `.sdlc-runner/runs/mini-sqlite-stage1a-lexer`
- `.sdlc-runner/runs/mini-sqlite-stage1b-parser-fix-type`
- `.sdlc-runner/runs/mini-sqlite-stage1c-record`
- `.sdlc-runner/runs/mini-sqlite-stage2a-pager`
- `.sdlc-runner/runs/mini-sqlite-stage2b1-btree-leaf-fix-cell-offset-legacy`
- `.sdlc-runner/runs/mini-sqlite-stage2b2-btree-split-add-tests`
- `.sdlc-runner/runs/mini-sqlite-stage3a-connect-create-repair-schema-page`
- `.sdlc-runner/runs/mini-sqlite-stage3b-insert-select-delete`
- `.sdlc-runner/runs/mini-sqlite-stage4-cli-repair-import-sys`
