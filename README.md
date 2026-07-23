# Kirana Software — Railway branch

Cloud-ready Kirana Software v0.4.1.

## Railway setup

- Deploy this `kirana-software` branch.
- Attach a persistent volume to the service at `/data`.
- Generate a public Railway domain.
- The app stores SQLite at `/data/kirana.db`.

## Move existing Termux data

1. Download **Full Database Backup** from the local Kirana Software settings.
2. Open the Railway URL and create a temporary account.
3. Settings → **Restore Full Backup** and upload the `.db` backup.
4. Log in with the credentials from the restored database.
