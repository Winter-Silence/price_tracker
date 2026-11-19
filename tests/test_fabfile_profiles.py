"""Пути профилей Avito при деплое считаются от WorkingDirectory сервиса."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import fabfile


def _conn(stdout_by_cmd: dict[str, str]) -> mock.MagicMock:
    conn = mock.MagicMock()

    def fake_run(cmd: str, **kwargs):
        stdout = ""
        for needle, value in stdout_by_cmd.items():
            if needle in cmd:
                stdout = value
                break
        return SimpleNamespace(stdout=stdout)

    conn.run.side_effect = fake_run
    return conn


class RemoteProfilePathTest(unittest.TestCase):
    """Каталог профиля на сервере должен совпадать с тем, что видит бот."""

    def test_relative_path_resolves_against_service_working_directory(self):
        conn = _conn({
            "WorkingDirectory": "/home/yury/projects/price_tracker\n",
        })
        with mock.patch.object(fabfile, "_load_env", return_value={}):
            path = fabfile._remote_profile_dir(conn, "price")

        assert path == "/home/yury/projects/price_tracker/chrome_profile_avito"

    def test_server_env_value_is_used_when_deploy_env_has_none(self):
        conn = _conn({
            "WorkingDirectory": "/home/yury/projects/price_tracker\n",
            "AVITO_BOOKS_PROFILE_DIR": "./data/books_profile\n",
        })
        with mock.patch.object(fabfile, "_load_env", return_value={}):
            path = fabfile._remote_profile_dir(conn, "books")

        assert path == "/home/yury/projects/price_tracker/data/books_profile"

    def test_deploy_env_value_wins_and_stays_absolute(self):
        conn = _conn({"WorkingDirectory": "/home/yury/projects/price_tracker\n"})
        with mock.patch.object(
                fabfile, "_load_env",
                return_value={"AVITO_PROFILE_DIR": "/data/price"}):
            path = fabfile._remote_profile_dir(conn, "price")

        assert path == "/data/price"


if __name__ == "__main__":
    unittest.main()
