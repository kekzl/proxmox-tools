import os
import unittest

from helpers import ROOT, TOOLS


class SharedBlockTests(unittest.TestCase):
    def test_tools_embed_identical_shared_block(self):
        with open(os.path.join(ROOT, "lib", "shared.py")) as f:
            want = f.read()
        for tool in TOOLS:
            with open(os.path.join(ROOT, tool)) as f:
                text = f.read()
            start = text.index("# ==== shared:begin\n") + len("# ==== shared:begin\n")
            end = text.index("# ==== shared:end")
            self.assertEqual(text[start:end], want, f"{tool}: run scripts/sync-shared.sh")


if __name__ == "__main__":
    unittest.main()
