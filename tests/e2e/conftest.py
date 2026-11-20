"""E2E fixtures deliberately do not import application modules."""

import subprocess

import pytest

from .harness import Controller, SUPPORT


@pytest.fixture(scope="session")
def e2e_guard(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e-guard")
    output = root / "guard.so"
    subprocess.run(["gcc", "-shared", "-fPIC", "-Wall", "-Werror", "-O2", "-o", str(output),
                    str(SUPPORT / "guard.c"), "-ldl"], check=True, capture_output=True)
    return output


@pytest.fixture
def controller_factory(tmp_path, e2e_guard):
    controllers: list[Controller] = []

    def make(*, start=True):
        instance = Controller(tmp_path / f"controller-{len(controllers)}", e2e_guard)
        controllers.append(instance)
        if start:
            instance.start()
        return instance

    yield make
    errors = []
    for instance in reversed(controllers):
        try:
            instance.close()
        except Exception as exc:
            errors.append(str(exc))
    assert not errors, "\n".join(errors)


@pytest.fixture
def controller(controller_factory):
    return controller_factory()
