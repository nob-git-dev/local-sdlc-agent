# ローカルLLMで動く「開発エージェント」を使う方法

この記事では、`Local SDLC Agent` というプログラムの使い方を、できるだけ専門用語を減らして説明します。

このプログラムは、ひとことで言うと、

> ローカルLLMにプログラムを作らせるための「開発の進行役」

です。

ただし、単にAIへ「コードを書いて」と丸投げする道具ではありません。

人間が書いた仕様書をもとに、AIが計画し、コードを書き、テストし、失敗したら原因を分析して、もう一度直す。そういう流れをターミナルから動かすためのプログラムです。

---

## まず全体像

通常のAIコーディングは、こうなりがちです。

```text
人間「これ作って」
AI「できました」
人間「本当に動く？」
AI「たぶん動きます」
```

このプログラムでは、もう少し開発チームっぽく分けます。

```text
人間
  ↓
Supervisor: 何を作るべきか整理する
  ↓
PM: 仕様やゴールを整理する
  ↓
Coder: コードを書く
  ↓
Judge: 本当に条件を満たしたか確認する
  ↓
失敗したら Failure Analysis: 原因を整理して次の修正へ
```

ポイントは、各役割を別々のAPI呼び出しとして実行することです。

同じAIにずっと会話させるのではなく、役割ごとにシステムプロンプトを分けて、文書を渡しながら進めます。

つまり、このプログラムは「AIに考えさせる」だけでなく、「AIの仕事を監督する仕組み」でもあります。

---

## このプログラムでできること

主に次のことができます。

- ローカルLLM APIが動いているか確認する
- 使えるSDLCスキルを一覧表示する
- 仕様書 `SPEC.md` を作る
- 仕様書をもとに開発ステージを分ける
- AIにコードを書かせる
- テストを実行する
- 失敗したら修正ループを回す
- 実行ログをMarkdownやJSONで残す
- モデルごとに設定を切り替える

ここで重要なのは、作業の記録が残ることです。

実行すると `.sdlc-runner/runs/` の中に、PMの判断、Coderの出力、Judgeのレビュー、テスト結果などが保存されます。

あとから「なぜこのコードになったのか」を追えます。

---

## 前提として必要なもの

使う前に、次のものが必要です。

- LinuxやmacOSなどのターミナル
- Python 3
- このリポジトリのファイル一式
- OpenAI互換APIとして動くローカルLLM

このプログラムは、標準では次のURLにLLM APIがある前提で動きます。

```text
http://localhost:30000/v1
```

ローカルLLMをOpenAI互換APIとして起動している場合、この形式になっていることが多いです。

別のURLを使う場合は、コマンドに `--base-url` を付けます。

```bash
python3 local_sdlc.py health --base-url http://localhost:30000/v1
```

---

## 最初にやる確認

まず、リポジトリの場所へ移動します。

```bash
cd ~/projects/claude-skills
```

次に、LLMなしでプログラム自体の構成を確認します。

```bash
python3 local_sdlc.py doctor --skip-llm
```

これは「必要なファイルがあるか」「スキルが読めるか」を確認するコマンドです。

次に、ローカルLLM APIが生きているか確認します。

```bash
python3 local_sdlc.py health
```

うまくいくと、だいたい次のような意味の表示になります。

```text
llm_health: alive
```

もし `unreachable` のように出たら、LLMサーバーが起動していないか、URLが違います。

最後に、読み込めるスキルを確認します。

```bash
python3 local_sdlc.py list-skills
```

`spec`、`architect`、`tdd`、`review` などが出ていれば、SDLCスキルが読めています。

---

## いちばん安全な考え方

初心者のうちは、次の順番で使うのがおすすめです。

1. まず `doctor` で状態確認
2. 次に `health` でLLM確認
3. いきなり大きいアプリを作らせない
4. 小さなファイル1つから試す
5. `--apply` を付ける時は、ファイルが実際に書き換わると理解する
6. 慣れるまでは `--worktree-mode copy` を使う

`--apply` は「AIが作った変更を実際にファイルへ適用する」という意味です。

`--worktree-mode copy` は、作業用コピーで試して、成功したものだけ戻すための安全装置です。

---

## 例1: 仕様書だけ作る

