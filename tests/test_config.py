"""The two config templates must match each other and the code's defaults.

`gittrack init` writes config.DEFAULT_CONFIG_TOML; the repo ships
gittrack.example.toml, which the README calls the list of every option. The
example had drifted: [digest], [notify] and untrack_after_days were missing.
"""

import dataclasses
import tomllib
from pathlib import Path

from gittrack import config as cfgmod

EXAMPLE = Path(__file__).parent.parent / "gittrack.example.toml"


def test_example_file_is_the_init_template():
    assert EXAMPLE.read_text() == cfgmod.DEFAULT_CONFIG_TOML


def test_template_sets_every_option_to_its_default():
    raw = tomllib.loads(cfgmod.DEFAULT_CONFIG_TOML)
    defaults = cfgmod.Config()
    for f in dataclasses.fields(defaults):
        if f.name == "source_path":
            continue
        want = getattr(defaults, f.name)
        if not dataclasses.is_dataclass(want):
            assert raw[f.name] == want, f.name
            continue
        section = raw.get(f.name, {})
        for sub in dataclasses.fields(want):
            if f.name == "github" and sub.name == "api_base":
                continue        # not in the template; only needed for a non-default API host
            assert sub.name in section, f"[{f.name}] {sub.name} missing from template"
            assert section[sub.name] == getattr(want, sub.name), f"[{f.name}] {sub.name}"
        assert set(section) <= {s.name for s in dataclasses.fields(want)}, f.name
