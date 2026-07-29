# local_sdlc.py 仕様書

## 目的

OpenAI 互換 LLM API を使い、仕様作成・実装・検証・失敗分析を進める単一 CLI 型のコーディングエージェントとして実行する。
既定接続先はローカル LLM API とするが、プロジェクト設定ファイル、環境変数、CLI 引数によって任意の OpenAI 互換 API へ切り替えられる。

ここでいう「単一プログラム」は利用者から見た起動単位を意味し、単一ソースファイルを意味しない。内部実装は責務ごとにモジュール分割し、将来の GUI 組み込みでも同じ Application / Domain / Infrastructure を再利用できる構造にする。

このリポジトリの主成果物は `local_sdlc.py` / `local_sdlc/` で構成される独立ローカルエージェントである。`sdlc-skills/` と `learning-skills/` は、このローカルエージェントが再利用するプロンプト資産として扱う。

### 用語

- 生成物（artifact）: LLM が runner へ渡す適用候補。unified diff、`BEGIN_FILE`、`BEGIN_SEARCH_REPLACE`、JSON の `replace_file` / `search_replace` などを含む。利用者向け文書では、原則として「生成物」「変更パッチ」「生成ファイル」と表記する。
- 文書成果物: `.sdlc-runner/runs/` に保存される Markdown / JSON の判断記録、実行証拠、失敗分析、生成物ログ。
- runner: LLM の出力を検査し、許可された場合だけファイル適用、テスト実行、証拠保存を行う決定的な実行制御層。
- 自律 Supervisor Runtime: agent / run-stages / Web job を外側から監視し、停止・停滞・失敗を状態として分類し、安全条件を満たす範囲で resume / retry / split / blocked へ遷移させる親制御層。
- Safety/Suppression Harness: command、artifact apply、resume、service 操作、git 操作などの action を実行前に分類し、allow / require_approval / block を決定する安全制御層。
- 中核命題: 現時点で固定してよい安全・進捗・証拠の不変条件。例: Safety は完走性より優先する、cancel 後に新しい action を開始しない。
- 発見命題: 実装・検証・失敗分析で新たに判明した条件。証拠、適用範囲、反例、汎用化理由を持つ場合だけ SPEC.md または learning record へ昇格できる。
- 過剰適合: 特定 benchmark、特定モデル、特定失敗ログだけに合わせた規則を一般規則として実装し、未知タスクへの解決力や安全性を下げること。

### 機能ごとの目的

| 機能・コンポーネント | 目的（この機能が存在する理由） | 変えてはならない本質 |
|---|---|---|
| スキルローダー | `sdlc-skills/skills/*/SKILL.md` をプロンプト資産として再利用する | 役割・安全規律・生成物契約を保ち、特定製品への依存だけを源流資産から除く |
| LLM クライアント | `http://localhost:30000/v1` などの OpenAI 互換 API に接続する | 特定 provider を前提にせず、既定はローカル、必要時は設定で切り替えられる |
| API Configuration | `local_sdlc.json/yaml/yml`、環境変数、CLI 引数から LLM 接続情報を合成する | 秘密値を Git 管理へ入れず、API 差し替えが role/function profile 設計を壊さない |
| SPEC 駆動フロー | `SPEC.md` を中心に仕様・設計・実装案・レビューを進める | SPEC.md を唯一の根拠として扱う思想を維持する |
| パッチ提案 | ローカル LLM に unified diff を生成させ、人間確認後に適用できるようにする | 破壊的変更や未確認パッチを自動適用しない |
| Doctor/検証 | 実行環境・スキル配置・LLM 接続を確認する | 可変環境事実を未確認のまま固定要件化しない |
| Supervisor | PM・Coder・Judge の独立 API call を制御する | エージェント間の情報交換を会話履歴ではなく文書成果物に限定する |
| SDLC Supervisor Router | ユーザー依頼を分類し、元リポジトリの Supervisor / SDLC フローに沿って専門スキルを選ぶ | 固定 PM/Coder/Judge だけに縮退せず、`spec`, `architect`, `tdd`, `review`, `security`, `deploy` 等を適切に呼び分ける |
| PM エージェント | 目的、仕様、設計、計画、受け入れ条件を固める | 実装作業を自分で兼ねない |
| Coder エージェント | PM 文書と SPEC.md に基づき実装案・パッチを作る | 仕様外の判断や未確認の事実を補完しない |
| Judge エージェント | Coder 成果物を仕様・証拠・テスト観点で客観評価する | Coder と同じ会話履歴を共有せず、文書成果物だけで判断する |
| Failure Analysis エージェント | 同じ失敗が繰り返された時に、観測事実・棄却仮説・次の必須行動を構造化する | パッチ生成や自己正当化をせず、実行証跡から命題化した制約だけを残す |
| Project Policy Triage エージェント | テスト所有権や生成物の復旧可否など、プロジェクト文脈に依存する判断を分類する | LLM に直接適用権限を渡さず、分類結果を runner の機械検証に通す |
| Acceptance Evidence Gate | SPEC.md の受け入れ条件を実行証拠の `covers` / 直接コマンド対応と照合し、未証明のまま承認されることを防ぐ | Coder/Judge の文章上の自己申告だけで完了扱いにしない |
| Browser Behavior Smoke | HTML/ブラウザ成果物を headless Chromium で実際に操作し、DOM/API 存在だけでなく可視状態変化を観測する | 静的な見た目や関数名だけで「動作する」と判定しない |
| Function API Profile | API 設定を実際の認知処理単位で最適化する | ロール責務を曖昧にせず、関数別 profile を role の下位実行設定として扱う |
| Model/API Preset Profile | Qwen/Ornith/Nemotron などモデル特性ごとの既定 model・temperature・max_tokens・thinking を名前付き preset として管理する | モデル差し替えを散在 CLI flag ではなく `--model-profile` と function-level override で行い、API call ごとの model 選択余地を残す |
| Local Web Chat UI | ブラウザから chat 形式で `agent` / `run-stages` / `spec` / `doctor` / `health` を投入・監視・停止する | Web UI は薄い presentation adapter に留め、既存 CLI harness の実行規律を迂回しない |
| Autonomous Supervisor Runtime | 長時間実行、停滞、失敗、再開、stage split を goal 単位で管理し、完了または理由付き blocked へ到達させる | 停止した agent 自身に自己観測を任せず、外側の親制御層が観測・判断・再投入を行う |
| Safety/Suppression Harness | すべての action を実行前に安全分類し、人間 cancel / approval-required / block を完走性より優先する | LLM に実行承認権限を渡さず、危険操作を粘り強く再試行する自律ループにしない |
| Anti-overfitting Governance | 失敗から得た学習を中核命題と発見命題に分け、適用範囲と反例を持つ規則だけを昇格する | Tetris / Mini SQLite / 特定モデルなど個別経験を無条件に一般規則へしない |

## 振る舞い

- `local_sdlc.py doctor` は、スキルディレクトリ、利用可能スキル、`SPEC.md`、git 状態、ローカル LLM API の到達性を確認する。
- `local_sdlc.py doctor` は、読み込んだ `local_sdlc.json/yaml/yml` または `--config-file` のパスを表示し、実効 LLM API 設定を監査できる。
- `local_sdlc.py` は、API 接続設定を `CLI 引数 > project config > 環境変数 > 内蔵デフォルト` の順に合成する。
- `local_sdlc.json/yaml/yml` の `llm.api_key_env` は、API キー値ではなく環境変数名を指す。runner はその環境変数から秘密値を読む。
- `local_sdlc.py list-skills` は、読み込めるスキル名と説明を表示する。
- `local_sdlc.py spec "<依頼内容>"` は、`spec` スキル本文と依頼内容を LLM に渡し、`SPEC.md` 草案を生成する。
- `local_sdlc.py phase <skill>` は、既存 `SPEC.md` 全文と指定スキル本文を LLM に渡し、該当フェーズの提案を生成する。
- `local_sdlc.py implement` は、`SPEC.md`、ファイル一覧、任意で指定されたファイル本文を LLM に渡し、unified diff 形式の実装パッチを提案する。
- `local_sdlc.py supervise "<依頼内容>"` は、Supervisor が PM → Coder → Judge を独立した API call として順に呼び出し、`.sdlc-runner/runs/` に各成果物を保存する。
- `local_sdlc.py supervisor "<依頼内容>"` は、元リポジトリの `agents/supervisor.md` に基づく分類・危険信号検知・フェーズ選定を行い、必要に応じて `spec`, `architect`, `tdd`, `review`, `security`, `deploy` 等の専門スキルを独立 API call で順次実行する。
- 各 skill/agent call では、SKILL.md 本文を user prompt ではなく system prompt に配置する。
- PM、Coder、Judge は会話履歴を共有しない。前段の出力は保存済み Markdown 文書として後段に渡す。
- `local_sdlc.py supervise --auto-fix --max-rounds N` は、Judge が修正依頼を出した場合に Judge 文書を次ラウンドの Coder 入力として渡し、承認または上限到達まで Coder/Judge を繰り返す。
- `local_sdlc.py agent` は同じ実行失敗シグネチャ、または同じ失敗テスト群を示す failure-family signature が繰り返された場合、Judge レベルの `failure_analysis` を独立 API call として実行し、`05-rXX-failure-analysis.json` と `run.json.failure_analyses` に観測事実・棄却仮説・制約・次行動を保存する。
- Root cause 修復は最新の Failure Analysis 文書を入力文書として受け取り、棄却済み仮説や禁止焦点を制約として扱う。
- 生成物 parser は、意味が一意に復元できる軽微な構文崩れだけを機械的に正規化する。例: `BEGIN_SEARCH_REPLACE: : path` の余分な colon、SEARCH/REPLACE ブロック全体を包む Markdown fence。編集対象や編集内容が曖昧な場合は正規化せず拒否する。
- `local_sdlc.py agent --project-policy-triage auto|always|never` は、生成テストハーネスの所有権やテスト編集可否など、固定ルール化するとプロジェクト過学習になりやすい判断だけを Judge レベルの `project_policy_triage` 独立 API call に渡す。
- Project Policy Triage は JSON 分類だけを返し、`run.json.project_policy_triages` と `05-rXX-project-policy-triage.json` に保存する。分類結果は次ラウンドの文書入力になるが、生成物の適用権限は持たない。
- Runner は三層制御を使う: Universal Invariants は機械的に常時強制し、Project Policy は SPEC.md/run documents から判断し、曖昧な境界だけを LLM triage に分類させる。
- Runner は初期検証後と各修復ラウンド後に Acceptance Evidence Gate を実行し、`acceptance_matrix` の `fail` または `unverified` blocker が残る場合は承認しない。
- Acceptance Evidence Gate は、チェックボックス付き箇条書きだけでなく通常の箇条書きも受け入れ条件として抽出し、証拠側の `covers` または受け入れ条件内のバッククォート付きコマンド断片と対応付ける。
- Browser Tetris smoke はローカル HTTP 経由で HTML を開き、Start 後に可視セルが生まれること、ArrowLeft 後に可視セル index が変わること、`gameOver()` が画面へ反映されることを構造化 JSON 証拠として保存する。
- Acceptance Evidence Gate の blocker は次ラウンドの repair advice に変換され、未証明命題を Coder へ文書で渡す。
- `--model-profile qwen-agent|qwen-agent-deep|ornith-agent|ornith-agent-deep|nemotron3-super-agent` は、モデルごとの既定 model と API call profile を選択する。
- `--model` は model profile の既定 model を上書きするが、`--api-profile FUNCTION:model=...` は特定 function/API call だけの model を上書きできる。
- `local_sdlc.py doctor` と `run.json` は、有効な `model_profile`、role/function 別の model、temperature、max_tokens、thinking を表示・保存する。
- 書き込みやパッチ適用は `--apply` が指定されたときだけ行う。デフォルトは標準出力またはパッチファイルへの保存に留める。
- `local_sdlc.py web --host 127.0.0.1 --port 8765` は、Python 標準ライブラリのみで軽量HTTPサーバーを起動し、ブラウザ用の単一HTMLチャットUIを返す。
- Web UI からの実行は既存 CLI コマンドをローカル子プロセスとして起動し、stdout/stderr と job metadata を `.sdlc-runner/web/jobs/` に保存する。
- Web UI のプロジェクト欄に存在しない新規ディレクトリを指定した場合、Web サーバー起動時の project 親ディレクトリ配下に限りジョブ開始前に自動作成する。
- Web UI から別プロジェクトを対象にする場合でも、`--skills-dir` は Web UI を起動したエージェント本体リポジトリ内の `sdlc-skills/skills` を絶対パスで渡す。
- Web UI はジョブの開始、状態取得、ログ表示、停止だけを担当し、パッチ適用・テスト・Judge 判定などの制御は既存 runner に委譲する。
- `python3 -m local_sdlc ...` は `local_sdlc.py ...` と同じ CLI を起動し、将来の package install / console script 起動に備える。
- 自律 Supervisor Runtime は goal を `PLANNED -> RUNNING -> PROGRESSING -> STALLED -> RECOVERY_PLANNED -> RESUMED -> VERIFYING -> COMPLETED` または `USER_CANCELLED` / `SAFETY_BLOCKED` / `APPROVAL_REQUIRED` / `BLOCKED` の状態機械として扱う。
- 自律 Supervisor Runtime は `progress.jsonl`、`run.partial.json`、子プロセス状態、stream stats、evidence 変化を使い、長時間思考と停滞を区別する。
- Safety/Suppression Harness は各 action 実行前に `SafetyDecision` を生成し、`safety_decisions.jsonl` に保存する。
- 人間が goal/job/stage を cancel した場合、既存プロセスを停止し、その後の新規 API call、command、resume、retry、stage split、copy back を開始しない。
- `require_approval` の action は人間承認が記録されるまで実行しない。LLM 出力、Judge 承認、成功予測は人間承認の代替にならない。
- 発見命題を一般規則に昇格する場合は、根拠となる evidence、適用範囲、既知の反例、汎用化理由、回帰テストを持たせる。
- 過去 benchmark から得た個別修復規則は、core runner に直接ハードコードせず、まず発見命題または scope 付き regression memory として保存する。

