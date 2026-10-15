"""BOX_COMMAND is parsed and never handed to a shell."""

import pytest

from stdio_box.boxcommand import BoxCommandError, parse_box_command


def test_uvx_and_npx_commands_parse() -> None:
    uvx = parse_box_command("uvx mcp-atlassian==0.23.1")
    assert uvx.runner == "uvx"
    assert uvx.package == "mcp-atlassian"
    assert uvx.version == "0.23.1"
    assert uvx.args == ()

    notion = parse_box_command("npx -y @notionhq/notion-mcp-server@2.5.2")
    assert notion.runner == "npx"
    assert notion.package == "@notionhq/notion-mcp-server"
    assert notion.version == "2.5.2"
    assert notion.args == ()

    tavily = parse_box_command("npx tavily-mcp@0.2.22 --flag x")
    assert tavily.runner == "npx"
    assert tavily.package == "tavily-mcp"
    assert tavily.version == "0.2.22"
    assert tavily.args == ("--flag", "x")


@pytest.mark.parametrize(
    "text",
    [
        "uvx mcp-atlassian",
        "uvx mcp-atlassian>=0.23",
        "npx pkg@latest",
        "npx pkg@^1.2.0",
        "uvx --from x y",
        "uvx mcp-atlassian==0.23.1; rm -rf /",
        "uvx $(whoami)",
    ],
)
def test_ranges_flags_and_shell_metacharacters_are_rejected(text: str) -> None:
    with pytest.raises(BoxCommandError) as exc:
        parse_box_command(text)
    assert exc.value.code == "box_command_invalid"
