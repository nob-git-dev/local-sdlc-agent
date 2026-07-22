# Local SDLC Agent

> **ローカル LLM API で、仕様作成・実装・検証・失敗分析を進める独立したコーディングエージェント。**

このリポジトリの主成果物は、`python3 local_sdlc.py ...` で起動できるローカル開発エージェントです。
OpenAI 互換 API を提供するローカル LLM に対して、PM / Coder / Judge / Failure Analysis /
Project Policy Triage などを別々の system prompt と API call で実行し、仕様書・実行ログ・
変更パッチなどの文書化された生成物を介して開発を進めます。

## 現在の位置づけ

| 項目 | 内容 |
|---|---|
| アプリ本体 | `local_sdlc.py` と `local_sdlc/` |
| 起動単位 | `python3 local_sdlc.py ...` の単独 CLI |
| LLM 接続 | 既定は `http://localhost:30000/v1` の OpenAI 互換 API |
| プロンプト資産 | `sdlc-skills/` と `learning-skills/` を同梱資産として再利用 |
| 状態交換 | 会話履歴ではなく `.sdlc-runner/runs/` の Markdown / JSON 文書 |
| ベンチマーク | `benchmarks/` に仕様、生成成果物、実験要約を保存 |

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

Qwen / Ornith などのモデル差し替えは、散在する個別 flag ではなく `--model-profile` と
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
経験しました。`CLAUDE.md` にルールを書いても、メモリに記録しても、防げませんでした。

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

各スキルセットのディレクトリ内の `LICENSE` を参照してください。
いずれのスキルセットも、個人・研究・非営利は **CC BY-NC-SA 4.0**（無償）、営利利用は**商用ライセンス**（要申請）です。
ライセンス本文: [sdlc-skills](sdlc-skills/LICENSE) / [learning-skills](learning-skills/LICENSE)。