## 受け入れ条件

- [x] `python3 local_sdlc.py list-skills` で `spec`, `sdlc`, `architect`, `tdd`, `review` を含むスキル一覧が表示される
- [x] `python3 local_sdlc.py doctor --skip-llm` が LLM 接続なしで成功し、スキル数と SPEC.md の有無を表示する
- [x] `python3 -m unittest discover -s tests` が成功する
- [x] LLM API が稼働している環境では `python3 local_sdlc.py doctor` が `/v1/models` を確認し、実測モデル名を表示する
- [x] `local_sdlc.py` は外部 Python パッケージなしで動作する
- [x] スキル本文は system role のメッセージに入り、user role には SPEC.md や成果物文書だけが入る
- [x] `python3 local_sdlc.py supervise "..." --steps pm --max-tokens 256` が Supervisor 経由の独立 API call を実行し、PM 文書を保存する
- [x] `supervise` の PM/Coder/Judge はそれぞれ別の chat completions request として実行される
- [x] LLM API の timeout は Python traceback ではなく利用者向けエラーとして表示される
- [x] 生成 timeout 後に `/v1/models` の短いヘルスチェックを行い、API の生死と生成 timeout を区別する
- [x] 生成リクエスト前に短い `/v1/models` preflight を行い、API が alive と確認できてから生成 timeout を使う
- [x] 生成 timeout はソケット読み取り単位ではなく、壁時計時間でも上限をかける
- [x] `python3 local_sdlc.py health` で API 生存確認だけを短く実行できる
- [x] 新規ファイル生成は `--new-file` で対象パスを明示でき、既存ファイル本文なしでも安全に Coder を実行できる
- [x] `--auto-fix --max-rounds N` で Judge の修正依頼を次ラウンドの Coder に渡し、承認または上限まで自動反復できる
- [x] `python3 local_sdlc.py supervisor "..."` が元リポジトリの Supervisor 文書を system prompt として呼び、分類・危険信号・推奨フェーズを run_dir に保存する
- [x] `python3 local_sdlc.py supervisor "..." --execute` が `spec`, `architect`, `tdd`, `review` 等の専門スキルをフェーズごとに別 API call / 別 system prompt で実行できる
- [x] 本番・DB・セキュリティ・不可逆操作の危険信号を検出した場合、`security` / `deploy` 等のゲートを推奨フェーズへ強制追加できる
- [x] 同じ実行失敗、または同じ失敗テスト群が繰り返された場合、Failure Analysis を独立 API call として保存し、Root cause 修復へ文書経由で渡す
- [x] 安全に一意復元できる fenced SEARCH/REPLACE 形式の生成物は正規化して適用可能な生成物として扱い、危険な loose function replacement と混同しない
- [x] 生成テストハーネス編集のようなプロジェクト依存判断では Project Policy Triage を独立 API call として保存し、LLM 分類を runner の機械検証に通してから次行動へ反映する
- [x] `--model-profile qwen-agent` / `--model-profile ornith-agent` / `--model-profile nemotron3-super-agent` で名前付き model/API preset を切り替えられる
- [x] `--api-profile failure_analysis:model=...` で、生成物作成とは別モデルを特定 function/API call に割り当てられる
- [x] `doctor` と `run.json` が有効な model_profile と role/function 別 API 設定を監査・比較用に残す
- [x] 受け入れ条件ごとに実行証拠を照合し、未証明または失敗している条件がある場合は `acceptance-evidence-gate` で承認を止める
- [x] Tetris の browser smoke は DOM/API 存在だけでなく、Start 後の可視 active piece と ArrowLeft 後の可視位置変化を検証する
- [x] Acceptance Evidence Gate の blocker は repair advice に変換され、次ラウンドの Coder へ具体的な未証明命題として渡る
- [x] `python3 local_sdlc.py web --host 127.0.0.1 --port 8765` で、完全ローカルのHTMLチャットUIを起動できる
- [x] Web UI は外部 Python パッケージ、npm、CDNを使わず、既存CLIを安全な argv 子プロセスとして起動する
- [x] Web UI から開始したジョブの状態、コマンド、ログ保存先、標準出力をブラウザで確認できる
- [x] Web UI のプロジェクト欄で作業ルート配下の新規ディレクトリを指定した場合、ジョブ開始前に自動作成し、範囲外の新規ディレクトリ作成は拒否できる
- [x] Web UI の初回 `agent` 新規作成ジョブは、対象プロジェクトに `SPEC.md` が無い場合でも最小SPECを自動生成し、CLI本体のSPEC必須ルールを満たしてから実行できる
- [x] Web UI から別プロジェクトを対象にしても、スキルディレクトリはエージェント本体側の絶対パスを使い、対象プロジェクト側に `sdlc-skills/skills` が無くても起動できる
- [x] `python3 -m local_sdlc web --help` で package entrypoint 経由の起動ヘルプを表示できる
- [x] `local_sdlc.json` / `local_sdlc.yaml` / `local_sdlc.yml` に `llm.base_url`, `llm.api_key_env`, `llm.model`, `llm.model_profile`, `llm.api_profile` を保存し、CLI引数なしで読み込める
- [x] `--config-file` で任意の設定ファイルを指定でき、相対パスは `--project` から解決される
- [x] CLI引数は設定ファイルのAPI設定を上書きできる
- [x] Web UI は設定ファイルを子プロセスへ引き継ぎ、画面で入力されたAPIキーをコマンド表示やジョブログへ露出しない
- [ ] P01: cancel 後、新しい API call / command / resume / retry / stage split / copy back が開始されない
- [x] P01a: `cancel.json` を run_dir に永続化でき、cancel 済み run_dir の `agent --resume` は LLM API call を開始せず拒否する
- [x] P01b: work-start を `progress.jsonl` に append-only 記録し、cancel sequence より後に work-start が存在しないことを機械的に検査できる
- [x] P01c: cancel 済み `run-stages` は stage agent call を開始せず拒否する
- [ ] P02: 危険 action は人間承認なしに実行されず、`SafetyDecision` として `require_approval` または `block` が記録される
- [x] P02a: command action は実行前に `SafetyDecision` を記録し、危険コマンドは `require_approval` または `block` として実行しない
- [ ] P03: progress vector が一定時間変化しない場合、goal または stage が `STALLED` に遷移する
- [ ] P04: `STALLED` 後、許可された recovery が存在する場合は `RECOVERY_PLANNED` を記録し、resume / retry / split / profile switch のいずれかへ遷移できる
- [ ] P05: 同一 failure family が閾値以上続く場合、通常 retry ではなく failure analysis または root cause recovery へ遷移する
- [ ] P06: artifact 生成中に形式違反が確定した場合、stream guard が早期停止し、次 action を format repair または blocked に限定する
- [ ] P07: `COMPLETED` は acceptance matrix の全条件が pass した場合だけ成立する
- [ ] P08: `BLOCKED` は reason、supporting evidence、next required human input を持つ
- [ ] P09: 自律 loop は goal / stage / recovery / API call / wall-clock の予算上限を持ち、上限到達時に理由付きで停止する
- [ ] P10: 自律 mode のファイル変更は既定で隔離 worktree 上で行われ、承認済み成果物だけが元 project へ copy back される
- [ ] P11: 発見命題は evidence、scope、counterexamples、generalization_rationale、regression_tests を持たない限り中核規則へ昇格されない
- [ ] P12: Tetris、Mini SQLite、Redis など既存 benchmark 固有の失敗規則は、未知小課題に対する regression で過剰発火しないことを確認する

## スコープ（やらないこと）

