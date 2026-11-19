"""Выбор Python для venv при деплое: asdf на сервере опционален."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import fabfile


class AsdfPrefixTest(unittest.TestCase):
    """Без asdf деплой должен использовать системный python3."""

    def test_returns_prefix_when_asdf_is_installed(self):
        conn = mock.MagicMock()
        conn.run.return_value = SimpleNamespace(ok=True)

        assert fabfile._asdf_prefix(conn) == '. "$HOME/.asdf/asdf.sh"'

    def test_returns_none_when_asdf_is_missing(self):
        conn = mock.MagicMock()
        conn.run.return_value = SimpleNamespace(ok=False)

        assert fabfile._asdf_prefix(conn) is None


if __name__ == "__main__":
    unittest.main()
