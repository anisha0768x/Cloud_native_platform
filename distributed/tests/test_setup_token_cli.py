import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from distributed.scripts import setup


class TokenCommandTests(unittest.TestCase):
    def test_token_only_prints_no_labels_and_changes_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".env"
            config.write_text("SETUP_TOKEN=test-only-token\nOTHER_SECRET=not-for-output\n")
            with (
                patch("sys.stdout", io.StringIO()) as output,
                patch.object(setup, "configure") as configure,
                patch.object(setup, "compose") as compose,
            ):
                setup.main(["--env-file", str(config), "--token-only"])
            self.assertEqual(output.getvalue(), "test-only-token\n")
            configure.assert_not_called()
            compose.assert_not_called()

    def test_show_token_does_not_create_configuration_in_wrong_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".env"
            with self.assertRaisesRegex(SystemExit, "correct project folder"):
                setup.main(["--env-file", str(config), "--show-setup-token"])
            self.assertFalse(config.exists())


if __name__ == "__main__":
    unittest.main()