- 特定の外部エージェント環境の Skill、context fork、hook 機構を完全再現しない
- LLM 出力を無条件に信用して自動コミットしない
- Docker サービスや LLM サーバーをこのプログラム内に同梱しない
- 外部クラウドサービス、npm build、CDN、重いWeb framework に依存したUIは作らない
- 複数エージェントで暗黙の会話履歴やメモリを共有しない
- 完走性を理由に Safety/Suppression Harness を迂回しない
- LLM に人間承認、危険操作の最終許可、cancel 解除を代行させない
- 特定 benchmark の成功だけを根拠に、scope なしの一般規則を追加しない

## 固定要件

- 利用者向けの CLI entrypoint は `local_sdlc.py` として維持する
- package entrypoint として `python3 -m local_sdlc` を維持する
- 利用者向けのローカルWeb entrypoint は `local_sdlc.py web` として維持する
- Web UI が自動作成できる新規 project directory は、Web サーバー起動時の project 親ディレクトリ配下に限定する
- Web UI の子プロセスには、エージェント本体リポジトリの `sdlc-skills/skills` を絶対 `--skills-dir` として渡す
- 内部実装は責務ごとに `local_sdlc/` パッケージへ分割する
- Python 標準ライブラリのみを使う
- デフォルト LLM API は `http://localhost:30000/v1` とし、project config、環境変数、CLI引数で上書き可能にする
- project config は `local_sdlc.json`, `local_sdlc.yaml`, `local_sdlc.yml` を自動検出し、`--config-file` で明示指定もできる
- 実設定ファイルは秘密値を含む可能性があるためGit管理しない。公開用には `local_sdlc.example.json` のみを置く
- APIキーは `api_key_env` で環境変数名を指定する方式を推奨し、Web UI はAPIキーを表示コマンドやジョブログへ残さない
- デフォルトではファイル変更・パッチ適用を行わない
- `SPEC.md` 全文をフェーズプロンプトに含め、省略・要約を既定動作にしない
- `sdlc-skills/skills/*/SKILL.md` の役割・安全規律・生成物契約を維持し、特定製品・企業・専用ランタイムへの依存表現は源流資産自体で中立化する
- 各スキルの SKILL.md は system prompt レベルで渡す
- PM/Coder/Judge は独立した API call とし、後段に渡す情報は保存済み文書だけにする
- Coder の成果物は Judge の確認対象にし、Coder 自身の自己評価だけで完了扱いにしない
- 自動反復は `--auto-fix` 明示時のみ行い、`--max-rounds` で必ず上限を設ける
- Qwen/Ornith 等のモデル別最適化は名前付き `model_profile` preset として管理し、散在する個別 flag やハードコードされたモデル名へ退化させない
- 実効 API 設定は `model_profile default -> global --model -> role override -> function profile -> --api-profile FUNCTION overrides` の階層で合成する
- model、temperature、max_tokens、thinking は API call 単位で監査可能にし、単一グローバル設定だけに閉じ込めない
- 分析系 API call は、サービング層が `reasoning_content` と `content` を分離できる場合に thinking on を許可する
- 成果物生成系 API call は、patch / JSON / `BEGIN_FILE` の機械可読性を守るため thinking off を維持する
- `reasoning_content` は監査用 metadata として保存するが、後続スキルに渡す本文・artifact・handoff document にはそのまま混ぜない
- 将来の mixed-model 運用のため、function/API call 単位の model override を維持する
- `doctor` と `run.json` は有効な `model_profile` と role/function 別 model/API 設定を表示・保存する
- 自律 Supervisor Runtime では Safety/Suppression Harness の判断を完走性より優先する
- cancel 後に新しい action を開始してはならない
- 危険 action の承認は人間または明示 policy だけが与えられる。LLM は承認を代替しない
- すべての自律 action は実行前に Safety/Suppression Harness を通す
- 自律 loop には goal / stage / recovery / API call / wall-clock の予算上限を必ず設ける
- 自律 mode の変更適用は既定で隔離 worktree 上で行う
- 中核命題と発見命題を分離し、発見命題を一般規則へ昇格するには evidence、scope、counterexamples、generalization_rationale、regression_tests を必須とする

## システム構成（コンポーネント依存関係）

- 変更対象: `local_sdlc.py`
  - 依存している: `local_sdlc/cli.py`
  - 依存されている: ユーザーの CLI 操作、既存コマンド互換性
- 変更対象: `local_sdlc/`
  - 依存している: `sdlc-skills/skills/*/SKILL.md`, プロジェクトの `SPEC.md`, OpenAI 互換 LLM API, git コマンド（任意）
  - 依存されている: `local_sdlc.py`, 将来の GUI / API wrapper / 自動化
- 変更対象: `.sdlc-runner/runs/<timestamp>/`
  - 依存している: Supervisor 実行、PM/Coder/Judge の出力
  - 依存されている: 後続エージェントの入力文書、ユーザーレビュー、将来の監査
- 変更対象: `.sdlc-runner/control/<goal-id>/`
  - 依存している: Web UI / CLI stop、human approval、Safety/Suppression Harness
  - 依存されている: Autonomous Supervisor Runtime、子 agent 起動可否、resume/retry 抑止
- 変更対象: `.sdlc-runner/runs/<timestamp>/progress.jsonl`
  - 依存している: API call、stream callback、command runner、artifact apply、evidence gate
  - 依存されている: Watchdog、Web UI、stalled 判定、recovery planner
- 変更対象: `.sdlc-runner/runs/<timestamp>/safety_decisions.jsonl`
  - 依存している: Safety/Suppression Harness、risk classifier、human approval
  - 依存されている: Autonomous Supervisor Runtime、audit、blocked/approval-required UI
- 変更対象: `docs/architecture/autonomous_supervisor_runtime_spec.md`
  - 依存している: 本 SPEC.md、これまでの Tetris / Mini SQLite / Redis 実験で得た停止・安全・過剰適合の知見
  - 依存されている: 今後の実装フェーズ、TDD、review
- 変更対象: `tests/test_local_sdlc.py`
  - 依存している: `local_sdlc.py`, 一時ディレクトリ上のテスト用 SKILL.md
  - 依存されている: 受け入れ条件の検証

---

## アーキテクチャ設計

### コンポーネント構成

- Presentation 層: `argparse` による CLI、標準出力、標準ライブラリHTTPサーバーによるローカルWeb UI
- Application 層: Supervisor、agent loop、stage runner、repair budget、workflow orchestration、Autonomous Supervisor Runtime
- Domain 層: Skill model、生成物プロトコル、failure classification、semantic contract、stage work item、ProgressEvent、SafetyDecision、Core/Discovered Proposition
- Infrastructure 層: OpenAI 互換 HTTP クライアント、streaming、git/subprocess、ファイルシステム読み書き
- Persistence 層: run manifest、run_dir、SPEC.md、progress.jsonl、safety_decisions.jsonl、control token、観測ログ、docs 出力

### ADR

#### ADR-1: 単一 CLI entrypoint + 責務別モジュール分割で実装する
**状況:** 特定の外部エージェント環境なしで実行できる基盤が目的であり、導入時の依存解決を最小化したい。一方で、7,000 行級の単一 Python ファイルは可読性、AI による局所変更、将来の GUI 組み込みを阻害する。
**判断:** 利用者向け entrypoint として `local_sdlc.py` を維持し、実装本体は `local_sdlc/` パッケージへ責務別に分割する。外部パッケージは使わず、標準ライブラリのみを維持する。
**理由:** 「単一プログラム」は起動単位の要件であり、単一ソースファイルの要件ではない。CLI、GUI、将来の API wrapper が同じ Application / Domain / Infrastructure を再利用できる構造にするため。
**影響:** 既存コマンドは `python3 local_sdlc.py ...` のまま維持する。分割リファクタリング中は、各段階で `python3 -m unittest discover -s tests` を通し、単一ファイル版 baseline commit と同等の振る舞いを保つ。

#### ADR-2: LLM にはスキル本文と SPEC.md 全文を渡す
**状況:** 元スキルセットの中核は、SPEC.md とスキル本文を省略せず渡すことにある。
**判断:** `phase` と `implement` では既存 `SPEC.md` 全文をプロンプトに含める。
**理由:** 要約で判断根拠を欠落させると、固定要件や受け入れ条件を逸脱しやすい。
**影響:** コンテキスト長が不足する小型モデルでは入力過多になる可能性があるため、将来は明示的な分割実行を追加する。

#### ADR-3: SKILL.md は system prompt として渡す
**状況:** user prompt にスキル本文・仕様・指示を混ぜると、役割定義と作業入力の境界が曖昧になる。
**判断:** 各 skill call では SKILL.md と PM/Coder/Judge の役割契約を system role に置き、user role には SPEC.md、前段成果物、今回の指示だけを置く。
**理由:** スキルを「エージェント人格/役割」として固定し、入力文書との混線を減らすため。
**影響:** OpenAI 互換 API が system role を扱えることを前提にする。system role 非対応エンジンでは gateway 側の互換が必要。

#### ADR-4: Supervisor は会話履歴ではなく文書でエージェントを接続する
**状況:** PM、Coder、Judge が同じ会話履歴を共有すると、Coder の思い込みやハルシネーションが Judge に伝染する。
**判断:** Supervisor は PM/Coder/Judge を別々の chat completions request で呼び、前段出力を `.sdlc-runner/runs/` の Markdown 文書として保存してから後段へ渡す。
**理由:** 独立した判断を促し、サボり・自己正当化・暗黙補完を減らすため。
**影響:** 1 タスクあたりの API 呼び出し数は増えるが、監査可能性とレビュー品質を優先する。

#### ADR-6: 元リポジトリ準拠の Supervisor Router を PM/Coder/Judge の上位に置く
**状況:** 初期実装では Supervisor を PM/Coder/Judge 固定ループとして扱い、`agents/supervisor.md` と `/sdlc` の「意図分類・危険信号検知・専門スキル選定」コンセプトを十分に移植できていなかった。
**判断:** `supervisor` コマンドを追加し、`agents/supervisor.md` を system prompt として独立 API call で実行する。そのうえで、決定されたフェーズを `spec`, `architect`, `tdd`, `ui`, `review`, `security`, `deploy`, `observe`, `sre`, `refactor` 等の各 SKILL.md に別 API call として渡す。
**理由:** ユーザーが求めていたのは「固定の coding loop」ではなく、元 GitHub の思想に沿った SDLC スキルセット全体の単一プログラム化であるため。
**影響:** `agent` は実装・検証用の下位ループとして残し、`supervisor` が上位の分類・フェーズ制御を担当する。

#### ADR-5: Judge 修正依頼は自動修正ループで Coder に戻す
**状況:** 1周だけの PM→Coder→Judge では、Judge が正しく修正依頼を出しても人間が手で再実行する必要がある。
**判断:** `--auto-fix` が指定された場合だけ、Judge 文書を次ラウンドの Coder 入力として渡し、`--max-rounds` 上限まで反復する。
**理由:** Judge の客観性を保ったまま、明らかな修正サイクルを自動化するため。
**影響:** API 呼び出し数と生成時間は増える。無限ループを避けるため、既定は1周で、反復は明示 opt-in とする。

