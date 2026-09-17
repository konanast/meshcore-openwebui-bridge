import tempfile
import unittest
from pathlib import Path

from conversation import ConversationService, ConversationStore, OpenWebUIChatClient


class FakeClient:
    def __init__(self):
        self.chats = {}
        self.created = 0
        self.updates = []

    async def create_chat(self, title, model):
        self.created += 1
        chat_id = f"chat-{self.created}"
        self.chats[chat_id] = {"title": title, "model": model}
        return chat_id

    async def get_chat(self, chat_id):
        return self.chats.get(chat_id)

    async def update_chat(self, chat_id, title, model, messages):
        self.updates.append((chat_id, title, model, messages))


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ConversationStore(Path(self.tempdir.name) / "history.sqlite3")
        await self.store.initialize()
        self.client = FakeClient()
        self.service = ConversationService(self.store, self.client, 2, 100)

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_chat_is_reused_and_recreated_only_when_missing(self):
        first = await self.service.ensure_chat("node-a", "Node A", "fast")
        second = await self.service.ensure_chat("node-a", "Node A", "fast")
        self.assertEqual(first["chat_id"], second["chat_id"])
        self.assertEqual(self.client.created, 1)

        del self.client.chats[first["chat_id"]]
        replacement = await self.service.ensure_chat("node-a", "Node A", "fast")
        self.assertNotEqual(first["chat_id"], replacement["chat_id"])
        self.assertEqual(self.client.created, 2)

    async def test_context_is_bounded_and_omits_failed_assistant_messages(self):
        await self.service.ensure_chat("node-a", "Node A", "fast")
        await self.store.append_exchange("node-a", "one", "answer one", "fast", "acked")
        await self.store.append_exchange(
            "node-a", "two", "not received", "fast", "failed"
        )
        await self.store.append_exchange(
            "node-a", "three", "answer three", "fast", "acked"
        )

        context = await self.service.context("node-a")
        self.assertNotIn("not received", [item["content"] for item in context])
        self.assertNotIn("two", [item["content"] for item in context])
        self.assertLessEqual(len(context), 4)
        self.assertEqual(context[-1]["content"], "answer three")
        self.assertEqual(context[0]["role"], "user")

    async def test_new_chat_clears_local_history_and_keeps_old_remote_chat(self):
        first = await self.service.ensure_chat("node-a", "Node A", "fast")
        await self.store.append_exchange("node-a", "hello", "hi", "fast", "acked")
        second_id = await self.service.new_chat("node-a", "Node A", "fast")

        self.assertNotEqual(first["chat_id"], second_id)
        self.assertEqual(await self.store.messages("node-a"), [])
        self.assertIn(first["chat_id"], self.client.chats)

    async def test_record_updates_openwebui_transcript(self):
        await self.service.ensure_chat("node-a", "Node A", "fast")
        await self.service.record_and_sync(
            "node-a", "question", "answer", "fast", "acked"
        )
        self.assertEqual(len(self.client.updates), 1)
        self.assertEqual(
            [item["content"] for item in self.client.updates[0][3]],
            ["question", "answer"],
        )


class ChatDocumentTests(unittest.TestCase):
    def test_document_contains_openwebui_message_graph(self):
        document = OpenWebUIChatClient._chat_document(
            "Node A",
            "fast",
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi", "model": "fast"},
            ],
        )
        self.assertEqual(document["title"], "Node A")
        self.assertEqual(len(document["messages"]), 2)
        current_id = document["history"]["currentId"]
        self.assertEqual(document["history"]["messages"][current_id]["content"], "hi")


if __name__ == "__main__":
    unittest.main()
