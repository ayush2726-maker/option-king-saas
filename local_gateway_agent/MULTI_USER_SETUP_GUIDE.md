# OKAI Multi-User Static-IP Gateway

Each user runs one gateway on their own Android/Termux phone or Windows/Linux desktop. The device must use that user's own public static IPv4, registered in that user's own Angel One SmartAPI app.

## What stays separate for every user

- OKAI login and subscription
- Angel One account and SmartAPI app
- Public static IPv4
- Gateway token
- Broker credentials stored only on the user's device
- Order command queue, live trades and arm/disarm state

## Android / Termux

```bash
pkg update -y
pkg install python git tmux -y
git clone https://github.com/ayush2726-maker/option-king-saas.git
cd option-king-saas/local_gateway_agent
python -m pip install -r requirements.txt
python okai_local_gateway_v2.py setup
python okai_local_gateway_v2.py doctor
```

Start in the background:

```bash
mkdir -p ~/.okai
tmux kill-session -t okai-gateway 2>/dev/null
tmux new -d -s okai-gateway \
  "cd $HOME/option-king-saas/local_gateway_agent && python -u okai_local_gateway_v2.py run 2>&1 | tee $HOME/.okai/gateway_v2.log"
```

Check:

```bash
tmux ls
tail -n 30 ~/.okai/gateway_v2.log
```

## Windows desktop

```powershell
git clone https://github.com/ayush2726-maker/option-king-saas.git
cd option-king-saas\local_gateway_agent
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python okai_local_gateway_v2.py setup
python okai_local_gateway_v2.py doctor
python okai_local_gateway_v2.py run
```

Keep the terminal open, or configure Windows Task Scheduler to start the final `run` command at login.

## Linux desktop / VPS at the user's static IP

```bash
git clone https://github.com/ayush2726-maker/option-king-saas.git
cd option-king-saas/local_gateway_agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python okai_local_gateway_v2.py setup
python okai_local_gateway_v2.py doctor
python -u okai_local_gateway_v2.py run
```

## Live safety

Setup and doctor leave both local and server entry gates disarmed. Arm only from the gateway device:

```bash
python okai_local_gateway_v2.py arm
```

Type exactly:

```text
ARM LIVE 1 LOT
```

Emergency disarm:

```bash
python okai_local_gateway_v2.py disarm
```

Disarming blocks new entries. Existing local positions remain monitored for ATR SL, profit-lock trailing and EOD exit.

## Never share

Never send the Angel API key, MPIN, OKAI password, TOTP secret, JWT, refresh token or gateway token in chat, screenshots or support messages.
