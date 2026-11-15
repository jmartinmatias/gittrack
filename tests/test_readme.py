"""The README's Commands table must describe the CLI that actually ships."""

import re
from pathlib import Path

from gittrack import cli, report

README = (Path(__file__).parent.parent / "README.md").read_text()
TABLE = README.split("## Commands", 1)[1].split("\n## ", 1)[0]
ROWS = re.findall(r"^\| `gittrack (\w+)[^`]*` \|(.*)$", TABLE, flags=re.M)


def _subparsers():
    action = next(a for a in cli.build_parser()._actions
                  if a.__class__.__name__ == "_SubParsersAction")
    return action.choices


def test_every_command_is_in_the_table_and_nothing_else():
    assert {name for name, _ in ROWS} == set(_subparsers())


def test_every_flag_the_table_names_exists_on_that_command():
    subs = _subparsers()
    for name, rest in ROWS:
        row = f"gittrack {name}" + rest
        known = {opt for a in subs[name]._actions for opt in a.option_strings}
        for flag in re.findall(r"(--[a-z][a-z-]*)", row):
            assert flag in known, f"README names {flag} for `{name}`, the CLI has no such flag"


def test_top_sort_keys_in_the_table_match_the_code():
    top = next(rest for name, rest in ROWS if name == "top")
    listed = re.search(r"--by ([\w\\|-]+)", top).group(1).split("\\|")
    assert sorted(listed) == sorted(report.SORT_KEYS)