#### ADR-7: API 設定は role ではなく call function を主キーにする
**状況:** PM/Coder/Judge のロール分離は有効だが、同じ Coder でも新規生成物作成、失敗修復、生成物形式修復では必要な温度・token budget が異なる。
**判断:** `role -> function -> api_profile` の階層にし、実効 API 設定は `base -> role override -> function profile -> explicit function override` の順に合成する。
**理由:** ロールは責務と視点、function は実際の認知処理、api_profile は実行設定という役割が異なるため。関数別 profile により、処理内容ごとに温度・max_tokens・thinking を最適化できる。
**影響:** `--pm-*`, `--coder-*`, `--judge-*` は互換維持するが、より細かい調整は `--api-profile FUNCTION:key=value,...` で行う。関数が増えた場合も profile table と alias 正規化に追加すればよい。

#### ADR-8: プロジェクト依存の境界判断は Project Policy Triage に委譲し、適用は runner が検証する
**状況:** 生成テストを修正してよいか、外部受け入れテストとして固定すべきか、生成物形式の崩れを復旧すべきか拒否すべきかは、プロジェクトと SPEC.md に依存する。これを固定ヒューリスティックだけで増やすと、Mini SQLite や Tetris に過剰最適化された agent になる。
**判断:** Universal Invariants は機械的に強制し、プロジェクト依存の曖昧判断だけを Judge レベルの `project_policy_triage` API call に分類させる。LLM は JSON で case_type / safe_next_action / editable_paths / readonly_paths / forbidden_actions を返すが、直接生成物を適用しない。
**理由:** LLM は文脈判断に有用だが、実行権限を持つと安全性が崩れる。分類と執行を分けることで、汎用性と安全性を両立する。
**影響:** `--project-policy-triage auto` では、テストハーネス所有権やテスト編集可否など境界ケースだけ追加 API call が発生する。分類結果は `run.json.project_policy_triages` と `05-rXX-project-policy-triage.json` に保存され、後続ロールへ文書として渡される。

#### ADR-9: モデル差し替えは名前付き model profile preset と function-level model override で扱う
**状況:** Qwen と Ornith のように、同じ OpenAI 互換 API でもモデルごとに得意な処理、推奨 max_tokens、thinking 制御、応答速度、生成物形式の安定性が異なる。単一の `--model` や散在する CLI flag だけで調整すると、比較実験や将来の mixed-model 運用で設定が失われやすい。
**判断:** `--model-profile` を first-class preset とし、preset は既定 model と function profile を持つ。`--model` は preset の既定 model を上書きし、`--api-profile FUNCTION:model=...` は特定 function/API call だけの model を上書きする。
**理由:** ロールは責務、function は認知処理、model profile はモデル特性、api profile は実行設定を表す。これらを分離すると、Qwen/Ornith の比較や、生成物作成だけ Qwen・失敗分析だけ Ornith のような mixed-model 実験を安全に行える。
**影響:** `doctor` と `run.json` は `model_profile` と role/function 別の有効 model/API 設定を必ず表示・保存する。将来モデルや function が増えても、preset table と function profile table に追加し、散在する条件分岐やハードコードで管理しない。

#### ADR-10: API provider 設定は project config と環境変数で外出しする
**状況:** 初期運用では `http://localhost:30000/v1` のローカル LLM を前提にしていた。しかし今後は Qwen/Ornith などのローカルモデルだけでなく、任意の OpenAI 互換 API や mixed-model 運用を比較する必要がある。
**判断:** `local_sdlc.json/yaml/yml` と `--config-file` を導入し、`llm.base_url`, `llm.model`, `llm.model_profile`, `llm.api_profile`, `llm.function_profiles`, `llm.role_profiles`, `llm.api_key_env` をプロジェクト単位で設定できるようにする。優先順位は `CLI 引数 > project config > 環境変数 > 内蔵デフォルト` とする。
**理由:** API接続先とモデル設定をコードや長いCLI引数に散在させると、比較実験・再実行・公開時の秘匿が難しくなる。project config で provider 設定を外在化し、APIキーは `api_key_env` で環境変数名だけを保存する。
**影響:** 既定のローカル LLM 運用は維持する。実設定ファイルは `.gitignore` し、公開用には `local_sdlc.example.json` のみを管理する。Web UI は設定ファイルを子プロセスへ渡し、画面で入力されたAPIキーはコマンド表示やジョブログへ残さない。

#### ADR-11: Web UI は CLI harness の薄いローカル adapter として実装する
**状況:** ブラウザ型の AI 開発エージェントとして、画面上で依頼を入力し、進捗を追える体験が必要になった。一方で、この agent の安全性は既存 CLI runner の artifact 検査、テスト、Judge、run document 保存に依存している。
**判断:** `local_sdlc.py web` は Python 標準ライブラリの `ThreadingHTTPServer` で単一HTMLを配信し、UI からの依頼を既存 CLI コマンドの argv に変換してローカル子プロセスとして起動する。
**理由:** Web UI が agent harness を再実装すると、CLI とブラウザで挙動が分岐し、安全制御が抜ける。UI は開始・監視・停止だけに限定し、実装判断は既存 Application/Domain/Infrastructure 層へ委譲する。
**影響:** Web UI は完全ローカルで動く。外部Web framework、npm、CDNは不要。ジョブログは `.sdlc-runner/web/jobs/` に残り、CLI実行時の `.sdlc-runner/runs/` と合わせて監査できる。

## テスト計画

### テストケース（受け入れ条件より）

| 受け入れ条件 | テストケース | 結果 |
|---|---|---|
| スキル一覧が表示される | `test_load_skills_reads_front_matter` | PASS |
| doctor が LLM なしで成功 | 手動: `python3 local_sdlc.py doctor --skip-llm` | PASS |
| unittest が成功 | 手動: `python3 -m unittest discover -s tests` | PASS |
| LLM モデル名を表示 | 手動: `python3 local_sdlc.py doctor` | PASS (`Ornith-1.0-35B` 自動選択) |
| 外部パッケージなし | `test_module_imports_without_external_dependencies` | PASS |
| スキル本文が system role に入る | `test_skill_messages_put_skill_body_in_system_role` | PASS |
| Supervisor ステップが独立する | `test_parse_supervisor_steps` | PASS |
| Supervisor PM 実行 | 手動: `python3 local_sdlc.py supervise ... --steps pm --max-tokens 256 --run-dir /tmp/local-sdlc-supervise-check-2` | PASS |
| PM/Coder/Judge が別 API call として実行される | `test_supervise_runs_pm_coder_judge_as_separate_calls` | PASS |
| Coder が文脈なしで走らない | `test_supervise_requires_file_context_for_coder` | PASS |
| 新規ファイル生成は既存 context なしで許可 | `test_supervise_allows_new_file_without_existing_context` | PASS |
| 危険な新規ファイルパスを拒否 | `test_new_file_rejects_unsafe_paths` | PASS |
| timeout 後に health probe を表示 | `test_timeout_error_reports_health_probe_result` | PASS |
| 生成前に短い health preflight を行う | `test_complete_preflights_health_before_generation_timeout` | PASS |
| Judge 修正依頼で Coder/Judge を自動反復 | `test_supervise_auto_fix_loops_until_judge_approval` | PASS |
| Supervisor が本番DB変更で security/deploy を強制 | `test_recommended_sdlc_phases_force_security_for_production_db` | PASS |
| Supervisor が専門スキルを別 API call として順次実行 | `test_supervisor_executes_dynamic_sdlc_phases_as_separate_calls` | PASS |
| function profile が role profile より優先される | `test_complete_uses_function_profile_over_role_profile` | PASS |
| CLI から function profile を上書きできる | `test_build_config_allows_function_profile_overrides_from_cli` | PASS |
| `run-stages` の子 agent に function profile override が伝播する | `test_stage_agent_args_propagate_function_api_profiles` | PASS |
| Qwen/Ornith の名前付き model profile を切り替えられる | `test_build_config_applies_qwen_model_profile_before_cli_overrides`, `test_build_config_supports_ornith_model_profile` | PASS |
| 特定 function/API call だけ model を上書きできる | `test_api_profile_can_override_model_for_one_function` | PASS |
| run manifest に model profile と function-level override が残る | `test_llm_model_profile_manifest_reports_overrides` | PASS |
| 同じ実行失敗、または同じ失敗テスト群が繰り返された場合、Failure Analysis を独立 API call として保存し、Root cause 修復へ文書経由で渡す | `test_agent_routes_repeated_same_failure_to_root_cause_repair`, `test_command_failure_family_signature_ignores_assertion_payload_drift` | PASS |
| 安全に一意復元できる fenced SEARCH/REPLACE 形式の生成物は正規化して適用可能な生成物として扱い、危険な loose function replacement と混同しない | `test_extract_fenced_search_replace_normalizes_extra_colon_path`, `test_loose_python_function_replacement_does_not_swallow_conflict_markers` | PASS |
| 生成テストハーネス所有権のようなプロジェクト依存判断では Project Policy Triage を独立 API call として保存し、repair advice に反映できる | `test_agent_runs_project_policy_triage_for_generated_test_harness_ownership` | PASS |
| 受け入れ条件ごとの証拠不足を gate として扱い、未証明のまま承認しない | `test_acceptance_criteria_parse_plain_bullets_and_gate_unverified`, `test_agent_manifest_records_acceptance_matrix_and_failure_classifier` | PASS |
| browser-tetris-smoke が Start 後の可視 active piece 不在を失敗として扱う | `test_browser_tetris_smoke_requires_visible_active_piece` | PASS |
| acceptance gate blocker を次ラウンドの repair advice へ変換できる | `test_repair_advice_converts_acceptance_gate_blockers_to_actions` | PASS |
| Web 初回 `agent` 新規作成時に最小SPECをbootstrapし、Webジョブから成果物生成・プレビューまで到達できる | `test_web_bootstrap_spec_creates_minimal_spec_for_first_agent_run`, `test_web_bootstrap_spec_preserves_existing_or_explicit_spec`, fake LLM Web fullsite smoke | PASS |
| cancel token を永続化し、cancel 済み resume を API call 前に拒否する | `test_request_cancel_writes_cancel_json`, `test_agent_refuses_cancelled_resume_before_llm_call` | PASS |
| work-start progress event を記録し、cancel 後の work-start を検出できる | `test_work_start_progress_is_blocked_after_cancel` | PASS |
| cancel 済み `run-stages` が stage agent call を開始しない | `test_run_stages_refuses_cancelled_run_before_stage_agent_call` | PASS |
| command action の SafetyDecision を実行前に記録し、approval-required / blocked command を停止する | `test_run_checked_command_records_allowed_safety_decision`, `test_run_checked_command_records_approval_required_safety_decision`, `test_run_checked_command_records_blocked_safety_decision`, `test_run_checked_command_requires_approval_for_risky_class_without_legacy_block_reason`, `test_agent_applies_patch_and_runs_test_command` | PASS |
| S07a: artifact extraction/apply primitives を `artifact_ops.py` へ分離しても `artifacts.py` 経由の既存 API が維持される | `test_extract_json_file_and_search_replace_artifacts`, `test_extracts_fenced_file_artifact`, `test_extracts_fenced_search_replace_artifact`, `test_agent_applies_patch_and_runs_test_command`, full suite | PASS |
| S07b: 巨大化した `tests/test_local_sdlc.py` から safety / cancel control / artifact_ops の焦点テストを分離しても既存挙動が維持される | `tests.test_safety`, `tests.test_cancel_control`, `tests.test_artifact_ops`, `tests.test_local_sdlc`, full suite | PASS |
| S07c: `stage-plan` / `run-stages` / stage queue の焦点テストを分離しても段階実行の既存挙動が維持される | `tests.test_stage_runner`, `tests.test_local_sdlc`, full suite | PASS |
| S02: HTML/browser smoke は harness plugin として Evidence を返し、既存 `run_html_smoke_checks()` / `run_browser_tetris_check()` の互換挙動を維持する | `tests.test_harnesses`, `test_html_smoke_flags_broken_tetris_file`, `test_browser_tetris_smoke_requires_visible_active_piece`, full suite | PASS |

