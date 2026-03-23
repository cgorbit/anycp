# anycp

Transfer files between SSH hosts without typing scp commands.

```
Host A:  anycp push data.tar.gz
Host B:  anycp pop
```

## How it works

```
Host A                     Meta Server                    Host B
------                     -----------                    ------
anycp push file.txt
  registers metadata -----> stores {filename, size, caps}
  waits...                                                anycp pop
                            <---- queries available transfers
                            selects transfer
                            claims ------>
                            <---- coordinator picks protocol
                            instructs push & pop sides
Host A <============== file data (peer-to-peer) ===============> Host B
                                                          notifies completion -->
  sees "completed!"   <---- status: completed
```

The meta-server handles **only metadata and coordination** — no file data passes through it.
File data flows **directly** between hosts. The server-side coordinator automatically
negotiates the best transfer protocol based on what both sides support.

## Transfer protocols

anycp supports multiple transfer protocols. The coordinator tries them in priority order
and falls back automatically if one fails:

| Protocol | Priority | How it works | Requires |
|---|---|---|---|
| **sky** | 1st | Torrent-based transfer via `sky share`/`sky get` | `sky` binary on both hosts |
| **nc** | 2nd | Raw TCP stream via netcat | `nc` binary on both hosts |
| **http** | 3rd | HTTP with gzip compression, progress bar | Nothing (stdlib) |

The CLI auto-detects which protocols are available on each host and advertises
its capabilities to the server. The coordinator picks the highest-priority protocol
supported by both sides.

## Server Setup

### Option 1: Docker

```bash
cd server
docker build -t anycp-server .
docker run -d -p 8787:8080 -e ANYCP_TOKEN=your-secret-token anycp-server
```

### Option 2: Direct

```bash
cd server
pip install -r requirements.txt
ANYCP_TOKEN=your-secret-token uvicorn main:app --host 0.0.0.0 --port 8787
```

### Server environment variables

| Variable | Default | Description |
|---|---|---|
| `ANYCP_TOKEN` | *(required)* | Auth token for API access |
| `ANYCP_TTL` | `3600` | Seconds before transfers expire |
| `ANYCP_POLL_TIMEOUT` | `30` | Long-poll timeout in seconds |

### Health check

```
GET /health → {"status": "ok", "transfers": <count>}
```

## CLI Setup

Copy `cli/anycp` to any host where you want to use it:

```bash
scp cli/anycp remote-host:~/bin/
chmod +x ~/bin/anycp
```

Then configure (once per host):

```bash
anycp config --server http://your-server:8787 --token your-secret-token
```

Or run `anycp config` without flags for interactive prompts.

Config is stored in `~/.config/anycp/config` (mode 0600).

## Usage

```bash
# Push a file (waits for receiver)
anycp push myfile.tar.gz

# Push, specifying the address the other host should use to reach you
anycp push myfile.tar.gz --host 10.0.0.5

# Pop (download) — auto-selects if only one transfer available
anycp pop

# "pull" is an alias for "pop"
anycp pull

# Pop into a specific directory
anycp pop /tmp/

# Pop with a specific filename
anycp pop /tmp/renamed.tar.gz

# Pop, overwriting existing file
anycp pop -f

# Force a specific protocol
anycp pop -p nc

# Interactively choose protocol
anycp pop -P

# List available transfers
anycp ls

# Remove a transfer
anycp rm abc123def456
```

## Features

- **Auto protocol negotiation** — coordinator picks the best protocol both sides support
- **Protocol fallback** — if the chosen protocol fails, the next one is tried automatically
- **SHA-256 checksum verification** — files are checksummed on push and verified after download
- **Gzip compression** — HTTP transfers are compressed on the fly (level 1) for speed
- **Progress bar** — downloads display real-time progress
- **Interactive selection** — when multiple transfers exist, you pick from a numbered list

## Config options

Set in `~/.config/anycp/config`:

```
server_url=http://your-server:8787
token=your-secret-token
push_host=10.0.0.5          # optional: default address for --host
verify_ssl=false             # optional: skip TLS verification
```

## Requirements

- **Server:** Python 3.10+, FastAPI, uvicorn
- **CLI:** Python 3.8+ (stdlib only, zero dependencies)
- **Network:** Pop host must be able to reach push host directly (same network, VPN, etc.)
