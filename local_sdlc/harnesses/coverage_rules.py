"""Domain coverage labels contributed by built-in harnesses."""

from __future__ import annotations

import re


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _contains(text: str, token: str) -> bool:
    """Match ASCII words as tokens instead of accidental substrings."""
    if re.fullmatch(r"[a-z0-9 ]+", token):
        pattern = r"(?<![a-z0-9_])" + re.escape(token).replace(r"\ ", r"\s+") + r"(?![a-z0-9_])"
        return re.search(pattern, text) is not None
    return token in text


def html_browser_required_covers(text: str) -> list[str]:
    lowered = text.lower()
    covers: list[str] = []
    direct_checks = (
        ("html smoke", "static_html"),
        ("browser-tetris-smoke", "browser_smoke"),
        ("browser smoke", "browser_smoke"),
        ("画面", "html_visible"),
        ("表示", "html_visible"),
        ("screen", "html_visible"),
        ("window.", "required_window_functions"),
        ("typeof", "required_window_functions"),
        ("#game-board", "board_200_cells"),
        (".cell", "board_200_cells"),
        ("start button", "start_button"),
        ("#start-btn", "start_button"),
        ("開始", "start_button"),
        ("キーボード", "keyboard_interaction"),
        ("keyboard", "keyboard_interaction"),
        ("arrow", "keyboard_interaction"),
        ("移動", "keyboard_interaction"),
        ("rotate", "keyboard_interaction"),
        ("回転", "keyboard_interaction"),
        ("drop", "keyboard_interaction"),
        ("落下", "keyboard_interaction"),
        ("active piece", "active_piece_visible"),
        ("現在のピース", "active_piece_visible"),
        ("score", "score_update"),
        ("level", "score_update"),
        ("lines", "score_update"),
        ("スコア", "score_update"),
        ("レベル", "score_update"),
        ("消去行", "score_update"),
        ("clearlines", "line_clear"),
        ("行", "line_clear"),
        ("gameover", "game_over"),
        ("game over", "game_over"),
        ("ゲームオーバー", "game_over"),
        ("restart", "restart_after_game_over"),
        ("再開", "restart_after_game_over"),
        ("再起動", "restart_after_game_over"),
    )
    for token, cover in direct_checks:
        if _contains(lowered, token):
            covers.append(cover)
    html_context = any(
        _contains(lowered, token)
        for token in ("html", "browser", "tetris", "web page", "screen", "画面", "ブラウザ")
    ) or any(token in lowered for token in ("window.", "#game-board", "#start-btn", ".html"))
    if html_context and any(_contains(lowered, token) for token in ("opening", "open", "shows")):
        covers.append("html_visible")
    if _contains(lowered, "200") and any(token in lowered for token in ("game-board", ".cell", "board", "盤面")):
        covers.append("board_200_cells")
    if _contains(lowered, "move") and any(
        _contains(lowered, token) for token in ("keyboard", "arrow", "piece", "block")
    ):
        covers.append("keyboard_interaction")
    if _contains(lowered, "line") and any(
        _contains(lowered, token) for token in ("clear", "score", "tetris", "game")
    ):
        covers.append("line_clear")
    return _unique(covers)


def required_covers_for_text(text: str) -> list[str]:
    """Dispatch requirement text to independent harness coverage providers."""
    covers: list[str] = []
    covers.extend(html_browser_required_covers(text))
    return _unique(covers)