### テスト環境

- フレームワーク: `unittest`
- 環境: ホスト Python 3（標準ライブラリのみ）
- 実行コマンド: `python3 -m unittest discover -s tests`

## レビュー結果

### 判定: 初期実装として承認

### 確認結果
- `python3 -m unittest discover -s tests`: 13 tests OK
- `python3 -m py_compile local_sdlc.py tests/test_local_sdlc.py`: OK
- `python3 local_sdlc.py doctor --skip-llm`: OK
- `python3 local_sdlc.py doctor`: `Ornith-1.0-35B` を検出して自動選択
- `python3 local_sdlc.py phase review --max-tokens 64 ...`: chat completions 応答 `準備完了`
- `python3 local_sdlc.py supervise ... --steps pm --max-tokens 256 --run-dir /tmp/local-sdlc-supervise-check-2`: 1 API call、PM 文書と `run.json` 保存を確認
- `python3 local_sdlc.py supervise ... --steps pm,coder,judge --include local_sdlc.py --max-tokens 192 --run-dir /tmp/local-sdlc-supervise-check-4`: 3 API calls、PM/Coder/Judge 文書保存を確認
- `chat_template_kwargs.enable_thinking=false` を既定送信し、Ornith の reasoning-only 応答で `message.content` が空になる問題を回避
- Coder は `--include` でファイル本文を渡すことを既定で必須化し、文脈なしのコード言及を防ぐ。例外は `--allow-no-context` 明示時のみ
- 新規作成だけのタスクでは `--new-file tetris.html` のように対象を明示すれば、既存ファイル本文なしで Coder を実行できる
- LLM API request timeout は `RunnerError` に変換し、`--timeout 600` などの再実行案を表示する
- 生成 timeout 時は短い `/v1/models` ヘルスチェックを追加実行し、`API health after timeout: alive/unreachable` を表示する
- 生成前にも短い `/v1/models` preflight を実行し、`alive` が取れてから `/chat/completions` の長い timeout を使う
- Linux 上では `SIGALRM` により `/chat/completions` の壁時計 timeout も強制する。ソケットが少しずつ読み取り続ける場合でも全体時間で止める
- `python3 local_sdlc.py health`: `alive (/v1/models OK: Ornith-1.0-35B)` を確認
- `--auto-fix --max-rounds N` で Judge 修正依頼を次ラウンド Coder へ渡し、承認時に `final_verdict: approved` で停止することを単体テストで確認
- `supervisor` コマンドで `agents/supervisor.md` を system prompt とした独立 API call を実行し、run_dir に `00-deterministic-route.md`, `01-supervisor-routing.md`, `run.json` を保存することを確認
- `supervisor --execute` は各フェーズを別 chat completions request として実行する。単体テストでは supervisor + spec + architect + tdd + review の 5 API call を確認
- 危険信号検出では、本番DB変更に対して `spec -> architect -> security -> deploy -> observe` 系のゲートを組み込めることを確認

### 残リスク
- `implement --apply` は `git apply --check` 付きだが、LLM 生成パッチの意味的妥当性は人間レビューが必要
- 大規模プロジェクトでは SPEC.md とファイル本文がコンテキスト上限を超える可能性があるため、将来は分割実行が必要
- PM/Coder/Judge の完全 3 ステップ実行は token 消費が大きいため、実利用では対象ファイルを絞って `--include` する

---

## 追加計画: Coding Agent 基本機能

### 背景

現状の `local_sdlc.py` は、PM/Coder/Judge を独立 API call として呼び、文書成果物と unified diff 提案を保存できる。しかし、一般的な coding agent として期待される「調査 → 編集 → 実行 → 結果観察 → 修正 → 再検証」の自律ループはまだ薄い。

そのため、現状は「SDLC 文書ランナー + パッチ提案器」に近く、実ファイルを作り、テストを実行し、失敗ログを読んで修正し、最終成果物を検証する agent 基盤としては未完成である。

### 追加する機能の目的

| 機能・コンポーネント | 目的 | 変えてはならない本質 |
|---|---|---|
| Workspace Inspector | git 状態、ファイル一覧、対象ファイル候補、既存構成を自動収集する | 実際に読んでいないファイルを読んだことにしない |
| Patch Extractor | LLM 出力から unified diff だけを抽出・検証する | 説明文や自己判定をパッチとして扱わない |
| Patch Applier | `git apply --check` 後に安全に作業ツリーへ反映する | デフォルトでは破壊的変更や commit をしない |
| Command Runner | テスト・lint・静的確認・軽量実行を timeout 付きで実行する | 危険コマンド、sudo、削除、全消去を自動実行しない |
| Observation Log | コマンド結果、exit code、stdout/stderr、生成ファイルを run_dir に保存する | エージェント間の情報交換は文書成果物に限定する |
| Repair Loop | 失敗ログを Coder に渡して最小修正を繰り返す | 無限ループせず `--max-rounds` で必ず止める |
| Judge Gate | 最終ファイル、diff、テストログ、受け入れ条件を客観レビューする | Coder の自己評価で完了扱いにしない |
| Handoff Compactor | auto-fix 時に過去全文ではなく必要情報だけを渡す | SPEC.md 全文と固定要件は保持し、過去 Coder 全文を無制限に膨らませない |

### 新しい CLI 設計

#### `agent` コマンド

```bash
python3 local_sdlc.py agent "<依頼>" \
  --new-file tetris.html \
  --test-command "python3 -m py_compile local_sdlc.py" \
  --max-rounds 3 \
  --apply
```

`agent` は以下を一連のループとして実行する。

1. `doctor` 相当の preflight
2. git status と project manifest の記録
3. PM control document 生成
4. Coder に最小 unified diff を生成させる
5. diff 抽出・検証
6. `--apply` 指定時のみ `git apply --check` → `git apply`
7. `--test-command` を安全判定後に実行
8. command result を文書化
9. Judge が SPEC、PM、diff、実行結果を確認
10. 修正依頼なら Coder に「最新 Judge 指摘 + 最新テストログ + 最新 diff 要約」だけ渡す
11. 承認または `--max-rounds` 到達で停止

### 実装フェーズ

#### Phase 1: 出力制御と handoff 圧縮

- Coder の output contract を「unified diff のみ」に強化する
- Coder 出力に `判定`, `Judge review`, Markdown 解説が混ざった場合はパッチ候補から除外する
- `extract_unified_diff(text)` を追加する
- `--max-handoff-chars` を追加し、auto-fix の次ラウンドでは過去 Coder 全文を渡さない
- 次ラウンド Coder へ渡す情報は以下に限定する
  - SPEC.md 全文
  - PM control document
  - 最新 Judge review
  - 最新 command/test result
  - 最新 diff の短い要約または抽出済み diff

#### Phase 2: Patch Applier と作業ツリー反映

- `supervise` に `--apply` を追加する
- Coder 出力から抽出した patch を `NN-rXX.patch` として保存する
- `git apply --check` が通った patch だけ反映する
- 反映前後の `git diff --stat` と `git diff --name-only` を run_dir に保存する
- `--dry-run` を既定にし、`--apply` 明示時だけ書き込む

#### Phase 3: Command Runner

- `--test-command` を複数指定可能にする
- 実行前に `dangerous_command_reason()` で安全判定する
- コマンドごとに timeout を設定する
- exit code、stdout、stderr、実行時間を `command-rXX-NN.md` に保存する
- 失敗ログは次 Coder/Judge の文書入力に含める

#### Phase 4: Agent Loop

- `agent` コマンドを追加し、PM → Coder → Apply → Test → Judge → Repair をまとめて実行する
- `supervise` は従来通り文書生成中心の低リスクコマンドとして残す
- `agent` は coding agent 実行用、`supervise` は設計・レビュー・文書交換用に役割分離する

#### Phase 5: 検証と監査

- run_dir に `run.json`、各 agent 文書、patch、command result、final summary を保存する
- `final_verdict` は Judge の出力と実コマンド結果から決める
- `accepted`, `needs_changes`, `blocked`, `patch_failed`, `test_failed` を明示する
- `replay` 可能なように実行引数と環境観測を記録する

### 受け入れ条件

- [x] `python3 local_sdlc.py agent "..." --new-file tetris.html --apply` が新規ファイルを実際に作成できる
- [x] Coder 出力に説明文が混ざっても、保存・適用される patch は unified diff 部分だけになる
- [x] `git apply --check` に失敗した場合、ファイルは変更されず `patch_failed` で停止する
- [x] `--test-command` の stdout/stderr/exit code が run_dir に保存される
- [x] テスト失敗時、次ラウンド Coder は失敗ログを根拠に最小修正 patch を生成する
- [x] auto-fix の次ラウンド prompt が過去 Coder 全文を無制限に含まない
- [x] Judge は Coder 出力だけでなく、実際の patch 適用結果と command result を見て判定する
- [x] 危険コマンドは `BLOCKED` として実行されない
- [x] `python3 -m unittest discover -s tests` が成功する
- [x] 単一 HTML 成果物は inline script の構文確認を自動実行できる
- [x] Tetris 依頼では headless browser smoke により、キーボード操作、ライン消去、ゲームオーバー停止を検証できる
- [x] 完成済み成果物が初期検証で PASS した場合、Coder を呼ばず `final_verdict: approved` で停止できる
- [x] Judge 本文中の説明語ではなく、`判定:` / `Verdict:` 行を優先して承認・修正依頼を判定できる

### 実装済みの追加成果

