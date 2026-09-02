# OKAI Single-Phone Multi-Account Gateway

One Android/Termux phone can now run multiple OKAI accounts at the same time. Each account runs in an isolated child worker with its own gateway token, broker credentials, SQLite state, STOP file, command history and local positions.

Supported local brokers:

- Angel One — `okai_local_gateway_v3.py`
- Upstox — `okai_local_gateway_upstox.py`

The supervisor is `okai_multi_account_agent.py`.

## Isolation model

For a profile named `ayush`, local files are stored under:

```text
~/.okai_multi/profiles/ayush/.okai/
```

For a profile named `rakesh`:

```text
~/.okai_multi/profiles/rakesh/.okai/
```

Because every worker receives a different HOME directory, the two accounts cannot share the same local gateway config, gateway token, STOP file or state database.

## Android / Termux installation

```bash
pkg update -y
pkg install python git tmux -y
git clone https://github.com/ayush2726-maker/option-king-saas.git
cd option-king-saas/local_gateway_agent
python -m pip install -r requirements.txt
```

## Example: Ayush on Angel One + Rakesh on Upstox

Add both profiles:

```bash
python okai_multi_account_agent.py add ayush --broker angelone
python okai_multi_account_agent.py add rakesh --broker upstox
python okai_multi_account_agent.py list
```

Pair and configure each account separately:

```bash
python okai_multi_account_agent.py setup ayush
python okai_multi_account_agent.py doctor ayush

python okai_multi_account_agent.py setup rakesh
python okai_multi_account_agent.py doctor rakesh
```

Angel setup asks for that account's SmartAPI credentials. Upstox setup asks for that account's daily Access Token. Never reuse one account's broker token in the other profile.

The SaaS gateway pairing currently requires a valid public static IPv4 for each paired OKAI account. When two accounts use the same phone/network they can use the same observed static IPv4, while gateway tokens and broker credentials remain separate.

## Arm accounts separately

Setup and doctor leave both accounts disarmed.

```bash
python okai_multi_account_agent.py arm ayush
python okai_multi_account_agent.py arm rakesh
```

For each profile, type the existing exact live confirmation phrase when prompted.

Disarm only one account without stopping the other:

```bash
python okai_multi_account_agent.py disarm ayush
```

or:

```bash
python okai_multi_account_agent.py disarm rakesh
```

## Start both from one command

Foreground check:

```bash
python -u okai_multi_account_agent.py run-all
```

Background Termux session:

```bash
tmux kill-session -t okai-multi 2>/dev/null
tmux new -d -s okai-multi \
  "cd $HOME/option-king-saas/local_gateway_agent && python -u okai_multi_account_agent.py run-all"
```

Check supervisor:

```bash
tmux ls
python okai_multi_account_agent.py list
```

Each worker writes its own log:

```bash
tail -n 40 ~/.okai_multi/profiles/ayush/.okai/multi_gateway.log
tail -n 40 ~/.okai_multi/profiles/rakesh/.okai/multi_gateway.log
```

## Safety behavior

- One worker crash does not merge credentials or state with another profile.
- `run-all` refuses to start if any enabled profile has not completed setup.
- Arming/disarming remains per OKAI user.
- Entry commands are leased by the authenticated per-user gateway token.
- Existing local positions continue to use the local risk/exit engine for that profile.
- Broker label in new gateway trades is normalized from the entry payload (`angelone` or `upstox`).

## Important Upstox note

Upstox uses a daily Access Token. Refresh the token in the Upstox profile when it expires before arming new entries. The current SaaS heartbeat funds table still accepts Angel One local-funds snapshots only; this does not mix accounts or change Upstox order routing, but the Upstox local-funds snapshot is not yet used by that table.

## Never share

Never send an OKAI password, Angel API key/MPIN/TOTP, Upstox Access Token, JWT, refresh token or local gateway token in chat, screenshots or support messages.
