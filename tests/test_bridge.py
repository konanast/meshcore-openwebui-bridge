import unittest
from unittest.mock import patch

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

    def test_channel_prompt_requires_and_removes_mention(self):
        self.assertEqual(bridge.channel_prompt("@AI /ping"), "/ping")
        self.assertEqual(bridge.channel_prompt("hello @ai there"), "hello  there")
        self.assertIsNone(bridge.channel_prompt("hello channel"))
        self.assertIsNone(bridge.channel_prompt("hello @aiden"))

    def test_channel_mention_requirement_can_be_disabled(self):
        with patch.object(bridge, "CHANNEL_REQUIRE_MENTION", False):
            self.assertEqual(bridge.channel_prompt("hello channel"), "hello channel")

    def test_public_channel_is_disabled_by_default(self):
        self.assertFalse(bridge.channel_index_allowed(0))
        self.assertTrue(bridge.channel_index_allowed(1))

    def test_public_channel_can_be_enabled_and_allowlisted(self):
        with (
            patch.object(bridge, "PUBLIC_CHANNEL", True),
            patch.object(bridge, "ALLOWED_CHANNELS", {0}),
        ):
            self.assertTrue(bridge.channel_index_allowed(0))
            self.assertFalse(bridge.channel_index_allowed(1))


if __name__ == "__main__":
    unittest.main()
