"""Pure tests for the Windows console backend."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import windows_console  # noqa: E402


class WindowsConsoleTests(unittest.TestCase):
    def test_decodes_navigation_and_action_keys(self):
        expected = {
            "q": windows_console.KEY_QUIT,
            "\t": windows_console.KEY_TAB,
            "r": windows_console.KEY_REFRESH,
            "a": windows_console.KEY_ACTIVE,
            " ": windows_console.KEY_PLAY,
            "p": windows_console.KEY_PLAY,
            "j": windows_console.ARROW_DOWN,
            "k": windows_console.ARROW_UP,
            "[": windows_console.STEP_BACK,
            "]": windows_console.STEP_FORWARD,
            "5": "5",
            "\xe0H": windows_console.ARROW_UP,
            "\xe0P": windows_console.ARROW_DOWN,
            "\xe0K": windows_console.ARROW_LEFT,
            "\xe0M": windows_console.ARROW_RIGHT,
            "\xe0I": windows_console.PAGE_UP,
            "\xe0Q": windows_console.PAGE_DOWN,
            "\xe0G": windows_console.HOME,
            "\xe0O": windows_console.END,
        }
        for sequence, key in expected.items():
            with self.subTest(sequence=sequence):
                self.assertEqual(windows_console.decode_key(sequence), key)

    def test_frame_clips_width_and_height(self):
        frame = windows_console.render_frame(
            ["title", "tabs", "help", "status"],
            ["x" * 100, "hidden"],
            columns=12,
            rows=5,
            clear=True,
        )

        self.assertTrue(frame.startswith("\x1b[2J\x1b[H"))
        visible = frame.removeprefix("\x1b[2J\x1b[H").split("\r\n")
        self.assertEqual(len(visible), 5)
        self.assertTrue(all(len(line) <= 12 for line in visible))
        self.assertNotIn("hidden", frame)

    def test_non_vt_frame_uses_readable_separator(self):
        frame = windows_console.render_frame(
            ["title"], ["body"], columns=20, rows=5, clear=False
        )

        self.assertNotIn("\x1b[2J", frame)
        self.assertIn("title\r\nbody", frame)
        self.assertTrue(frame.endswith("-" * 20))


if __name__ == "__main__":
    unittest.main()
