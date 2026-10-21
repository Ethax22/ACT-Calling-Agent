# Deploying the phone agent to the Azure test VM

Phase 4 of `ACT-Voice-Assistant/docs/Calling_Agent_DIY_Implementation_Plan.md`. This VM is for **testing only**; launch runs on the college's own host.

## What you end up with

| Service | Port | Reachable from |
|---|---|---|
| nginx (HTTPS, free Let's Encrypt certificate) | 80, 443 | the internet |
| `act-phone-agent` (calls, browser test line) | 8002 | nginx only |
| `act-rag-backend` (shared brain) | 8000 | the VM only |
| `act-erp-lookup` (records, transfer numbers) | 8100 | the VM only |

- The three services start on boot and restart if they crash (systemd).
- The browser test page `https://<your DNS name>/test` asks for a password you set in step 8.
- rag-backend and erp-lookup are **not** public. erp-lookup can read student records, and only the phone agent on the same VM needs it.
- The website voice service (`voice-pipeline/server.py`) is not run here. The phone agent imports its modules directly, so it doesn't need that service.

VM facts (2026-10-21): `act-agent-vm`, Central India, Ubuntu 24.04, Standard_D2s_v5, public IP `20.244.2.204`, user `azureuser`, key `C:\Users\praka\.ssh\act-agent-vm_key.pem`. Ports 80 and 443 are open; SSH is limited to your IP.

Commands marked **laptop** run in PowerShell on your Windows machine. Commands marked **VM** run inside the SSH session.

---

## 1. Log in (laptop)

```powershell
ssh -i "$env:USERPROFILE\.ssh\act-agent-vm_key.pem" azureuser@20.244.2.204
```

## 2. Install system packages (VM, ~3 minutes)

```bash
sudo apt update && sudo apt -y upgrade
```

```bash
sudo apt install -y python3-venv python3-pip git nginx certbot python3-certbot-nginx apache2-utils sqlite3
```

If an upgrade screen asks which services to restart, press Enter to accept. If it says a reboot is needed, run `sudo reboot`, wait a minute, and log in again.

## 3. Get the code (VM)

The website repo is named `ACT-Voice-Assitant` on GitHub (missing "s"). The folder on the VM must be `ACT-Voice-Assistant`, because the phone agent looks for it by that name next to its own folder.

```bash
cd ~ && git clone https://github.com/Ethax22/ACT-Voice-Assitant.git ACT-Voice-Assistant && git clone https://github.com/Ethax22/ACT-Calling-Agent.git
```

## 4. Python environments (VM, ~5 to 10 minutes)

The constraint files hold the exact versions from your tested Windows setup.

```bash
cd ~/ACT-Voice-Assistant/rag-backend && python3 -m venv venv && venv/bin/pip install --upgrade pip && venv/bin/pip install -r requirements.txt -c ~/ACT-Calling-Agent/deploy/constraints-rag-backend.txt
```

```bash
cd ~/ACT-Calling-Agent/phone-agent && python3 -m venv venv && venv/bin/pip install --upgrade pip && venv/bin/pip install -r requirements.txt -c ~/ACT-Calling-Agent/deploy/constraints-phone-agent.txt
```

Each should end with `Successfully installed ...`. If pip says a pinned version can't be found for Linux, send me the error.

## 5. Copy the secret files from the laptop (laptop)

These files are git-ignored, so they are not on GitHub. Open a **new PowerShell window on the laptop** (leave the SSH window open).

First create the folder for the Google key on the VM:

```powershell
ssh -i "$env:USERPROFILE\.ssh\act-agent-vm_key.pem" azureuser@20.244.2.204 "mkdir -p ~/ACT-Voice-Assistant/rag-backend/secrets"
```

Then copy the files. Paste this whole block:

```powershell
$k = "$env:USERPROFILE\.ssh\act-agent-vm_key.pem"; $vm = "azureuser@20.244.2.204"
scp -i $k "C:\ACT-Calling-Agent\.env" "${vm}:ACT-Calling-Agent/.env"
scp -i $k "C:\ACT-Calling-Agent\erp-lookup\.env" "${vm}:ACT-Calling-Agent/erp-lookup/.env"
scp -i $k "C:\ACT-Calling-Agent\erp-lookup\erp.db" "${vm}:ACT-Calling-Agent/erp-lookup/erp.db"
scp -i $k "C:\ACT-Voice-Assistant\rag-backend\.env" "${vm}:ACT-Voice-Assistant/rag-backend/.env"
scp -i $k "C:\ACT-Voice-Assistant\voice-pipeline\.env" "${vm}:ACT-Voice-Assistant/voice-pipeline/.env"
$sa = (Get-ChildItem "C:\ACT-Voice-Assistant\rag-backend\secrets\*.json")[0].FullName
scp -i $k $sa "${vm}:ACT-Voice-Assistant/rag-backend/secrets/service-account.json"
```

Each line prints the file name with `100%`.

**`erp.db` holds the records the agent reads.** The contacts are fake, but the marks and attendance may be real student data. Keep SSH limited to your IP, and delete the VM when testing ends.

## 6. Point the settings at the VM (VM)

The copied `.env` files still contain Windows paths. This script swaps them for VM paths and points the phone agent at the local services:

```bash
bash ~/ACT-Calling-Agent/deploy/vm_env_setup.sh
```

It ends with `OK: .env paths set for this VM` and prints six settings. Check that `PHONE_BACKGROUND=off` is among them, as on your laptop.

## 7. Start the services (VM)

```bash
sudo cp ~/ACT-Calling-Agent/deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now act-rag-backend act-erp-lookup act-phone-agent
```

Wait about 60 seconds (the first start renders the greeting and loads the speech models), then:

```bash
curl -s 127.0.0.1:8100/health; echo; curl -s 127.0.0.1:8000/health; echo; curl -s 127.0.0.1:8002/health; echo
```

Expected: `{"ok":true}`, `{"status":"ok"}`, `{"status":"ok"}`. The very first rag-backend start also builds its search index (the embedding cache is not on GitHub), so if 8000 or 8002 doesn't answer yet, wait another minute and run the line again. If one still fails, see **Problems** below.

## 8. nginx and the test page password (VM)

Put your DNS name in the first line (Azure portal: VM → Overview → DNS name):

```bash
DNS=act-agent.centralindia.cloudapp.azure.com
```

```bash
sudo cp ~/ACT-Calling-Agent/deploy/nginx/act-agent.conf /etc/nginx/sites-available/act-agent && sudo sed -i "s/DNS_NAME_HERE/$DNS/" /etc/nginx/sites-available/act-agent && sudo ln -sf /etc/nginx/sites-available/act-agent /etc/nginx/sites-enabled/act-agent && sudo rm -f /etc/nginx/sites-enabled/default
```

Set the test page password (username `prakash`; it asks for the password twice):

```bash
sudo htpasswd -c /etc/nginx/act-agent.htpasswd prakash
```

```bash
sudo nginx -t && sudo systemctl reload nginx && curl -s "http://$DNS/health"; echo
```

Expected: `syntax is ok`, then `{"status":"ok"}`.

## 9. HTTPS certificate (VM)

The browser only allows the microphone on HTTPS pages, and Vobiz needs HTTPS too. Use your own email for expiry notices:

```bash
sudo certbot --nginx -d "$DNS" --redirect --agree-tos --no-eff-email -m YOUR_EMAIL
```

It ends with `Successfully deployed certificate`. The certificate renews itself; nothing to remember.

## 10. Check from the laptop

1. Open `https://<your DNS name>/health` in the browser. It shows `{"status":"ok"}` with a padlock.
2. Open `https://<your DNS name>/test`, log in as `prakash` with your password, and allow the microphone.
3. Hold a call as you did locally, with caller numbers from `LOCAL_TEST.md` (for example 9000000003). Expect the greeting within about a second of connecting.

This completes plan steps 4.2 and 4.3.

---

## Day to day (VM)

| To do this | Run |
|---|---|
| Watch the phone agent live | `journalctl -u act-phone-agent -f` (Ctrl+C to stop watching) |
| See the last 200 lines of a service | `journalctl -u act-rag-backend -n 200 --no-pager` |
| Restart everything | `sudo systemctl restart act-rag-backend act-erp-lookup act-phone-agent` |
| Status of all three | `systemctl status act-rag-backend act-erp-lookup act-phone-agent --no-pager` |

**Updating the code** after new commits are pushed:

```bash
cd ~/ACT-Voice-Assistant && git pull && cd ~/ACT-Calling-Agent && git pull && sudo systemctl restart act-rag-backend act-erp-lookup act-phone-agent
```

If a `requirements.txt` changed, rerun its line from step 4 before restarting. If a file in `deploy/systemd/` changed, rerun step 7's first command.

**Changing a setting:** edit `~/ACT-Calling-Agent/.env` (for example `nano ~/ACT-Calling-Agent/.env`), then restart. Example: add `TURN_TAKING_TRACE=1` to log every voice-detection and interruption event.

**Call transcripts and audio:** `~/ACT-Voice-Assistant/voice-pipeline/logs/<UTC date>/phone-test-<id>/`. To copy one to the laptop (laptop):

```powershell
scp -i "$env:USERPROFILE\.ssh\act-agent-vm_key.pem" -r "azureuser@20.244.2.204:ACT-Voice-Assistant/voice-pipeline/logs/<date>/<folder>" .
```

**Unlock the test numbers** after three wrong dates of birth:

```bash
sqlite3 ~/ACT-Calling-Agent/erp-lookup/erp.db "delete from audit where caller like '90000000%' and result in ('verification_failed','locked_out');"
```

## Problems

| What you see | What to do |
|---|---|
| A health check in step 7 fails | `journalctl -u <service name> -n 80 --no-pager`. The usual cause is a missing key: the error names it (`... is missing from ...`) |
| `MISSING:` from step 6 | A copy in step 5 failed; rerun that line |
| `502 Bad Gateway` in the browser | The phone agent is down or still starting: check step 7's health line |
| certbot: `Timeout during connect` or `NXDOMAIN` | Port 80 is closed in the Azure network rules, or the DNS name is wrong or not yet saved |
| SSH: `Connection timed out` | Your IP changed. Azure portal → VM → Networking → SSH rule → Source: My IP address → Save |
| The test page loads but the call won't connect | Reload the page and log in again; the browser must send the password with the call connection too. Then check `journalctl -u act-phone-agent -n 80` |

## Cost and cleanup

The VM costs about $0.10 an hour (about $74 a month) while running, plus a little for disk and IP. To pause it: portal → VM → **Stop**, and wait for "Stopped (deallocated)". While stopped, the test page and phone line don't answer. When testing is over, delete the resource group `act-calling-agent-rg`; that removes the VM, disk, IP and network together.
