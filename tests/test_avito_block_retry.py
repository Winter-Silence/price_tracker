"""Тесты повторов после заглушки Avito «Доступ ограничен»."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from scheduler.jobs import (
    _avito_block_state,
    _get_avito_block_max_attempts,
    _get_avito_block_retry_minutes,
)


class BlockStateTest(unittest.TestCase):
    def test_notifies_once_on_threshold_and_resets_after_success(self):
        attempts = 0
        for expected in (1, 2):
            attempts, notify = _avito_block_state(True, attempts, 3)
            assert (attempts, notify) == (expected, False)

        attempts, notify = _avito_block_state(True, attempts, 3)
        assert (attempts, notify) == (3, True)

        # Продолжающиеся блокировки не спамят уведомлениями.
        attempts, notify = _avito_block_state(True, attempts, 3)
        assert (attempts, notify) == (4, False)

        attempts, notify = _avito_block_state(False, attempts, 3)
        assert (attempts, notify) == (0, False)


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert _get_avito_block_retry_minutes() == 5
            assert _get_avito_block_max_attempts() == 3

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {
            "AVITO_BLOCK_RETRY_MINUTES": "7",
            "AVITO_BLOCK_MAX_ATTEMPTS": "4",
        }):
            assert _get_avito_block_retry_minutes() == 7
            assert _get_avito_block_max_attempts() == 4