まずはコードを書かせず、仕様書だけ作ってみます。

作業用フォルダを作ります。

```bash
mkdir -p work/hello-agent
```

仕様書を作ります。

```bash
python3 local_sdlc.py spec "Pythonで動く簡単な挨拶プログラムを作る。hello.py を実行すると挨拶を表示する。" \
  --project work/hello-agent \
  --model-profile qwen-agent \
  --stream \
  --apply
```

成功すると、次のファイルができます。

```text
work/hello-agent/SPEC.md
```

`SPEC.md` は、この開発の約束事です。

AIにとっての「作るべきものの説明書」になります。

---

## 例2: 小さなプログラムを作らせる

次に、1ファイルだけ作らせます。

```bash
python3 local_sdlc.py agent "SPEC.mdに従って、hello.py を作成する" \
  --project work/hello-agent \
  --spec-file SPEC.md \
  --new-file hello.py \
  --require-path hello.py \
  --apply \
  --worktree-mode copy \
  --test-command "python3 -m py_compile hello.py" \
  --model-profile qwen-agent \
  --stream \
  --max-rounds 4
```

このコマンドの意味を分解すると、こうです。

- `agent`: コード作成、適用、テスト、修正を行う
- `--project work/hello-agent`: 作業対象のフォルダ
- `--spec-file SPEC.md`: 仕様書を読む
- `--new-file hello.py`: 新しく作るファイル
- `--require-path hello.py`: 完成条件として、このファイルが必要
- `--apply`: 実際にファイルへ反映する
- `--worktree-mode copy`: 安全のため一時コピーで作業する
- `--test-command "python3 -m py_compile hello.py"`: 作ったあとに実行する構文チェック
- `--max-rounds 4`: 最大4回まで修正する

実行後、次のような表示が出ます。

```text
run_dir: ...
final_verdict: approved
```

`approved` なら、エージェントが「条件を満たした」と判断した状態です。

---

## 例3: 仕様書から段階的に作らせる

少し大きなプログラムでは、いきなり全部作らせるより、ステージに分ける方が安定します。

ステージとは、たとえばこういう単位です。

```text
S01: エラー型と基本データ構造
S02: 入力の読み取り
S03: パーサー
S04: 保存処理
S05: CLI
```

まず、仕様書からステージ計画だけ見ます。

```bash
python3 local_sdlc.py stage-plan \
  --project work/hello-agent \
  --spec-file SPEC.md \
  --format markdown
```

実際にステージ実行する場合は `run-stages` を使います。

```bash
python3 local_sdlc.py run-stages "SPEC.mdに従って段階的に実装する" \
  --project work/hello-agent \
  --spec-file SPEC.md \
  --model-profile qwen-agent \
  --stream \
  --apply \
  --worktree-mode copy \
  --stage-max-rounds 5 \
  --test-command "python3 -m unittest discover -s tests -v"
```

`tests` フォルダがまだない小さなプロジェクトでは、最初は `--test-command` を省略しても構いません。

大きな開発では、この `run-stages` が中心になります。

AIが一度に全部作るのではなく、小さなゴールに分けて、各ステージごとにテストと修正を行います。

---

## ログの見方

実行すると、画面に `run_dir` が表示されます。

例:

```text
run_dir: /home/xxx/project/.sdlc-runner/runs/20260722-120000
```

このフォルダを見ると、実行の証拠が残っています。

よく見るファイルは次の通りです。

- `run.json`: 実行全体のまとめ
- `01-pm-control.md`: PMやSupervisorの判断
- `02-r01-coder-output.md`: Coderの出力
- `03-r01-...`: 適用された変更パッチや生成ファイル
- `04-r01-apply.md`: 適用結果
- `05-r01-command-01.md`: テストコマンドの結果
- `05-r01-failure-analysis.json`: 失敗分析
- `06-r01-judge-review.md`: Judgeのレビュー

初心者は、まず `run.json` と `05-r01-command-01.md` を見れば十分です。

テストが失敗している場合は、`failure-analysis` や `repair-advice` という名前のファイルを見ると、次に何を直そうとしたか分かります。

---

## モデル設定の考え方

このプログラムでは、モデルごとの設定を `--model-profile` で切り替えます。

Qwenを使う場合は、まずこれでよいです。

