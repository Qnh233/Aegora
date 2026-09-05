from __future__ import annotations

import os
import tempfile
import unittest
import logging
from pathlib import Path
from unittest.mock import patch

from aegora_runtime.config import load_settings
from aegora_runtime.logging import get_logger, log_event, setup_logging


class LoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("aegora_runtime")
        self.original_handlers = list(self.logger.handlers)
        self.logger.handlers.clear()

    def tearDown(self) -> None:
        for handler in self.logger.handlers:
            handler.close()
        self.logger.handlers[:] = self.original_handlers

    def test_setup_logging_returns_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = setup_logging(load_settings(env_path=None), log_dir=tmp)
            # Windows cannot remove an open FileHandler target. Close the
            # handler while the temporary directory still exists so the test
            # exercises the same explicit lifecycle required by a real
            # process shutdown/reconfiguration.
            for logger in (self.logger, logging.getLogger("pocoflow")):
                for handler in list(logger.handlers):
                    if isinstance(handler, logging.FileHandler):
                        logger.removeHandler(handler)
                        handler.close()

        self.assertIsInstance(path, Path)
        self.assertIn("aegora_runtime", path.name)

    def test_setup_logging_can_disable_local_file(self) -> None:
        with patch.dict(os.environ, {"FILE_LOG_ENABLED": "false"}, clear=True):
            settings = load_settings(env_path=None)

        self.assertIsNone(setup_logging(settings))

    def test_setup_logging_keeps_console_quiet_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {"FILE_LOG_ENABLED": "false", "APP_CONSOLE_LOG_ENABLED": "false"},
            clear=True,
        ):
            settings = load_settings(env_path=None)
            setup_logging(settings)

        console_handlers = [
            handler
            for handler in self.logger.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, (logging.FileHandler, logging.NullHandler))
        ]
        self.assertEqual(console_handlers, [])

    def test_setup_logging_can_enable_console(self) -> None:
        with patch.dict(os.environ, {"FILE_LOG_ENABLED": "false"}, clear=True):
            settings = load_settings(env_path=None)

        setup_logging(settings, console=True)

        self.assertTrue(
            any(
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, (logging.FileHandler, logging.NullHandler))
                for handler in self.logger.handlers
            )
        )

    def test_setup_logging_can_enable_console_by_env(self) -> None:
        with patch.dict(
            os.environ,
            {"FILE_LOG_ENABLED": "false", "APP_CONSOLE_LOG_ENABLED": "true"},
            clear=True,
        ):
            settings = load_settings(env_path=None)
            setup_logging(settings)

        self.assertTrue(
            any(
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, (logging.FileHandler, logging.NullHandler))
                for handler in self.logger.handlers
            )
        )

    def test_log_event_includes_instance_id(self) -> None:
        with patch.dict(os.environ, {"INSTANCE_ID": "pod-2"}, clear=True):
            with self.assertLogs(get_logger("test"), level="INFO") as captured:
                log_event(get_logger("test"), logging.INFO, "checked")

        self.assertIn('"instance_id": "pod-2"', captured.output[0])


if __name__ == "__main__":
    unittest.main()
