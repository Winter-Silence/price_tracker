"""Выбор Python для venv при деплое: asdf, mise или системный python3."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import fabfile


def _conn(checks: list[bool]) -> mock.MagicMock:
    conn = mock.MagicMock()
    conn.run.side_effect = [SimpleNamespace(ok=ok) for ok in checks]
    return conn


class PythonToolchainTest(unittest.TestCase):
    """На сервере может не быть asdf — деплой не должен из-за этого падать."""

    def test_asdf_when_installed(self):
        python_cmd, install_cmd = fabfile._python_toolchain(_conn([True]))

        assert python_cmd == '. "$HOME/.asdf/asdf.sh" && asdf exec python'
        assert install_cmd == '. "$HOME/.asdf/asdf.sh" && asdf install'

    def test_mise_when_asdf_is_missing(self):
        python_cmd, install_cmd = fabfile._python_toolchain(_conn([False, True]))

        assert python_cmd == '"$HOME/.local/bin/mise" exec -- python'
        assert install_cmd == '"$HOME/.local/bin/mise" install'

    def test_system_python3_when_no_version_manager(self):
        python_cmd, install_cmd = fabfile._python_toolchain(_conn([False, False]))

        assert python_cmd == "python3"
        assert install_cmd is None


if __name__ == "__main__":
    unittest.main()
