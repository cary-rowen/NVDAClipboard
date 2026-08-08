"""Tests for the ClipDataCloud boundary and completion handling."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import sys
from threading import Lock
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
_PACKAGE_NAME = "nvdaClipboardCloudSyncTests"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_MODULE_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _PACKAGE


def _translate(message: str) -> str:
	"""Return an untranslated message for standalone validation."""
	return message


def _initTranslation() -> None:
	"""Install the standalone translator in the importing module globals."""
	sys._getframe(1).f_globals["_"] = _translate


_ADDON_HANDLER = ModuleType("addonHandler")
_ADDON_HANDLER.initTranslation = _initTranslation
_CONFIG = ModuleType("config")
_CONFIG.conf = SimpleNamespace(spec={})
_CONFIG.post_configProfileSwitch = SimpleNamespace(register=Mock(), unregister=Mock())
_LOG_HANDLER = ModuleType("logHandler")
_LOG_HANDLER.log = Mock()
_UI = ModuleType("ui")
_UI.message = Mock()
_WX = ModuleType("wx")
_WX.CallAfter = Mock()
_SPEC = importlib.util.spec_from_file_location(
	f"{_PACKAGE_NAME}.cloudSync",
	_MODULE_DIRECTORY / "cloudSync.py",
)
assert _SPEC is not None and _SPEC.loader is not None
cloudSync = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cloudSync
with patch.dict(
	sys.modules,
	{
		"addonHandler": _ADDON_HANDLER,
		"config": _CONFIG,
		"logHandler": _LOG_HANDLER,
		"ui": _UI,
		"wx": _WX,
	},
):
	_SPEC.loader.exec_module(cloudSync)
cloudClipboard = importlib.import_module(f"{_PACKAGE_NAME}.cloudClipboard")


class CloudSyncTests(unittest.TestCase):
	"""Verify cloud validation and completion cleanup without NVDA."""

	def testStateObserverFailureDoesNotBlockCompletion(self) -> None:
		"""Continue to onDone when the state observer targets a destroyed window."""
		manager = object.__new__(cloudSync.CloudSyncManager)
		manager._terminated = False
		manager._onStateChanged = Mock(side_effect=RuntimeError("destroyed"))
		completionLock = Lock()
		completionLock.acquire()
		onDone = Mock(side_effect=lambda *_args: completionLock.release())

		with patch.object(cloudSync.log, "exception") as logException:
			manager._finish(True, "done", onDone, announce=False)

		onDone.assert_called_once_with(True, "done")
		self.assertFalse(completionLock.locked())
		logException.assert_called_once_with("CloudSyncManager state callback failed.")

	def testDispatchFailureReleasesStateAndCleanup(self) -> None:
		"""Release operation guards when wx rejects a worker result."""
		manager = object.__new__(cloudSync.CloudSyncManager)
		manager._terminated = False
		manager._stateOperationId = 7
		manager._stateOperationInProgress = True
		manager._sdkOperationLock = Lock()
		cleanup = Mock()

		with (
			patch.object(cloudSync.wx, "CallAfter", side_effect=RuntimeError("wx stopped")),
			patch.object(cloudSync, "Thread") as threadClass,
		):
			threadClass.return_value.start.side_effect = lambda: threadClass.call_args.kwargs["target"]()
			manager._runSdkOperation(
				lambda: object(),
				lambda _result: "done",
				"failed: {error}",
				onDone=None,
				stateOperationId=7,
				onDispatchFailed=cleanup,
			)

		self.assertFalse(manager._stateOperationInProgress)
		cleanup.assert_called_once_with()

	def testSuccessfulResultCallbackFailureStillCompletes(self) -> None:
		"""Clear busy state and fail completion when successful-result application raises."""
		manager = object.__new__(cloudSync.CloudSyncManager)
		manager._terminated = False
		manager._stateOperationId = 3
		manager._stateOperationInProgress = True
		manager._statusMessage = "busy"
		manager._onStateChanged = None
		onDone = Mock()

		manager._finishSuccessfulOperation(
			object(),
			Mock(side_effect=RuntimeError("invalid result")),
			onDone,
			announce=True,
			stateAtStart=3,
			stateOperationId=3,
		)

		self.assertFalse(manager._stateOperationInProgress)
		onDone.assert_called_once_with(False, "")

	def testValidateUploadTextBoundaries(self) -> None:
		"""Enforce the native SDK's text encoding and one-megabyte boundary."""
		invalidValues = (
			("", cloudSync.TEXT_EMPTY_ERROR),
			("a\0b", cloudSync.TEXT_CONTAINS_NUL_ERROR),
			("\ud800", cloudSync.TEXT_INVALID_UTF8_ERROR),
		)
		for text, expectedMessage in invalidValues:
			with self.subTest(text=repr(text)):
				with self.assertRaises(cloudSync.CloudClipboardError) as context:
					cloudSync.validateUploadText(text)
				self.assertEqual(context.exception.code, cloudSync.INVALID_ARGUMENT)
				self.assertEqual(context.exception.message, expectedMessage)

		withinLimit = "a" * cloudClipboard.MAX_TEXT_BYTES
		self.assertEqual(cloudSync.validateUploadText(withinLimit), withinLimit.encode("utf-8"))
		with self.assertRaises(cloudSync.CloudClipboardError) as context:
			cloudSync.validateUploadText(withinLimit + "a")
		self.assertEqual(context.exception.code, cloudSync.INVALID_ARGUMENT)
		self.assertEqual(context.exception.message, cloudSync.TEXT_EXCEEDS_MAX_BYTES_ERROR)


if __name__ == "__main__":
	unittest.main()
