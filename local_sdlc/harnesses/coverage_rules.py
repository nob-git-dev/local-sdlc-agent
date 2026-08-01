"""Domain coverage labels contributed by built-in harnesses."""

from __future__ import annotations


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def html_browser_required_covers(text: str) -> list[str]:
    lowered = text.lower()
    covers: list[str] = []
    checks = (
        ("html smoke", "static_html"),
        ("browser-tetris-smoke", "browser_smoke"),
        ("browser smoke", "browser_smoke"),
        ("opening", "html_visible"),
        ("open", "html_visible"),
        ("画面", "html_visible"),
        ("表示", "html_visible"),
        ("shows", "html_visible"),
        ("screen", "html_visible"),
        ("window.", "required_window_functions"),
        ("typeof", "required_window_functions"),
        ("function", "required_window_functions"),
        ("#game-board", "board_200_cells"),
        (".cell", "board_200_cells"),
        ("200", "board_200_cells"),
        ("start button", "start_button"),
        ("#start-btn", "start_button"),
        ("開始", "start_button"),
        ("キーボード", "keyboard_interaction"),
        ("keyboard", "keyboard_interaction"),
        ("arrow", "keyboard_interaction"),
        ("move", "keyboard_interaction"),
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
        ("line", "line_clear"),
        ("行", "line_clear"),
        ("gameover", "game_over"),
        ("game over", "game_over"),
        ("ゲームオーバー", "game_over"),
        ("restart", "restart_after_game_over"),
        ("再開", "restart_after_game_over"),
        ("再起動", "restart_after_game_over"),
    )
    for token, cover in checks:
        if token in lowered:
            covers.append(cover)
    return _unique(covers)


def required_covers_for_text(text: str) -> list[str]:
    """Dispatch requirement text to independent harness coverage providers."""
    covers: list[str] = []
    covers.extend(html_browser_required_covers(text))
    return _unique(covers)