- `agent` コマンドを追加し、PM → Coder → Apply → HTML/browser smoke → Judge → repair loop を実行できる
- Coder 成果物として unified diff、`BEGIN_FILE`、`BEGIN_APPEND_FILE`、`BEGIN_SEARCH_REPLACE` を処理できる
- `run_html_smoke_checks()` に Tetris 専用の静的断片確認と headless Chromium 検証を追加した
- `judge_approved()` は判定行を優先し、本文に含まれる「修正依頼時」などの説明語で誤判定しない
- 初期 HTML/browser smoke が全て PASS し、追加 test command がない場合は再編集せず承認で止まる

### 実装検証（2026-07-04）

- `python3 -m py_compile local_sdlc.py tests/test_local_sdlc.py`: PASS
- `python3 -m unittest discover -s tests`: 23 tests PASS
- `run_html_smoke_checks(..., ["tetris.html"], tetris_checks=True)`: HTML smoke PASS / browser-tetris-smoke PASS
- `python3 local_sdlc.py agent "tetris.htmlの実動作を検証し、必要なら最小修正する" --include tetris.html --apply --max-rounds 3 --timeout 600 --max-tokens 1000 --command-timeout 30`: `api_calls: 1`, `final_verdict: approved`

### 優先順位

1. Phase 1 と Phase 2 を最優先で実装する。これにより「巨大出力で遅い」「diff以外が混ざる」「実ファイルが作られない」を解消する。
2. Phase 3 を追加し、coding agent に必須の実行・観察能力を持たせる。
3. Phase 4 で `agent` コマンドとして統合する。
4. Phase 5 で監査性と再現性を高める。

### 実装上の制約

- `local_sdlc.py` は薄い CLI entrypoint として維持する
- 実装本体は `local_sdlc/` パッケージへ責務別に分割する
- Python 標準ライブラリのみを維持する
- デフォルトでは dry-run とし、実ファイル変更は `--apply` 明示時のみ
- git commit は自動実行しない
- 破壊的コマンドはユーザー承認なしに実行しない
- SPEC.md と固定要件を Coder/Judge の根拠として維持する

### 目標モジュール構成

```text
local_sdlc.py                  # 互換用の薄い CLI entrypoint
local_sdlc/
  __init__.py
  cli.py                       # argparse と command dispatch
  config.py                    # API/role/function profile 設定
  models.py                    # dataclass と共有型
  llm_client.py                # OpenAI 互換 API、health、streaming
  skills.py                    # SKILL.md 読み込みと prompt assembly
  artifact_ops.py              # JSON/BEGIN_FILE/diff/search_replace parser と artifact apply
  artifacts.py                 # artifact facade、stream guard、semantic lint、repair advice
  protocol.py                  # failure classification と repair policy
  supervisor.py                # PM/Coder/Judge と router
  agent_loop.py                # apply/test/judge repair loop
  stages.py                    # stage planning と stage execution
  commands.py                  # subprocess 実行、command observation
  persistence.py               # run_dir、manifest、docs 出力
tests/
```

分割は段階的に行い、各段階で `python3 -m unittest discover -s tests` を通す。リファクタリング中は CLI 互換性を維持し、外部からの起動方法を変えない。

---

## 追加検討: 実用 Coding Agent に必要な周辺機能

### 位置づけ

前節の `Coding Agent 基本機能` は、最小の閉ループである「patch 生成 → 適用 → test → repair → judge」を成立させるための計画である。

ただし、実用的な coding agent として継続的に使うには、それ以外にも context 管理、安全性、再開性、モデル制御、評価の仕組みが必要になる。

### 必須度 A: 早期に必要な機能

| 機能 | 目的 | 理由 |
|---|---|---|
| Relevant File Discovery | 依頼内容から読むべきファイル候補を `rg`, manifest, 拡張子、設定ファイルから推定する | 毎回 `--include` を手で指定すると agent として弱い |
| Context Budget Manager | SPEC、PM文書、ファイル本文、ログを token/文字数予算内に収める | handoff 肥大化と timeout を防ぐ |
| Structured Agent Output | Coder/Judge の出力を `patch`, `summary`, `tests`, `verdict`, `required_fixes` に分離する | prose 混入や自己判定の混線を防ぐ |
| Run Resume | `run_dir` から途中再開できるようにする | 長い生成や timeout 後に最初からやり直さない |
| Snapshot/Rollback | patch 適用前の状態を記録し、失敗時に戻せるようにする | `--apply` を安全にする |
| Secret Redaction | `.env`, API key, token らしき値をログや prompt から除外・マスクする | LLM prompt と run_dir への秘密情報混入を防ぐ |
| Prompt Injection Guard | リポジトリ内文書や生成物を「命令」ではなく「データ」として扱う境界を明示する | README や依存ファイル内の悪意ある指示で agent が逸脱しないようにする |

### 必須度 B: 実用性を大きく上げる機能

| 機能 | 目的 | 理由 |
|---|---|---|
| Test Command Detection | `package.json`, `pyproject.toml`, `Makefile`, `go.mod`, `Cargo.toml` などからテスト候補を推定する | ユーザーが毎回 `--test-command` を指定しなくても検証できる |
| Command Allowlist/Policy | 実行してよいコマンド種別を設定できるようにする | `npm test` は許可、`rm -rf` は禁止のような運用が必要 |
| Resource Guard | command timeout、最大出力サイズ、最大ファイルサイズ、最大実行回数を制限する | runaway process や巨大ログで詰まるのを防ぐ |
| Progress/Streaming | 長い LLM 生成やテスト実行中に現在ステップを表示する | 「止まっているのか動いているのか」が分かる |
| Failure Classifier | 失敗を `llm_timeout`, `patch_invalid`, `test_failed`, `missing_context`, `blocked_command` に分類する | 次の repair 方針を機械的に選びやすくする |
| Acceptance Matrix | SPEC.md の受け入れ条件ごとに確認コマンドと結果を対応づける | 「テストは通ったが仕様を満たしたか」を分離できる |
| Dirty Worktree Guard | 作業前に未コミット変更を検出し、触るファイルを限定する | ユーザー作業を壊さない |
| Changed File Review | 最終的に変更されたファイルだけを Judge に渡す | Judge の context を圧縮し、レビュー品質を上げる |

### 必須度 C: 成熟した agent に必要な機能

| 機能 | 目的 | 理由 |
|---|---|---|
| Temporary Worktree Mode | `git worktree` や一時コピーで試行し、成功 patch だけ本体へ戻す | 安全に試行錯誤できる |
| Tool Transcript Replay | LLM入力、出力、patch、command結果を再現できる形式で保存する | 失敗分析と regression test に必要 |
| Agent Eval Harness | 既知の小タスクを fake/real LLM で回し、成功率と失敗類型を測る | agent 自体の品質を継続評価する |
| Model Capability Profile | `/v1/models` のモデルごとに context 長、thinking制御、推奨 max_tokens を設定する | ローカルLLM差し替え時の不安定性を下げる |
| Retry/Backoff Policy | 一時的な HTTP 失敗、空出力、reasoning-only 出力に再試行方針を持つ | ローカル推論APIの揺らぎに耐える |
| Config File | `local_sdlc.yaml` 等で base_url、model、timeout、allowlist、test commands を固定する | CLI引数が長くなりすぎるのを防ぐ |
| Project Memory | プロジェクト固有のテスト方法、設計判断、禁止事項を文書として保存・再利用する | 毎回同じ探索を繰り返さない |

### 追加 CLI 案

```bash
# 依頼から関連ファイル候補だけを調べる
python3 local_sdlc.py inspect "ログイン画面のバグを直して"

# 前回 run_dir から再開する
python3 local_sdlc.py agent --resume .sdlc-runner/runs/20260704-120000

# プロジェクトのテストコマンド候補を表示する
python3 local_sdlc.py detect-tests

# run_dir の成果物を要約し、次の一手を表示する
python3 local_sdlc.py summarize-run .sdlc-runner/runs/20260704-120000
```

### 設計判断

- 最初に実装すべきは A 群のうち `Structured Agent Output`, `Context Budget Manager`, `Run Resume`, `Secret Redaction`。
- `Relevant File Discovery` は便利だが、誤読・読み漏れのリスクがあるため、最初は候補提示に留め、実際に prompt に入れるファイルはログへ明示する。
- `Temporary Worktree Mode` は安全性が高いが実装量が増えるため、Patch Applier と Command Runner が安定してから導入する。
- `Project Memory` は docs との整合が重要。共有正典にすべき内容は `~/projects/docs/`、プロジェクト固有の実行設定はリポジトリ内 config に分ける。

### 追加受け入れ条件

- [ ] `inspect` が依頼文から関連ファイル候補を表示し、読んだ根拠を説明できる
- [ ] agent 実行時、prompt に入れたファイル一覧と文字数が run_dir に保存される
- [ ] `.env` や秘密値らしき文字列は prompt/log にそのまま保存されない
- [x] timeout や中断後に `--resume` で次ラウンドから再開できる
- [ ] `detect-tests` が主要言語のテスト候補を表示できる
- [x] command 出力が最大サイズを超えた場合、安全に truncate される
- [x] 最終 summary が `approved`, `blocked`, `needs_changes`, `patch_failed`, `test_failed`, `llm_timeout` 相当の状態を明示する

### 2026-07-04 実装済み: Agent 汎用改善 1-5

ユーザーとの Tetris / Redis benchmark 実行で見えた同種問題を、個別タスク用の場当たり対応ではなく agent 基盤の機能として追加した。

| 実装 | CLI / 証跡 | 解決する同種問題 |
|---|---|---|
| Acceptance Matrix | `run.json.acceptance_criteria`, `evidence`, `acceptance_matrix` | 「テストが通った」ことと「仕様のどの条件を満たしたか」が混ざる問題 |
| Run Resume | `agent --resume <run_dir>` | timeout / 中断 / 上限到達後に最初からやり直して文脈と時間を失う問題 |
| JSON 生成物 Parser | `--artifact-format auto|json|legacy` | 複数ファイルや長文成果物で `BEGIN_FILE` や diff が崩れる問題 |
| Failure Classifier | `run.json.failure_summary`, `evidence[].failure_type` | 失敗ログを毎回 LLM に読ませないと次の修復方針が決まらない問題 |
| Temporary Worktree Mode | `agent --worktree-mode copy` | 修復ループ中に本体作業ツリーを壊す問題 |

#### 実装詳細

- `--artifact-format auto` は JSON 形式の生成物を先に試し、なければ従来の search/replace、file 形式の生成物、unified diff を使う。
- `--artifact-format json` は JSON 形式の生成物契約違反を `patch_failed` / `artifact_invalid` として扱い、勝手に diff として解釈しない。
- `--resume` は前回 `run.json` と Markdown 文書を読み、`completed_rounds + 1` から repair round を続ける。
- `--worktree-mode copy` は一時ディレクトリへプロジェクトをコピーして適用・テストし、`final_verdict: approved` の場合だけ許可済み対象ファイルを元プロジェクトへ戻す。
- command result parser は既存 run 文書のインデント差にも耐え、`SyntaxError`, timeout, blocked command, missing executable などを分類できる。