```bash
--model-profile qwen-agent
```

より長く考えさせたい場合は、次のようなprofileもあります。

```bash
--model-profile qwen-agent-deep
```

ただし、深い設定にすると時間が長くなることがあります。

まずは `qwen-agent` で試し、失敗が続く時だけ `qwen-agent-deep` を試すのが現実的です。

---

## よく使うコマンド一覧

状態確認:

```bash
python3 local_sdlc.py doctor --skip-llm
python3 local_sdlc.py health
```

使えるスキルを見る:

```bash
python3 local_sdlc.py list-skills
```

仕様書を作る:

```bash
python3 local_sdlc.py spec "作りたいものを書く" --project work/my-app --apply
```

1つの開発ループを回す:

```bash
python3 local_sdlc.py agent "作業内容を書く" \
  --project work/my-app \
  --spec-file SPEC.md \
  --apply \
  --worktree-mode copy \
  --max-rounds 4
```

ステージ分割して実行する:

```bash
python3 local_sdlc.py run-stages "SPEC.mdに従って実装する" \
  --project work/my-app \
  --spec-file SPEC.md \
  --apply \
  --worktree-mode copy
```

過去runを比較する:

```bash
python3 local_sdlc.py compare-runs path/to/run1 path/to/run2
```

---

## エラーが出た時の見方

### `llm_health: unreachable`

ローカルLLM APIに接続できていません。

確認すること:

- LLMサーバーが起動しているか
- URLが `http://localhost:30000/v1` で合っているか
- 別URLなら `--base-url` を付けているか

### `TimeoutError` や timeout 表示

LLMの応答が遅すぎる状態です。

対処:

- `--stream` を付ける
- `--timeout` を長くする
- `--max-rounds` を小さくする
- タスクを小さくする
- `run-stages` で段階分割する

例:

```bash
python3 local_sdlc.py agent "小さな修正内容を書く" \
  --project work/my-app \
  --stream \
  --timeout 900
```

### テストが失敗する

テスト失敗は悪いことではありません。

むしろ、このプログラムはテスト失敗を見て修正するためにあります。

見る場所:

```text
.sdlc-runner/runs/.../05-rXX-command-01.md
.sdlc-runner/runs/.../05-rXX-failure-analysis.json
.sdlc-runner/runs/.../05-rXX-repair-advice.md
```

同じ失敗が何度も続く場合は、タスクが大きすぎるか、仕様が曖昧な可能性があります。

---

## 初心者におすすめの運用ルール

最初はこのルールで使うと安全です。

1. いきなり本番コードに使わない
2. まず `work/` や `benchmarks/runs/` のような実験フォルダで試す
3. `--apply` を付ける前に、何が書き換わるか理解する
4. 可能なら `--worktree-mode copy` を付ける
5. 1回の依頼を小さくする
6. 大きな開発は `SPEC.md` を作ってから `run-stages` にする
7. `run_dir` のログを見る習慣をつける
8. 成功したら git commit する

---

## このプログラムのいちばん大事な考え方

AIにプログラムを書かせる時、一番怖いのは「できたように見えるけど、実は動いていない」ことです。

このプログラムは、その問題に対して次の考え方で作られています。

- 仕様書を先に作る
- 役割を分ける
- コードを書いたAIとは別の視点で確認する
- テストを実行する
- 失敗したら原因を文書化する
- 文書を通じて次の修正へ進む

つまり、AIを信用しすぎないために、AIを使う。

これが `Local SDLC Agent` の基本思想です。

---

## まとめ

`Local SDLC Agent` は、ローカルLLMを使ってソフトウェア開発を進めるためのCLIツールです。

小さなプログラムなら `agent`、大きなプログラムなら `SPEC.md` と `run-stages` を使います。

最初に覚えるコマンドは、この3つで十分です。

```bash
python3 local_sdlc.py doctor --skip-llm
python3 local_sdlc.py health
python3 local_sdlc.py agent "app.py を作る" --project work/my-app --new-file app.py --require-path app.py --apply --worktree-mode copy
```

慣れてきたら、仕様書を作り、ステージに分け、ログを読みながら改善していく。

そうすると、単なる「AIコーディング」ではなく、AIをチームメンバーとして扱う開発プロセスに近づいていきます。
