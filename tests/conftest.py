from __future__ import annotations

import importlib.util
import pathlib
import site
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))


@pytest.fixture(scope="session")
def upstream_maxminddb():
    candidates = [
        pathlib.Path(directory) / "maxminddb" / "__init__.py"
        for directory in site.getsitepackages()
    ]
    path = next(
        (
            candidate
            for candidate in candidates
            if candidate.exists()
        ),
        None,
    )
    if path is None:
        pytest.skip("upstream maxminddb is not installed")
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "maxminddb" or name.startswith("maxminddb.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        spec = importlib.util.spec_from_file_location(
            "maxminddb",
            path,
            submodule_search_locations=[str(path.parent)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        for name in list(sys.modules):
            if name == "maxminddb" or name.startswith("maxminddb."):
                del sys.modules[name]
        sys.modules.update(saved)
    return module
