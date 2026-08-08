# Local SDLC Agent

> **OpenAI 互換 API で、仕様作成・実装・検証・失敗分析を進める独立したコーディングエージェント。**

このリポジトリの主成果物は、`python3 local_sdlc.py ...` で起動できるローカル開発エージェントです。
既定ではローカル LLM の OpenAI 互換 API に接続しますが、設定ファイルやCLI引数で任意の
OpenAI互換APIへ切り替えられます。PM / Coder / Judge / Failure Analysis /
Project Policy Triage などを別々の system prompt と API call で実行し、仕様書・実行ログ・
変更パッチなどの文書化された生成物を介して開発を進めます。

各修復候補は隔離領域で検証されます。同じテスト群に対する失敗数が直前より増えた場合、
候補を自動的に変更前へ戻して byte 単位で復元を確認し、限られた追加予算で別案を試します。
悪化した候補や未承認の変更が元プロジェクトへコピーされることはありません。

> **Project status:** source-available research preview. Non-commercial use is free
> under the public license terms. Commercial use requires a separate license. No
> production warranty is provided.

## 日本語ビジュアル解説

**[GitHub Pages でビジュアルガイドを開く](https://nob-git-dev.github.io/local-sdlc-agent/)**

実装全体を短時間で把握したい場合は、まずこのガイドを参照してください。
15,828 行のコア Python、Supervisor ルーティング、Agent 修復ループ、Stage 実行、
Artifact Stream Guard、検証モデル、Run 文書、実際に組み立てられる PM / Coder / Judge /
Failure Analysis プロンプトを、フローチャートと図表で日本語解説しています。

- 単一 HTML: Mermaid 9 図、Chart.js、検索、ダークモード、印刷対応
- 基準コミット: `66850426a1bed049d78027512a0ae17a1d96bcf8`
- ソース: [`docs/architecture/local_sdlc_agent_visual_guide_20260724.html`](docs/architecture/local_sdlc_agent_visual_guide_20260724.html)
- `main` のガイド更新時に GitHub Actions から Pages へ自動再配信

## 現在の位置づけ

| 項目 | 内容 |
|---|---|
| アプリ本体 | `local_sdlc.py` と `local_sdlc/` |
| 起動単位 | `python3 local_sdlc.py ...` の単独 CLI |
| LLM 接続 | 既定は `http://localhost:30000/v1`。設定ファイル/CLIでOpenAI互換APIへ切替可能 |
| プロンプト資産 | `sdlc-skills/` と `learning-skills/` を同梱資産として再利用 |
| 状態交換 | 会話履歴ではなく `.sdlc-runner/runs/` の Markdown / JSON 文書 |
| ベンチマーク | `benchmarks/` に仕様、生成成果物、実験要約を保存 |

## 動作環境

- Python 3.10 以上を対象とします。`pyproject.toml` の `requires-python` も `>=3.10` です。
- 現在の `.py` ファイルは Python 3.10 / 3.11 の構文として静的解析済みです。3.12 以上専用の構文や、本体での 3.12 以上専用標準ライブラリ API は使っていません。
- ローカル実行確認は Python 3.12.3 で行っています。
- Python 3.13 は今回の実機確認対象外です。3.13 対応を保証する場合は CI の検証 matrix に追加してください。
- コア CLI / Web UI は外部 Python パッケージ不要です。LLM は別途 OpenAI 互換 API として起動・設定します。

## クイックスタート

```bash
python3 local_sdlc.py doctor --skip-llm
python3 local_sdlc.py list-skills
python3 local_sdlc.py health
```

ローカル LLM API が稼働している場合:

```bash
python3 local_sdlc.py doctor
python3 local_sdlc.py run-stages --help
python3 local_sdlc.py agent --help
```

API接続先をプロジェクトに固定する場合は、公開用サンプルをコピーして編集します。
`local_sdlc.json` / `local_sdlc.yaml` / `local_sdlc.yml` は `.gitignore` 済みです。

```bash
cp local_sdlc.example.json local_sdlc.json
export LOCAL_LLM_API_KEY=...
python3 local_sdlc.py doctor
```

外部のOpenAI互換APIを使う場合も、キー本体はファイルへ直書きせず `api_key_env` を推奨します。

```json
{
  "llm": {
    "base_url": "https://api.example.com/v1",
    "api_key_env": "EXAMPLE_API_KEY",
    "model": "example-model",
    "model_profile": "default",
    "timeout": 300,
    "stream": true
  }
}
```

優先順位は `CLI引数 > project config > 環境変数 > 内蔵デフォルト` です。API call別の調整は
設定ファイルの `api_profile` / `function_profiles` でも、従来通り `--api-profile` でも指定できます。

### Qwen / DeepSeek の切り替え

モデルサーバーの切り替えとエージェントのプロファイル選択は別の操作です。単一モデルだけを
常駐させる環境では、先に外部のモデルサーバーを切り替え、その後に一致するプロファイルを選びます。
エージェント自身がモデルやコンテナを起動・停止することはありません。

```bash
# DeepSeek安定版: 全API callでthinking off、生成上限8,192 tokens
python3 local_sdlc.py doctor --model-profile deepseek-v4-flash-agent

# DeepSeek分析版: open-ended分析のみthinking on、短い分類/Judgeと生成物はthinking off
python3 local_sdlc.py doctor --model-profile deepseek-v4-flash-agent-deep

# Qwenへ戻した後
python3 local_sdlc.py doctor --model-profile qwen-agent
```

分析系の thinking call が reasoning だけで出力枠を使い切り、結論本文を返さなかった場合は、
同じロール・同じ system prompt・同じ入力文書に、そのcallの推論末尾だけを回復用入力として添え、
`thinking=off`・最大2,048 tokensで結論へ圧縮する再試行を一度だけ行います。推論抜粋は後続スキルへ
渡しません。この再試行も独立した API call として予算、Action Gate、`api_calls`、
`completion_recoveries` に記録されます。生成物系 call は最初から thinking off のため、この
fallback の対象にはなりません。

`local_sdlc.json`を使う場合は、`llm.model`を空にしたまま`llm.model_profile`だけを変更します。
名前付きプロファイルと`/v1/models`の実測モデルが一致しない場合、Doctorと生成処理はAPI call前に
停止します。関数別の詳細設定と切り替え規則は
[`docs/usage/model_profiles.md`](docs/usage/model_profiles.md)を参照してください。

### ブラウザ検証

HTML の受け入れ検証では Chromium 互換ブラウザを使います。通常は利用可能なブラウザを自動検出して
直接実行します。workspace sandbox などからブラウザを直接起動できない場合だけ、認証付きローカル
ワーカーへ分離できます。ワーカーは任意コマンドや任意URLを受け付けず、登録済み検証と許可した
プロジェクト配下だけを扱います。

同じ秘密値をワーカーとエージェントの環境へ渡し、ワーカーを先に起動します。秘密値は
`local_sdlc.json` や Git へ保存しないでください。

```bash
export LOCAL_SDLC_BROWSER_WORKER_TOKEN="十分に長いランダムな秘密値"
python3 local_sdlc.py browser-worker \
  --allowed-root /path/to/projects \
  --host 127.0.0.1 \
  --port 8766
```

エージェント側では次の接続先を設定します。接続先を明示した場合、ワーカー停止時に直接実行へ
暗黙に切り替わらず、検証基盤の障害として停止します。

```bash
export LOCAL_SDLC_BROWSER_WORKER_URL="http://127.0.0.1:8766"
export LOCAL_SDLC_BROWSER_WORKER_TOKEN="ワーカーと同じ秘密値"
python3 local_sdlc.py doctor --skip-llm
```

設定ファイルには秘密値そのものではなく、秘密値を保持する環境変数名だけを指定できます。

```json
{
  "browser": {
    "worker_url": "http://127.0.0.1:8766",
    "worker_token_env": "LOCAL_SDLC_BROWSER_WORKER_TOKEN"
  }
}
```

ブラウザからチャット形式で使う場合:

```bash
python3 local_sdlc.py web --host 127.0.0.1 --port 8765
```

その後、ブラウザで `http://127.0.0.1:8765/` を開きます。
Web UI は完全ローカルで動作し、Flask / FastAPI / npm / CDN は使いません。Python標準ライブラリの
軽量HTTPサーバーが、既存の `agent` / `run-stages` / `spec` / `doctor` / `health` コマンドを
ローカル子プロセスとして起動します。各ジョブのログは
`.sdlc-runner/web/jobs/` に保存されます。
初回の `agent` 新規作成ジョブでは、対象プロジェクトに `SPEC.md` が無い場合、Web UI が最小SPECを
自動生成してから既存CLIを起動します。既存の `SPEC.md` や明示した `--spec-file` は上書きしません。
明確な作成依頼では `new_file` / `require_path` を安全に補完します。曖昧な依頼は自動補完せず、
CLI と同じ strict validation で止めます。

パッケージ形式の起動にも対応しています。

```bash
python3 -m local_sdlc web --host 127.0.0.1 --port 8765
```

実行中の処理は Web の停止ボタン、または run directory を指定した CLI で停止できます。停止状態は
`cancel.json` に残るため、同じ実行の resume / retry / stage / API call / command / copy back は再開しません。

```bash
python3 local_sdlc.py cancel --run-dir .sdlc-runner/runs/<run-id> --reason user_stop
```

危険な操作は自動実行されず、Web に「人間の承認が必要」と表示されます。CLI では状態を確認し、表示された
`run_dir` と `decision_id` に対して一回だけ承認できます。承認は同じ操作の次回試行で消費され、LLM は
承認元になれません。`block` 判定は人間承認でも解除できません。

```bash
python3 local_sdlc.py safety-status --run-dir .sdlc-runner/runs/<run-id>
python3 local_sdlc.py approve --run-dir <表示された-run_dir> --decision-id D000001 --note "内容を確認済み"
```

自律実行には、全体、1工程、やり直し、AI呼び出し、実行時間の5種類の上限があります。
Webでは「詳細設定」から変更できます。CLIでは次のように指定し、実行中または停止後の使用量を
`budget-status` で確認できます。

```bash
python3 local_sdlc.py agent "修正して" \
  --include app.py --apply \
  --max-goal-actions 1000 \
  --max-stage-actions 200 \
  --max-recovery-actions 100 \
  --max-api-calls 250 \
  --max-wall-seconds 86400

python3 local_sdlc.py budget-status --run-dir .sdlc-runner/runs/<run-id>
```

上限到達は通常のテスト失敗とは区別され、`budget_stop.json` に理由が残ります。同じrunの再開で
上限を引き上げたり停止を解除したりはできないため、設定を変える場合は新しいrunとして開始します。

進捗監視は、工程、実行中の機能、LLMストリーム量、作成文書・検証証拠・変更ファイルなどの
機械観測値が変化しているかを確認します。既定では900秒間変化がなければ`STALLED`として停止し、
`stall.json`へ最後の進捗ベクトルと理由を残します。単なる経過時間や監視ログ自身の更新は進捗に
数えません。

```bash
python3 local_sdlc.py agent "修正して" \
  --include app.py --apply \
  --max-idle-seconds 900

python3 local_sdlc.py progress-status --run-dir .sdlc-runner/runs/<run-id>
```

ストリーミング中は受信量の変化で期限が更新されるため、長く考えていても出力が継続している処理を
時間だけで停止しません。一方、同じ観測値のままAPIやコマンドが止まった場合は、残り停滞時間を
単一処理のtimeoutにも反映します。`STALLED`になった元runは通常のresumeでは解除されません。
自動回復を有効にすると、停止証拠に結び付いた変更不能な回復計画を保存し、新しいrunで再開します。

テスト証拠は終了コードだけでは合格になりません。たとえば `unittest` が `Ran 0 tests` と
報告した場合は、終了コードの実装差にかかわらず空の成功として拒否し、テストハーネスを作成または
修復する対象に戻します。

```bash
python3 local_sdlc.py agent "修正して" \
  --include app.py --apply \
  --auto-recover-stalls \
  --max-idle-seconds 900
```

手動で確認してから再開する場合は、まず計画を作り、その計画が指定した新しいrunへ進めます。

```bash
python3 local_sdlc.py recovery-plan \
  --run-dir .sdlc-runner/runs/<stalled-run-id> \
  --strategy auto

python3 local_sdlc.py agent "停止原因を分析して修正" \
  --resume .sdlc-runner/runs/<stalled-run-id> \
  --recovery-plan .sdlc-runner/runs/<stalled-run-id>/recovery_plan.json \
  --run-dir <計画に表示された-recovery-target> \
  --include app.py --apply
```

同じfailure familyが閾値以上続いた場合、通常のretryは許さず、独立したfailure analysisを先に実行します。
分析済みでも同じ系列が続けばroot cause recoveryへ進みます。元runのcancel状態と回復予算は引き継ぎ、
新しいrunの進捗時計だけを新しく始めます。

変更後に同じテスト群の失敗数が増えた場合、その候補は自動的に破棄され、変更前のバイト列へ戻したことを
確認してから別案へ進みます。このとき、それ以前に機械検査で確定したAPI情報や対象ファイルは失われません。
存在しないことが確認済みのメソッドを、型が確認できるオブジェクトへ新しく呼び出す変更は、定義側も同じ
変更単位で実装されていない限り適用前に拒否されます。

生成途中に同じ複数行が周期的に繰り返された場合は、単なる長文ではなく出力暴走として早期停止します。
次の形式修復では同じ記法を繰り返さず、1ファイル・1変更だけのJSON形式へ切り替えます。

### 仕様書からの自律段階実行

`run-stages` は既定で、一時コピー上での隔離実行、工程内の形式修復・分割・原因修復、停滞した
親runの証拠付き再開を行います。各判断は `autonomy_decisions.jsonl`、最終判定は `run.json` に残り、
仕様の衝突、外部価値判断、不可逆な高影響操作、外部資源、予算追加だけを人間へ問い合わせます。

```bash
python3 local_sdlc.py run-stages "SPEC.mdを実装して受け入れ条件を満たす" \
  --project /path/to/project \
  --apply
```

工程を曖昧な推測に任せたくない場合は、`SPEC.md` の `## Implementation Stages` に次の
機械検証可能なJSONを置きます。`S01`から連番にし、工程ごとの変更許可範囲と読み取り専用の証拠を
分離してください。

```json
{
  "stage_plan_schema": 1,
  "stages": [
    {
      "stage_id": "S01",
      "title": "Core model",
      "goal": "Implement the smallest domain model required by the acceptance criteria.",
      "writable_paths": ["src/model.py", "tests/test_model.py"],
      "readonly_evidence_paths": ["SPEC.md", "tests/acceptance/"],
      "test_commands": ["python3 -m unittest tests.test_model"],
      "required_observables": ["unit tests pass"],
      "api_profile": [
        "plan_work:max_tokens=8192,thinking=on",
        "generate_artifact:max_tokens=8192,temperature=0.05,thinking=off"
      ],
      "max_rounds": 4
    }
  ]
}
```

`## Verification Commands` に実行可能なコマンドをコードフェンスで記載すると、明示的な
`--test-command` がない場合の最終受け入れゲートとして実行されます。安全判定を通らないコマンドは
実行されません。受け入れ条件が一つでも未検証または不合格なら、runは完了扱いになりません。

````markdown
## Verification Commands

```bash
python3 -m unittest discover -s tests
```
````

自律回復を意図的に無効にする場合だけ `--no-autonomous-recovery` を指定します。工程あたりの回復回数、
停滞後の親run再開回数、分割前の最大変更path数は、それぞれ `--max-stage-recoveries`、
`--max-stalled-recoveries`、`--max-stage-writable-paths` で制限できます。

### 検証済み経験の再利用

Experience Learning Runtime は、成功・失敗の記録からいきなり規則を有効化しません。まず知識案を
`candidate` として保存し、replay、改名しても同じ結果になる検査、negative、別プロジェクトの
holdoutを通します。検証後も、低影響の提案だけが機械昇格でき、`require` / `forbid`、安全、権限に
関わる知識は一回限りの人間承認が必要です。

共有データの保存先は環境変数か `--data-dir` で指定します。

```bash
export LOCAL_SDLC_LEARNING_HOME=/path/to/learning-data
python3 local_sdlc_learning.py doctor
python3 local_sdlc_learning.py inspect K-example
python3 local_sdlc_learning.py explain K-example
python3 local_sdlc_learning.py snapshots --data-dir "$LOCAL_SDLC_LEARNING_HOME"
python3 local_sdlc_learning.py work-status --data-dir "$LOCAL_SDLC_LEARNING_HOME"
```

検証済みの知識案を昇格する例です。高影響の場合、最初のコマンドは
`approval_required` と `operation_id` / `decision_id` を返します。内容を確認した人間が承認し、同じ
昇格を再実行した時だけ承認が消費されます。

```bash
python3 local_sdlc_learning.py promote \
  --data-dir "$LOCAL_SDLC_LEARNING_HOME" --candidate K-example
python3 local_sdlc_learning.py approve-promotion \
  --data-dir "$LOCAL_SDLC_LEARNING_HOME" \
  --operation PO-example --decision D000001
python3 local_sdlc_learning.py promote \
  --data-dir "$LOCAL_SDLC_LEARNING_HOME" --candidate K-example
```

誤った知識は無効化でき、履歴は削除されません。

```bash
python3 local_sdlc_learning.py challenge \
  --data-dir "$LOCAL_SDLC_LEARNING_HOME" \
  --knowledge K-example --reason observed_regression
python3 local_sdlc_learning.py rollback \
  --data-dir "$LOCAL_SDLC_LEARNING_HOME" --snapshot KS-previous
```

候補生成と検証は、API呼び出し回数、検証ケース数、予約出力token数、経過時間の上限を持ちます。
実行状況は `work-status` で確認でき、人間は実行IDを指定するか、実行中の学習処理すべてを次の安全な
チェックポイントで停止できます。

```bash
python3 local_sdlc_learning.py cancel-work \
  --data-dir "$LOCAL_SDLC_LEARNING_HOME" --operation LW-example
```

上限は `mine-candidates` / `validate` の
`--learner-max-api-calls`、`--learner-max-cases`、`--learner-max-tokens`、
`--learner-max-wall-seconds` で変更できます。中止または予算超過後は次のAPI呼び出し、ケース判定、
保存処理を開始しません。

実行対象プロジェクトに `DOMAIN_MAP.json` がある場合、`agent` / `run-stages` / `supervisor` はrun開始時に
適用可能なactive知識だけを選び、`knowledge-snapshot.json`へ固定します。実行途中で共有知識が更新されても
そのrunは変わりません。Domain Mapや共有ストアが無い場合は、理由付きの空snapshotで通常実行を続けます。
明示的に無効化する場合は `--disable-learning-context` を使います。詳細契約は
[`learning-runtime/SPEC.md`](learning-runtime/SPEC.md) を参照してください。

Qwen / DeepSeek / Ornith などのモデル差し替えは、散在する個別 flag ではなく `--model-profile` と
`--api-profile FUNCTION:key=value` で管理します。

---

## 背景

---

## なぜ作ったか — Vibe Coding の先にある課題

「AI に頼めば動くものができる」——この体験（**Vibe Coding**）は、開発の敷居を劇的に下げました。
エンジニアでなくてもアイデアをソフトウェアにできる。これは「**床を上げた**」と言えます。

しかし、本番で使えるソフトウェアには別の規律が要ります。Andrej Karpathy はこれを
**Agentic Engineering**——「失敗しうるエージェントを調整し、品質・セキュリティ・保守性を
保ちながら**天井を上げる**専門的規律」——と整理しました。その本質は、エージェントに丸投げせず、
**仕様・計画・検証・権限・レビュー・理解を人間が握り続けること**です。

このリポジトリの開発者自身、AI に開発を任せる中で**本番データベースの全消失を短期間に 2 回**
経験しました。エージェント向け指示ファイルにルールを書いても、メモリに記録しても、防げませんでした。

そこで得た結論はシンプルです:

> **ルールを「書いて渡す」のではなく、プロセスを「守らざるを得ない構造」にする。**

「守ろうと思う」だけでは、人も AI も忘れます。だから構造で強制します。

---

## 3 つの柱

### 1. Supervisor — 常駐する「最初の窓口」

スキルは**呼ばれなければ何もしません**。Supervisor はすべての発言を受け取り、意図を分類し
（開発か / 質問か / 危険な操作か）、危険信号（削除・本番・マイグレーション等）を手前で止め、
**承認なしには次に進みません**。「気づいたら実行されていた」を構造的に防ぎます。

### 2. SDLC オーケストレーション — 仕様書が開発を駆動する

`SPEC.md`（仕様書）を唯一の真実（Single Source of Truth）とし、
`仕様 → 設計 → 実装 → レビュー → デプロイ` を**品質ゲート**で進めます。
各フェーズは `context: fork` で隔離実行され、引き継ぎは SPEC.md と git だけを経由します。
完了は「動いた」ではなく、**受け入れ条件を一つずつ照合して**判断します。

検証コマンドが1つでも失敗していれば、要件との対応表がPASSに見えても完了にはしません。
また、原因分析から修正計画を作った場合は、Coderとは別のAPI呼び出しが候補差分を
義務ごとに確認します。不完全な差分はファイルへ適用せず、同じ計画を保ったまま再生成します。
LLMのレビューは助言であり、最終的な適用・完了権限は機械的なゲートに残ります。

### 3. 行動の憲法 — 失敗から確立した判断軸（全 12 条）

ルールの羅列ではなく、**「なぜ守るか（経緯と本質）」を保持する上位原則**です。
実プロジェクトの失敗から条文化されました。一部を挙げると:

- **検証した事実だけに従う** — 表示・記憶・未実行の結果は「主張」。検証した値だけを事実とする
- **リスクに比例して検証を厚くする** — 読むだけは速く、消す / 本番に触れるほど段数を増やす
- **権威ある定義元を当たる** — 手探りや記憶でなく、一次情報（ソース・定義・実データ）を先に読む
- **借り物の解は適用条件を照合する** — 「少量向け」の手法を本番規模で検算せずに使わない
- **本番影響は能動的に壊しにいって検証する** — 「動く」ではなく「壊せない」を確かめてから出す

ほかに、全層を掃いて完了する / 対症でなく根治へ / 公式の継ぎ目を尊重する / 判断を外在化する /
agent-native に作る——など、計 12 条。詳細は
[`sdlc-skills/docs/work-constitution.md`](sdlc-skills/docs/work-constitution.md)。

---

## 思想の系譜

| 源流 | 受け継いだもの |
|---|---|
| Andrej Karpathy "Agentic Engineering" | 天井を上げる規律。仕様・検証・レビュー・理解を人間が握る |
| 古典的ソフトウェア工学 | Uncle Bob（TDD 三法則）/ Kent Beck / Fowler（リファクタリング）/ Evans（DDD）/ Google SRE |
| 実プロジェクトの失敗 | 本番事故から条文化した「行動の憲法」（経緯と本質を保持） |

設計判断の詳細: [`sdlc-skills/docs/design-decisions.md`](sdlc-skills/docs/design-decisions.md)

---

## リポジトリ構成

| パス | 役割 |
|---|---|
| `local_sdlc.py` | 互換性を維持する CLI entrypoint |
| `local_sdlc/` | Application / Domain / Infrastructure に分割された実装本体 |
| `learning_runtime/` | 経験収集、候補化、反例検証、昇格、snapshot公開を行う独立制御面 |
| `learning-runtime/SPEC.md` | Experience Learning Runtime の正本仕様 |
| `tests/` | ハーネス自体の回帰テスト |
| `SPEC.md` | ローカル SDLC Agent 自体の仕様書 |
| `benchmarks/` | Agent に作らせた成果物、仕様、比較実験 |
| `docs/` | 設計判断、失敗分析、改善履歴 |
| `sdlc-skills/` | SDLC prompt 資産。ローカル Agent では system prompt の材料として扱う |
| `learning-skills/` | 学習・改善 skill。ローカル Agent では prompt 資産として扱う |

## 同梱スキルセット

各スキルセットは独立したサブディレクトリに収録されています。

| ディレクトリ | 内容 |
|---|---|
| **[sdlc-skills/](sdlc-skills/)** | SDLC を仕様書中心に規律正しく進めるスキルセット。12 スキル（`/sdlc` `/spec` `/architect` `/tdd` `/review` `/security` `/deploy` ほか）+ 4 サブエージェント + 3 フック + 行動の憲法。**まずここを参照してください** |
| **[learning-skills/](learning-skills/)** | 完了したプロジェクトから AI 自身の挙動を学習し、**人間のゲートを通して**改善する自己改善パイプライン。3 スキル（`/post-project-learning-engine` `/skill-proposal-engine` `/skill-regression-checker`）。`観測 → 抽出 → 提案 → 回帰検査` の多段ゲートで、学習の暴走（過剰一般化・肥大化・回帰）を防ぐ。sdlc-skills と合わせて「やる → やり方を直す」の閉ループになる |

---

## ライセンス

このリポジトリは **source-available research preview** として公開しています。OSI 承認の
open source license ではありません。

| 対象 | 公開ライセンス |
|---|---|
| コード本体（`local_sdlc.py`, `local_sdlc/`, `tests/`, benchmark source code など） | [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) 相当の非商用利用 |
| 文書・プロンプト・skill asset（`docs/`, `sdlc-skills/`, `learning-skills/` など） | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode) |

個人利用、研究、学習、評価などの非商用利用は公開ライセンスの範囲で利用できます。
営利企業内での利用、商用サービスへの組み込み、SaaS・クラウド・コンサルティング用途などは
別途商用ライセンスが必要です。

詳細:

- [LICENSE](LICENSE)
- [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)
- [NOTICE.md](NOTICE.md)
