# MeshCore -> Open WebUI bridge

Dockerized bridge between a USB-connected MeshCore Companion radio and Open WebUI.
It accepts direct or channel messages, routes prompts to configured models, and
returns length-limited responses over MeshCore.

Direct-message conversations use hybrid persistence:

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

- Docker Engine with Docker Compose
- A USB-connected MeshCore Companion radio, normally `/dev/ttyACM0`
- A running Open WebUI instance reachable from the container
- A dedicated Open WebUI user and API key
- Model IDs available to that Open WebUI user

Keep the API key only on the bridge. MeshCore nodes do not need Open WebUI
credentials.

## Docker setup

1. Confirm the radio device:

   ```bash
   ls -l /dev/ttyACM*
   ```

2. Create the configuration file:

   ```bash
   cp .env.example .env
   nano .env
   ```

3. Set at least the API key and model IDs:

   ```dotenv
   OPENWEBUI_URL=http://host.docker.internal:3000
   OPENWEBUI_API_KEY=REPLACE_ME

   FAST_MODEL=Qwen3-0.6B-GGUF
   ASK_MODEL=Qwen3.5-4B-MTP-GGUF
   DEEP_MODEL=qwen38-27b
   ```

4. Build and start the bridge:

   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

5. Verify operation from a node:

   ```text
   /ping
   /status
   /history
   ```

The Compose service maps the configured serial device, resolves
`host.docker.internal` to the Docker host, reads `.env`, and mounts the named
`meshcore-bridge-data` volume at `/data`. The volume preserves SQLite history when
the container is replaced.

### Common Docker operations

```bash
# Rebuild after code changes
docker compose up -d --build

# Follow logs
docker compose logs -f --tail=200

# Restart without rebuilding
docker compose restart

# Stop the bridge without deleting history
docker compose down

# Remove the bridge and its persistent conversation database
docker compose down -v
```

Back up the history volume before removing it:

```bash
docker run --rm \
  -v meshcore-openwebui-bridge_meshcore-bridge-data:/data \
  -v "$PWD":/backup \
  alpine tar czf /backup/meshcore-history.tgz -C /data .
```

The Compose project name may change the volume prefix. Use
`docker volume ls | grep meshcore-bridge-data` to find the actual name.

## Open WebUI conversation behavior

On the first persistent prompt from a direct-message node, the bridge:

1. resolves the node's full public key;
2. creates an Open WebUI chat for the API-key user;
3. creates or reuses the configured Open WebUI folder;
4. assigns the chat to that folder;
5. stores the public-key-to-chat-ID mapping in SQLite;
6. records and mirrors subsequent exchanges to the same chat.

If the bridge restarts, it reloads the mapping from SQLite. If the mapped Open
WebUI chat was deleted, the bridge creates a replacement on the next prompt.
Folder assignment is best-effort: a folder API error is logged but does not block
radio replies.

Only successfully delivered direct-message exchanges are reused as model context.
When direct ACK tracking is disabled, locally accepted responses are treated as
delivered. Failed and unacknowledged exchanges remain in the transcript for
diagnosis but are excluded from future model context.

The Open WebUI chat and folder endpoints are application-specific APIs. Test chat
creation, transcript updates, and folder assignment after upgrading Open WebUI.

## Commands

| Command | Behavior |
| --- | --- |
| Plain text | Persistent direct chat using `FAST_MODEL`; stateless on channels |
| `/fast <question>` | Use `FAST_MODEL` |
| `/ask <question>` | Use `ASK_MODEL` |
| `/deep <question>` | Use `DEEP_MODEL` |
| `/short <question>` | Use `FAST_MODEL` with the short reply limits |
| `/temp <question>` | One stateless request; does not read or write chat history |
| `/history` | Show the direct node's chat ID prefix and turn count |
| `/new` | Start a new direct-node chat and retain the old chat in Open WebUI |
| `/reset` | Alias for `/new` |
| `/models` | Show configured model routes |
| `/uptime` | Show bridge uptime and serial configuration |
| `/last` | Show the latest LLM and radio transmission result |
| `/ping` | Check bridge, MeshCore, and Open WebUI reachability |
| `/status` | Show health, uptime, and latest activity |
| `/diag` | Add available MeshCore core, radio, and packet statistics |
| `/info` or `/help` | Show the command list |

`/history`, `/new`, and `/reset` are supported only in direct messages. `/temp`
does not create an Open WebUI chat, which prevents one-use chats from cluttering
the interface.

## Configuration reference

### MeshCore and access control

