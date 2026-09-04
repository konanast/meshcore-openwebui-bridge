import unittest

import bridge


class RoutingTests(unittest.TestCase):
    def test_temp_is_one_shot_route(self):
        self.assertEqual(
            bridge.route("/temp do not remember this"),
            ("__temp__", "do not remember this", False),
        )

    def test_new_and_reset_start_new_persistent_chat(self):
        self.assertEqual(bridge.route("/new")[0], "__new__")
        self.assertEqual(bridge.route("/reset")[0], "__new__")

    def test_normalized_reply_matches_radio_limit(self):
        self.assertEqual(bridge.normalized_reply("a   b", 20), "a b")
        self.assertEqual(bridge.normalized_reply("123456", 5), "1234…")


if __name__ == "__main__":
    unittest.main()