#### 追加受け入れ条件

- [x] `extract_json_artifacts()` が `replace_file`, `append_file`, `search_replace` を検証して取り出せる
- [x] `agent --artifact-format json` が JSON 形式の生成物を実ファイルへ適用し、コマンド PASS で承認できる
- [x] command 失敗時、`run.json` に `evidence`, `acceptance_matrix`, `failure_summary.failure_type` が保存される
- [x] `py_compile` の `SyntaxError` は `syntax_error` として分類される
- [x] `agent --resume <run_dir>` が既存文書を Coder 入力に渡し、次ラウンド番号から続行できる
- [x] `agent --worktree-mode copy` は一時コピー上で編集・テストし、承認後に許可対象ファイルだけ本体へ戻せる
- [x] `python3 -m unittest discover -s tests` が 46 tests PASS する

---

## 2026-07-04 追記: Benchmark 型タスクの分割実行

### 背景

Redis 互換ミニ KVS のような複数ファイル・複数工程の benchmark では、1 回の Coder call に全ファイル生成、テスト設計、レビュー記録、修正まで詰め込むと、ローカル LLM の生成時間が長くなりすぎる。

### 設計判断

- Coder は複数 `BEGIN_FILE` ブロックを返せるため、複数ファイル生成は可能。
- ただし実運用では `resp.py`, `store.py`, `commands.py`, `server.py`, `tests/`, `README.md`, `PROCESS.md` のように小さい単位へ分け、各 call の責務を絞る。
- 部分生成中に総合 Redis smoke を走らせると、まだ存在しない `server.py` や未完成機能で失敗し、repair 方向が乱れる。
- そのため `agent --redis-smoke auto|always|never` を追加し、部分生成では `never`、最終統合では `always` を使えるようにする。

### 追加受け入れ条件

- [x] `agent --redis-smoke never` で、パス名に `resp.py` が含まれていても Redis smoke を抑止できる
- [x] `agent --redis-smoke always` で、依頼文やファイル名に関係なく Redis smoke を実行対象にできる
- [x] `agent --skip-pm` で、明確な小修復時に PM API call を省略できる
- [x] `agent --judge-mode command-only` で、コマンド/smoke PASS を最終ゲートにできる
- [x] 適用成果物に conflict marker が含まれる場合は書き込み前に拒否できる
- [x] `BEGIN_SEARCH_REPLACE` の置換が no-op の場合は、適用前に拒否できる
- [x] file/search-replace 形式の生成物本文に別の生成物 marker が混入した場合は、適用前に拒否できる
- [x] semantic contract 抽出後の修復ラウンドは `semantic_repair` function profile に切り替わり、1つの短い `BEGIN_SEARCH_REPLACE` または1ファイル unified diff だけを許可できる
- [x] semantic repair mode では JSON 形式の生成物、whole-file 形式の生成物、複数生成物、テスト編集、巨大 search/replace を lint で拒否できる
- [x] semantic repair mode の壊れた出力は `semantic_repair_missing_path` などの具体的 failure_type に分類し、次ラウンドを `format_repair` へ遷移できる
- [x] semantic contract の `focus_files` は次ラウンドの context に自動追加し、製品コード focus は修正可能 target、テスト focus は read-only evidence として扱える
- [x] `MISSING_CONTEXT` は semantic repair mode でも生成物 lint より先に処理し、箇条書きの file path も読み取れる
- [x] `format_repair` でも `format_repair_missing_path`, `format_repair_no_artifact` などの具体的 failure_type に分類し、生成物のみを許す契約を機械的に強制できる
- [x] stream guard は JSON 形式の生成物反復だけでなく、同一 token/line の汎用反復 runaway を `stream_repeated_text_runaway` として早期停止できる
- [x] `--protocol-repair-rounds` により生成物プロトコル修復予算を機能修復の `--max-rounds` から分離できる
- [x] 全 skill API call の system prompt に `G=(V,E)` graph model と `P* and C* and G* and E* |- A or V` の導出規則を注入できる
- [x] PM/Judge の文書出力契約に `Proposition Ledger` と `Graph Edges` を含め、coder には生成物だけを出力する時も内部で同じ命題/graph reduction を要求できる
- [x] Redis benchmark では、Agent 自身が `server.py`, parser/store/command 層, tests, README, PROCESS.md を生成し、unittest と Redis smoke を通す
  - 確認コマンド: `timeout 30 python3 -m unittest discover -s tests` in `benchmarks/redis-kvs`
  - 結果: `Ran 63 tests in 0.820s OK`
  - Redis smoke 証跡: `.sdlc-runner/runs/redis-part-23-process-md/05-r01-redis-smoke.md` が PASS

---

## 2026-07-15 追記: Agent Harness 汎用化・完遂性強化仕様

### 背景

Tetris、Mini SQLite、Redis/KVS の反復検証では、失敗のたびに harness を強化することで前進できた。一方で、個別失敗への条件分岐をそのまま増やすと、特定課題に過学習した agent になり、未知タスクへの汎用性が落ちる。

今後は個別 smoke や repair rule を増やすだけでなく、失敗を以下の汎用制御モデルへ昇格させる。

```text
Requirement
  -> Observable
  -> Evidence
  -> Verdict
  -> Repair Action
  -> Regression Memory
```

この節は、agent harness を「トラブル都度の拡張」から「再利用可能な完遂制御」へ移行するための実装仕様である。

### 目的

- SPEC.md の受け入れ条件を、実行可能または観測可能な命題として扱う。
- 「テストが通った」と「仕様の各条件が証明された」を分離する。
- domain-specific smoke を core runner から切り離し、plugin 的に追加できるようにする。
- failure / repair / acceptance / history を構造化し、同種失敗を次回の事前検証に利用する。
- 巨大化した `artifacts.py` / `agent_runner.py` を責務別に分割し、AI と人間が安全に変更できる構造に戻す。

### スコープ

#### やること

- Requirement / Observable / Evidence / Verdict / Repair Action の domain model を追加する。
- 既存 `acceptance_matrix` を新しい domain model の投影結果として再実装する。
- HTML/browser、Python package、CLI、storage/DB などの harness を plugin 境界へ分離する。
- repair advice を generic rules と domain rules へ分離する。
- stage planner を、要求・観測・証拠・編集可能範囲・API profile を持つ work item へ強化する。
- failure history を regression memory として保存し、次回以降の検証候補に使える形へ正規化する。
- 既存 CLI 互換性と標準ライブラリのみの制約を維持する。

#### やらないこと

- LLM に直接ファイル適用権限を渡さない。
- 個別課題だけに合わせた hard-code を core runner に追加しない。
- 外部クラウド API、外部 Python package、常駐DBを前提にしない。
- 既存 `local_sdlc.py` CLI entrypoint を変更しない。
- 仕様・設計・テストなしに大規模分割だけを先行しない。

### 固定要件

- `local_sdlc.py` は互換 entrypoint として維持する。
- 標準ライブラリのみを使う。
- `python3 -m unittest discover -s tests` が各段階で通ることを必須 gate とする。
- `approved` は、認識された acceptance blocker が存在しない場合だけ許可する。
- `fail` と `unverified` は区別するが、どちらも approval blocker として扱う。
- domain-specific knowledge は core runner ではなく harness / repair plugin 層へ隔離する。
- LLM の判断は分類・提案に限定し、生成物適用・command 実行・approval は runner の機械検証を通す。
- run manifest には、要求、証拠、判定、failure history、実効 API profile を監査可能な形で保存する。
- 既存の Tetris / Mini SQLite / Redis の regression は、個別対応ではなく汎用制御の検証として維持する。

### Domain Model

#### Requirement

SPEC.md から抽出される受け入れ命題。

必須フィールド:

```text
id
text
source_path
source_section
required_observables
status
```

`status` は `pending`, `covered`, `blocked` のいずれか。

#### Observable

Requirement を証明するために観測すべき対象。

例:

- command が exit code 0 で終了する
- `#game-board .cell` が 200 個存在する
- Start 後に active piece が可視セルとして現れる
- CLI の連続実行後に永続化状態が残る
- public API が import 可能で、指定例外を送出する

必須フィールド:

```text
id
kind
target
expected
harness
timeout
```

#### Evidence

Observable を実行・観測した結果。

必須フィールド:

```text
id
observable_id
kind
status
command
exit_code
document
covers
observations
failure_type
```

#### Verdict

Requirement と Evidence を照合した判定。

許可値:

```text
pass
fail
unverified
blocked
invalid_evidence
```

意味:

- `pass`: Requirement に対応する十分な passing evidence がある
- `fail`: Requirement に対応する failing evidence がある
- `unverified`: Requirement に必要な evidence が存在しない
- `blocked`: 実行環境・安全ポリシー・リソース不足で検証不能
- `invalid_evidence`: evidence 自体が壊れている、または観測対象と対応していない

#### Repair Action

Verdict blocker から導かれる次ラウンド指示。

必須フィールド:

```text
id
source_verdict_ids
strategy
focus_files
readonly_evidence_paths
instructions
forbidden_actions
api_profile
```

#### Regression Memory

再発防止に使う失敗履歴。

必須フィールド:

```text
failure_family
trigger
false_positive_pattern
required_future_observables
fixed_by
regression_tests
scope
```

### システム構成

目標モジュール構成:

```text
local_sdlc/
  requirements.py             # Requirement / Observable / Verdict model
  evidence.py                 # Evidence record, coverage, manifest projection
  harnesses/
    __init__.py
    base.py                   # Harness interface
    html_browser.py           # HTML/browser/DOM behavior smoke
    python_package.py         # py_compile/import/unittest/API probe
    cli.py                    # CLI command sequence and stdout assertions
    storage.py                # persistence/state probes
  repair/
    __init__.py
    advice.py                 # RepairAction model and rendering
    generic_rules.py          # acceptance gap, syntax error, missing artifact
    html_rules.py             # browser/DOM behavior advice
    python_rules.py           # import/API/exception advice
    policy_triage.py          # LLM triage adapter
  history.py                  # failure history and regression memory
  stage_planner.py            # strengthened stage queue synthesis
```

既存モジュールからの移動方針:

- `stages.py` の acceptance parsing / matrix logic は `requirements.py` と `evidence.py` へ移す。
- `verification.py` の browser / HTML / command smoke は `harnesses/` へ分割する。
- `artifacts.py` の repair advice logic は `repair/` へ分割する。
- `agent_runner.py` の loop 内に直書きされた gate / evidence / repair 接続は Application 層の orchestrator に残し、domain logic は新モジュールへ委譲する。

### 実行フロー

