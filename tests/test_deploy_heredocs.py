"""Regression checks for shell expansion in the full deploy payload."""

from __future__ import annotations

import re
from pathlib import Path


DEPLOY_SCRIPT = Path(__file__).parents[1] / "deploy" / "deploy.sh"
NGINX_HEREDOCS = ("TVCONF", "TACONF", "TAPCONF")


def test_nested_nginx_heredocs_escape_variables_from_outer_shell() -> None:
    """Quoted inner heredocs are still expanded by deploy.sh's outer heredoc."""
    source = DEPLOY_SCRIPT.read_text()

    for marker in NGINX_HEREDOCS:
        match = re.search(
            rf"<<'{marker}'\n(?P<body>.*?)\n{marker}$",
            source,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert match, f"missing {marker} heredoc"

        unescaped = re.findall(r"(?<!\\)\$[A-Za-z_][A-Za-z0-9_]*", match["body"])
        assert not unescaped, (
            f"{marker} contains variables that the local shell would expand: "
            f"{unescaped}"
        )
