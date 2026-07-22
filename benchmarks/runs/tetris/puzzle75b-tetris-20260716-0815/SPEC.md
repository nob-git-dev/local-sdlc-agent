# Tetris SPEC

## 固定要件
- 単一ファイル `tetris.html` として実装する
- 外部ライブラリ、CDN、npm、ビルド工程を使わない
- Vanilla HTML/CSS/JavaScript のみを使う
- DOMセル方式で描画する
- `#game-board` 内に `.cell` 要素を 200 個作る
- 盤面は 10列 x 20行
- 以下の関数を `window` に公開する
  - `startGame`
  - `gameLoop`
  - `movePiece`
  - `rotate`
  - `softDrop`
  - `hardDrop`
  - `clearLines`
  - `gameOver`

## 必須HTML要素
- `#start-btn`
- `#game-board`
- `#score`
- `#level`
- `#lines`
- `.overlay-title`

## 操作
- 左矢印: 左移動
- 右矢印: 右移動
- 上矢印: 回転
- 下矢印: ソフトドロップ
- Space: ハードドロップ
- P: ポーズ/再開

## 受け入れ条件
- ブラウザで `tetris.html` を開くと画面が表示される
- `#start-btn` を押すとゲームが開始する
- `#game-board` には `.cell` が 200 個存在する
- キーボード操作で現在のピースが移動・回転・落下する
- 行が埋まると `clearLines()` により消える
- スコア、レベル、消去行数が更新される
- `gameOver()` を呼ぶと `.overlay-title` が `GAME OVER` になる
- ゲームオーバー後に Start ボタンで再開できる

## 検証方法
- HTML smoke が通ること
- browser-tetris-smoke が通ること
- `window.startGame` などの必須関数が `typeof ... === "function"` になること
- `document.querySelectorAll("#game-board .cell").length === 200` になること
