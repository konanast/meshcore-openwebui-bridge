# MeshCore -> Open WebUI bridge

USB-connected MeshCore Companion gateway for Raspberry Pi.

## Commands

```text
plain text            -> FAST_MODEL
/fast <question>      -> FAST_MODEL
/ask <question>       -> ASK_MODEL
/deep <question>      -> DEEP_MODEL
/short <question>     -> FAST_MODEL, very short response
/temp <question>      -> one stateless question, not saved to chat history

/info or /help        -> command list
/models               -> configured model routes
/uptime               -> bridge uptime
/last                 -> last LLM call + last radio TX result
/ping                 -> bridge/MeshCore/Open WebUI health
/status               -> health + recent activity
/diag                 -> deeper MeshCore radio/packet stats + API status
/history              -> current node's persistent chat status
/new or /reset        -> start a new persistent chat for this node
```

`/info`, `/models`, `/uptime`, `/last`, `/ping`, `/status`, and `/diag`
are handled locally and do not invoke an LLM.

## Persistent per-node conversations

Direct-message history uses a hybrid architecture:

* SQLite is the bridge's authoritative operational history and retains the
  node-to-chat mapping across restarts.
* Each full MeshCore public key is assigned its own Open WebUI chat, owned by the
  account associated with `OPENWEBUI_API_KEY`.
* The chat is created on the node's first request and reused after radio, bridge,
  or container restarts.
* The transcript is mirrored into Open WebUI so it can be viewed and managed in
  the normal interface.
* Chats are placed in the `meshcore` Open WebUI folder by default. Folder setup is
  best-effort: a folder API error is logged but does not prevent a reply.

This integration targets Open WebUI **v0.11.3**. Pin that version and validate the
chat API before upgrading Open WebUI, because its `/api/v1/chats` document format
is Open WebUI-specific rather than part of the OpenAI compatibility API.

Configure:

```dotenv
CHAT_HISTORY_ENABLED=true
CHAT_HISTORY_DATABASE=/data/history.sqlite3
CHAT_HISTORY_MAX_TURNS=8
CHAT_HISTORY_MAX_CHARS=8000
OPENWEBUI_CHAT_FOLDER=meshcore
```

`CHAT_HISTORY_MAX_TURNS` and `CHAT_HISTORY_MAX_CHARS` bound the context sent to the
model; they do not delete the transcript. Failed direct-radio responses are kept
for review but are excluded from future model context. Channel history is disabled
because a channel is public and the current bridge does not establish a stable
per-sender channel conversation.

The Compose configuration mounts `/data` in the named
`meshcore-bridge-data` volume. Back up or remove that volume according to your
retention policy.

### Conversation commands

```text
/history          show this node's chat ID prefix and turn count
/new              retain the old Open WebUI chat and start a new one
/reset            alias for /new
/temp <question>  make a one-shot stateless request
```

`/temp` neither reads nor writes persistent history and does not create a temporary
Open WebUI chat. This avoids cluttering the interface with one-use conversations.
Conversation commands are available in direct messages only; channels remain
stateless.

## Reliability improvement: direct-message ACK tracking

For direct MeshCore messages, the bridge now:

1. asks the local companion to send each response chunk;
2. extracts that message's expected ACK code;
3. waits for the matching MeshCore `ACK` event;
4. retries the send if the remote ACK does not arrive.

Configure:

```dotenv
WAIT_FOR_DIRECT_ACK=true
ACK_TIMEOUT_SECONDS=12
DIRECT_SEND_RETRIES=2
```

The logs distinguish:

```text
Direct TX accepted locally
Direct TX delivered: ACK ... received
Direct TX ... had no remote ACK
```

This is useful for the failure mode where the AI generated a response and the local radio accepted it, but the remote/end node never received it.

For **channel messages**, there is no equivalent per-recipient end-to-end ACK. `/diag` therefore reports channel TX as local-radio accepted, not remote-delivery confirmed.

## Diagnostic examples

```text
/ping
```

Example:

```text
AI: PONG bridge=ok meshcore=ok openwebui=ok detail=200 12ms
```

```text
/last
```

Example:

```text
AI: Last: LLM=Qwen3-0.6B-GGUF ok 340ms 8s ago; TX=direct->a1b2c3 ACK chunks=2 3s ago
```

If delivery failed:

```text
AI: Last: ... TX=direct->a1b2c3 NO-ACK chunks=2 ...
```

`/diag` additionally asks the MeshCore companion for core, radio, and packet statistics when supported by the firmware/library.

## Install/update

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f
```

If upgrading from an older bundle, keep your existing `.env` values and add the new variables:

```dotenv
WAIT_FOR_DIRECT_ACK=true
ACK_TIMEOUT_SECONDS=12
DIRECT_SEND_RETRIES=2
SHORT_OUTPUT_TOKENS=80
SHORT_REPLY_CHARS=220
```

## USB

Verify first:

```bash
ls -l /dev/ttyACM*
```

Expected:

```text
/dev/ttyACM0
```

## Important diagnostic distinction

A direct reply can now have three states:

```text
1. LLM generated response
2. local MeshCore companion accepted TX
3. remote MeshCore node ACKed delivery
```

Previously the bridge only knew #1 and #2. v3 tracks #3 for direct messages.

For channel traffic, only #1 and #2 can be confirmed.


## v4: complex prompts returning `AI: (no response)`

Qwen3-0.6B-GGUF is a reasoning model. A small `max_tokens` budget can be consumed
by reasoning before any visible answer is produced.

v4 defaults to no-thinking for fast radio tasks:

```dotenv
DISABLE_THINKING_FAST=true
DISABLE_THINKING_ASK=true
DISABLE_THINKING_DEEP=false
```

`/short` always disables thinking.

The bridge sends both Qwen's `/no_think` instruction and
`chat_template_kwargs.enable_thinking=false` for compatible routes. It now logs
`finish_reason`, visible character count, and reasoning character count.

If Open WebUI still returns empty visible content, the bridge retries once:

```dotenv
EMPTY_RESPONSE_RETRY=true
EMPTY_RESPONSE_RETRY_TOKENS=512
```

If the retry remains empty, the bridge reports a service error rather than
`(no response)`.
