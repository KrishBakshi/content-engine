"""The committed systemd/cron files are exactly what deploy/render.py makes with its defaults."""

import importlib.util
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_spec = importlib.util.spec_from_file_location("render", DEPLOY / "render.py")
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)

DEFAULT_VALUES = render.values(**{k: v for k, v in render.DEFAULTS.items()})
COMMITTED = {
    "content-engine.service": DEPLOY / "systemd" / "content-engine.service",
    "content-engine.timer": DEPLOY / "systemd" / "content-engine.timer",
    "content-engine.cron": DEPLOY / "cron" / "content-engine.cron",
}


def test_every_template_has_a_committed_rendering():
    assert set(render.render_all(DEFAULT_VALUES)) == set(COMMITTED)


@pytest.mark.parametrize("name", sorted(COMMITTED))
def test_committed_file_matches_template(name):
    # On failure: python3 deploy/render.py --out /tmp/x, then copy the file over the committed one.
    assert COMMITTED[name].read_text() == render.render_all(DEFAULT_VALUES)[name]


def test_no_placeholder_left_and_unknown_ones_fail():
    for text in render.render_all(DEFAULT_VALUES).values():
        assert "@@" not in text
    with pytest.raises(KeyError):
        render.render("@@NOPE@@", DEFAULT_VALUES)


def test_data_dir_outside_var_lib_drops_state_directory():
    vals = render.values("/srv/ce", "/srv/ce-data", "ce", "4", "hourly", "0 * * * *")
    service = render.render_all(vals)["content-engine.service"]
    assert "StateDirectory=" not in service
    assert "ReadWritePaths=/srv/ce-data" in service
    assert "ExecStart=/srv/ce/.venv/bin/content-engine run" in service
