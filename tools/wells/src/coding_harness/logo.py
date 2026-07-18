"""Wells logo: a small, crisp glyph lockup for the TUI welcome.

Hand-drawn with box/block glyphs (no image sampling, so no blur): a tiny
tripod robot — dome, teal eye, three legs — next to the wordmark. Colors
match the logo PNG (grey shell, teal eye).
"""

_SHELL = "rgb(122,132,141)"
_LEGS = "rgb(88,99,108)"
_EYE = "rgb(38,150,160)"

LOGO_MARKUP_LINES = [
    f"  [{_SHELL}]▄▄▄▄▄[/]",
    f" [{_SHELL}]▐   [/][bold {_EYE}]●[/][{_SHELL}] ▌[/]  [bold blue]W E L L S[/]",
    f"  [{_SHELL}]▀▀▀▀▀[/]   [dim]agentic coding harness[/dim]",
    f"  [{_LEGS}]╱ ║ ╲[/]",
]


def logo_lines(max_width: int = 0) -> list[str]:
    """Return the logo's Rich markup lines; empty when the terminal is too narrow."""
    if max_width and max_width < 30:
        return []
    return list(LOGO_MARKUP_LINES)
