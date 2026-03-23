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
  starts HTTP server
  on random port
  registers metadata -----> stores {host, port, token}
  waits...                                                anycp pop
                            <---- queries available transfers
                            returns {host, port, token} ---->
                                                          downloads directly
Host A <============== file data (peer-to-peer) ===============> Host B
                                                          notifies completion -->
  sees "completed!"   <---- status: completed
```

The meta-server handles **only metadata** (filenames, addresses, tokens).
File data flows **directly** between hosts over a temporary HTTP connection.

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

Config is stored in `~/.config/anycp/config` (mode 0600).

## Usage

```bash
# Push a file (starts local HTTP server, waits for receiver)
anycp push myfile.tar.gz

# Push, specifying the address the other host should use to reach you
anycp push myfile.tar.gz --host 10.0.0.5

# Pop (download) — auto-selects if only one transfer available
anycp pop

# Pop into a specific directory
anycp pop /tmp/

# Pop with a specific filename
anycp pop /tmp/renamed.tar.gz

# Pop, overwriting existing file
anycp pop -f

# List available transfers
anycp ls

# Remove a transfer
anycp rm abc123def456
```

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
- **CLI:** Python 3.6+ (stdlib only, zero dependencies)
- **Network:** Pop host must be able to reach push host directly (same network, VPN, etc.)