```text
1. SPEC.md から Requirement を抽出する
2. Requirement から Observable 候補を生成する
3. Stage Planner が stage ごとに Observable / writable paths / readonly evidence / API profile を割り当てる
4. Coder が生成物を作る
5. Runner が生成物 lint と apply check を行う
6. Harness plugin が Observable を実行して Evidence を生成する
7. Verdict Engine が Requirement と Evidence を照合する
8. blocker があれば Repair Action を生成する
9. 同種失敗が繰り返されたら Failure Analysis / Project Policy Triage を呼ぶ
10. 完了または上限到達時に Regression Memory を更新する
```

### 実装フェーズ

#### S01: Requirement / Evidence / Verdict model 分離

- `requirements.py` と `evidence.py` を追加する。
- 既存 `build_acceptance_matrix()` の外部挙動を維持する。
- `run.json.acceptance_matrix` は互換維持する。
- 新規内部表現として `requirements`, `observables`, `verdicts` を保存できるようにする。

完了条件:

- 既存 acceptance matrix 関連テストが通る。
- `fail`, `unverified`, `invalid_evidence` の区別を単体テストで確認する。

#### S02: Harness plugin interface

- `Harness` interface を定義する。
- HTML/browser smoke を `harnesses/html_browser.py` へ移す。
- 既存 `run_html_smoke_checks()` の CLI 挙動は維持する。
- harness は `Evidence` を返し、直接 `approved` を決めない。

完了条件:

- Tetris の false positive regression が fail になる。
- HTML smoke / browser smoke の既存テストが通る。

#### S03: Python / CLI / Storage harness 分離

- py_compile、unittest、import/API probe、CLI state probe、storage persistence probe を harness として整理する。
- Mini SQLite で使った mechanical probe を core から domain harness へ移す。

完了条件:

- Mini SQLite S01-S03 相当の regression が既存挙動と同等に通る。
- test files は原則 readonly evidence として扱われる。

#### S04: Repair Action model と repair rules 分離

- `repair/advice.py` を追加する。
- acceptance gate blocker から Repair Action を生成する。
- generic rules と domain rules を分離する。

完了条件:

- acceptance blocker が次ラウンド prompt に具体命題として渡る。
- HTML固有の advice が core artifact parser に依存しない。

#### S05: Stage Planner 強化

- stage work item に `requirements`, `observables`, `writable_paths`, `readonly_evidence_paths`, `api_profile`, `max_rounds` を持たせる。
- 仕様が大きい場合は stage を小さく切る。
- stage ごとに「何を証明すれば次へ進めるか」を明示する。

完了条件:

- Mini SQLite の stage resume が、S03以降から再開可能。
- 1 stage の観測対象と修復対象が run manifest で確認できる。

#### S06: Regression Memory

- failure history を `history.py` で正規化する。
- false positive pattern と required future observable を保存する。
- Tetris の active piece invisible 問題を regression memory として記録する。

完了条件:

- 過去 false positive と同種の成果物に対し、事前に必要 Observable が追加される。
- regression memory は docs だけでなく run manifest または専用 JSON として機械利用できる。

#### S07: 巨大モジュール分割

- `artifacts.py` と `agent_runner.py` の責務を上記モジュールへ移す。
- 分割中も CLI 互換性を維持する。

完了条件:

- `artifacts.py` は生成物プロトコル中心の責務へ縮小する。
- `agent_runner.py` は orchestration 中心の責務へ縮小する。
- 全体テストが通る。

#### S08: Benchmark regression

- Tetris
- Mini SQLite
- Redis/KVS
- 追加の未知小課題

を同一 harness policy で回し、過学習していないことを確認する。

完了条件:

- Tetris false positive は再発しない。
- Mini SQLite は stage 単位で進捗・失敗理由が観測できる。
- Redis/KVS は既存成功条件を壊さない。
- 未知小課題で、domain-specific rule が不適切に発火しない。

### 受け入れ条件

- [ ] `Requirement`, `Observable`, `Evidence`, `Verdict`, `RepairAction`, `RegressionMemory` が型として定義されている
- [ ] 既存 `acceptance_matrix` は新 domain model から生成され、既存 `run.json` 互換を維持する
- [ ] `fail`, `unverified`, `blocked`, `invalid_evidence` が区別され、`pass` 以外は approval blocker になる
- [x] HTML/browser smoke は harness plugin として実装され、core runner に Tetris 固有判定が直書きされない
- [ ] Python/API/CLI/storage probe は harness plugin として実装される
- [ ] repair advice は generic rules と domain rules に分離される
- [ ] acceptance blocker から Repair Action が生成され、次 Coder call に文書として渡る
- [ ] stage planner は stage ごとに required observables と writable/readonly paths を保存する
- [ ] failure history は機械利用可能な regression memory として保存される
- [ ] Tetris active piece false positive は regression test として残る
- [ ] Mini SQLite stage resume regression が残る
- [ ] Redis/KVS benchmark regression が残る
- [ ] `python3 -m unittest discover -s tests` が全段階で成功する
- [ ] `artifacts.py` と `agent_runner.py` は責務分割され、各ファイルの責務が SPEC.md のモジュール構成と一致する

### テスト計画

#### Unit tests

- Requirement parsing
- Observable generation
- Evidence cover matching
- Verdict calculation
- Repair Action generation
- Regression Memory serialization
- Harness plugin dispatch
- Domain rule selection

#### Integration tests

- Tetris:
  - DOM/APIだけ存在し、active piece が表示されない HTML は fail
  - Start後に active piece が表示され、ArrowLeftで可視位置が変わる HTML は pass
- Mini SQLite:
  - generated tests が readonly evidence として扱われる
  - stage resume が S03 以降から成立する
  - API probe の事実に反する repair が拒否される
- Redis/KVS:
  - partial stage では Redis smoke を抑止できる
  - final integration では Redis smoke が required observable になる

#### Regression tests

- 過去の false positive run を fixture 化し、同じ判定ミスが再発しないことを確認する。
- 過去の生成物 format 崩れを fixture 化し、recoverable / reject の境界を確認する。
- 同じ失敗ファミリーの AssertionError 文言揺れを同一 family として扱えることを確認する。

### 完了検証ループ

この仕様の実装時は、各フェーズで以下を繰り返す。

```text
1. SPEC.md の対象フェーズを確認する
2. 小さい実装単位に分ける
3. 対応する unit test / regression test を先に追加または更新する
4. 実装する
5. `python3 -m unittest discover -s tests` を実行する
6. run manifest / docs / SPEC.md に結果を記録する
7. acceptance blocker が残る場合は次フェーズへ進まない
```

### 優先順位

1. S01 Requirement / Evidence / Verdict model 分離
2. S02 Harness plugin interface
3. S04 Repair Action model
4. S03 Python / CLI / Storage harness 分離
5. S05 Stage Planner 強化
6. S06 Regression Memory
7. S07 巨大モジュール分割
8. S08 Benchmark regression

S07 の巨大分割は重要だが、S01-S04 の domain boundary が定義される前に実施すると単なるファイル移動になるため、先に制御モデルを固める。

### リスクと対策

| リスク | 対策 |
|---|---|
| domain model が抽象化されすぎ、実装が進まない | 既存 acceptance matrix と browser smoke から段階的に置換する |
| plugin 化で既存 CLI 互換が壊れる | public function は wrapper として残し、内部委譲にする |
| 過去課題への過学習が残る | regression memory に `scope` を持たせ、発火条件を明示する |
| LLM 判断に依存しすぎる | LLM は Project Policy Triage の分類まで。適用・承認は runner が行う |
| テストが巨大化する | fixture を分割し、unit / integration / benchmark を分ける |

### この節の仕様完結判定

この追記は、以下を満たすため仕様として完結している。

- 目的が明確である
- スコープと非スコープが明確である
- 固定要件が明確である
- domain model が定義されている
- モジュール構成が定義されている
- 実行フローが定義されている
- 実装フェーズが順序付きで定義されている
- 受け入れ条件がチェック可能である
- テスト計画がある
- 完了検証ループが定義されている
- リスクと対策がある

---

## 2026-07-25 追記: 源流資産を含む製品非依存化

### 目的

ローカル SDLC agent のプログラム、源流の憲法、SKILL.md、agent prompt、hook、
インストーラー、CLI 表示、利用者向け文書、説明 HTML から、特定のホスト型
コーディングエージェント製品・企業・専用ランタイムを前提とする表現を除去する。

実行時の文字列置換で隠すのではなく、同梱する正本を最初から製品非依存にする。
これにより、ファイルを直接読んだ場合と system prompt として実行した場合の説明を一致させる。

### 固定要件

- SDLC の役割、SPEC.md 優先、安全ゲート、生成物契約は変更しない。
- 源流の憲法、SKILL.md、agent prompt を直接中立化し、意味・責務・安全性は維持する。
- system prompt 組み立て時の製品名置換ロジックには依存しない。
- 製品専用の設定ファイル名、隠しディレクトリ、CLI コマンド、memory / tool semantics は、
  「エージェント向け指示ファイル」「エージェント設定ディレクトリ」
  「実行環境が提供する機能」など、実装に即した一般表現へ置き換える。
- 一般技術用語としての「code／コード」、OpenAI 互換 API、モデル名は、
  製品ブランドを意味しない範囲で維持する。
- ライセンス上必須の著作権・出典表示が存在する場合は削除しない。
- CLI のコマンド名、引数、終了コード、OpenAI 互換 API の呼び出し構造は変更しない。

### 受け入れ条件

- [x] 基礎 system prompt が特定製品・企業・専用ランタイムへの依存を指示しない。
- [x] 源流の憲法、全 SKILL.md、agent prompt、hook、インストーラーに製品固有名・専用パスが残らない。
- [x] system prompt 組み立てに製品名置換ロジックが存在せず、源流本文がそのまま組み立てられる。
- [x] 実際に同梱される全 SKILL.md と Supervisor asset から組み立てた system prompt に製品固有名が残らない。
- [x] フェーズ指示、CLI help、README、調査文書、説明 HTML が製品非依存の表現になる。
- [x] リポジトリ内の対象テキスト資産を走査する回帰テストが、製品固有名の再混入を検知する。
- [x] Python 3.13 の実体パス一時ディレクトリで全テストが成功する。
- [x] `python3.13 local_sdlc.py doctor --skip-llm` が成功する。

### 検証結果

- リポジトリ横断の対象テキスト走査: 製品・企業固有名の残存 0 件。
- 同梱する 12 SKILL.md と Supervisor asset から組み立てた runtime system prompt:
  13 件すべて製品固有名の残存 0 件。
- 製品名置換関数と置換テーブルを削除し、source description / body の直接組み立てを確認。
- Python 3.13 / `TMPDIR=/private/tmp`: `Ran 327 tests ... OK (skipped=1)`。
- `python3.13 local_sdlc.py doctor --skip-llm`: 成功。
- 変更した shell script 5 件: `bash -n` 成功。両 installer の一時ディレクトリ導入、
  3 hook の実行権限、write guard の block / allow 分岐を確認。hook 設定 JSON: parse 成功。
- 説明 HTML: HTML parse と inline JavaScript 構文検査に成功。