| Variable | Default | Description |
| --- | --- | --- |
| `MESHCORE_SERIAL` | `/dev/ttyACM0` | Host and container serial-device path |
| `MESHCORE_BAUD` | `115200` | Serial baud rate |
| `MESHCORE_DEBUG` | `false` | Enable MeshCore library and bridge debug logging |
| `ENABLE_DIRECT_MESSAGES` | `true` | Process direct messages |
| `ENABLE_CHANNEL_MESSAGES` | `true` | Process channel messages |
| `ALLOWED_CHANNELS` | empty | Comma-separated allowed channel indexes; empty allows all |
| `ALLOWED_CONTACT_PREFIXES` | empty | Comma-separated public-key prefixes; empty allows all |
| `AI_PREFIX` | `AI:` | Prefix added to transmitted responses and used to prevent reply loops |
| `MAX_CONCURRENT_REQUESTS` | `1` | Maximum simultaneous request handlers |
| `DEDUPE_TTL_SECONDS` | `300` | Duplicate incoming-message retention period |

### Open WebUI and models

| Variable | Default | Description |
| --- | --- | --- |
| `OPENWEBUI_URL` | `http://host.docker.internal:3000` | Open WebUI base URL from inside the container |
| `OPENWEBUI_API_KEY` | none | Required API key for the dedicated bridge user |
| `FAST_MODEL` | `Qwen3-0.6B-GGUF` | Default, `/fast`, `/short`, and `/temp` model |
| `ASK_MODEL` | `FAST_MODEL` | `/ask` model |
| `DEEP_MODEL` | `FAST_MODEL` | `/deep` model |
| `TEMPERATURE` | `0.2` | Completion sampling temperature |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Open WebUI HTTP timeout |

Model names must exactly match IDs exposed by Open WebUI.

### Persistent history

| Variable | Default | Description |
| --- | --- | --- |
| `CHAT_HISTORY_ENABLED` | `true` | Enable persistent direct-node conversations |
| `CHAT_HISTORY_DATABASE` | `/data/history.sqlite3` | SQLite database path |
| `CHAT_HISTORY_MAX_TURNS` | `8` | Maximum completed exchanges supplied as model context |
| `CHAT_HISTORY_MAX_CHARS` | `8000` | Maximum history characters supplied as model context |
| `OPENWEBUI_CHAT_FOLDER` | `meshcore` | Open WebUI folder for node chats; empty disables folder assignment |

The history limits constrain inference context; they do not delete stored or
mirrored transcript entries.

### Response limits and radio chunking

| Variable | Default | Description |
| --- | --- | --- |
| `MAX_OUTPUT_TOKENS` | `180` | Normal completion token limit |
| `MAX_REPLY_CHARS` | `700` | Normal reply character limit before radio chunking |
| `SHORT_OUTPUT_TOKENS` | `80` | `/short` completion token limit |
| `SHORT_REPLY_CHARS` | `220` | `/short` reply character limit |
| `MESH_CHUNK_CHARS` | `120` | Target radio chunk size, clamped to 50-125 characters |
| `CHUNK_DELAY_SECONDS` | `1.0` | Delay between transmitted chunks |

Long replies are normalized, truncated to the applicable reply limit, and split
into numbered chunks such as `AI: [1/3] ...`.

### Direct-message delivery

| Variable | Default | Description |
| --- | --- | --- |
| `WAIT_FOR_DIRECT_ACK` | `true` | Wait for the matching remote ACK for every direct chunk |
| `ACK_TIMEOUT_SECONDS` | `12` | ACK wait time per attempt |
| `DIRECT_SEND_RETRIES` | `2` | Direct send attempts per chunk |

Direct delivery has three stages: model response generated, local companion
accepted the transmission, and remote node acknowledged it. Channel transmission
can confirm only local companion acceptance.

### Reasoning and empty-response handling

| Variable | Default | Description |
| --- | --- | --- |
| `DISABLE_THINKING_FAST` | `true` | Disable supported reasoning mode for `FAST_MODEL` |
| `DISABLE_THINKING_ASK` | `true` | Disable supported reasoning mode for `ASK_MODEL` |
| `DISABLE_THINKING_DEEP` | `false` | Disable supported reasoning mode for `DEEP_MODEL` |
| `EMPTY_RESPONSE_RETRY` | `true` | Retry once when no visible assistant content is returned |
| `EMPTY_RESPONSE_RETRY_TOKENS` | `512` | Token limit for the no-thinking retry |

`/short` always requests no-thinking mode. Compatible backends receive both the
`/no_think` prompt instruction and `chat_template_kwargs.enable_thinking=false`.

## Diagnostics

Useful log states include:

```text
Direct TX accepted locally
Direct TX delivered: ACK ... received
Direct TX ... had no remote ACK
```

Use `/last` to distinguish model completion from local transmission and remote
delivery. Use `/diag` to request available MeshCore core, radio, and packet
statistics in addition to Open WebUI reachability.

## Updating

```bash
git pull
docker compose up -d --build
docker compose logs -f --tail=200
```

Keep the existing `.env`, compare it with `.env.example`, and add newly introduced
variables. Do not run `docker compose down -v` during a normal update because it
deletes the persistent conversation volume.
