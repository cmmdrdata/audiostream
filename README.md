# AudioStream

Stream your music and audiobooks from your computer to any browser (desktop or phone).
It remembers where you stopped in every file.

**Needs:** Python 3.8+, and nothing else to install.

## Run it

```bash
python3 server.py --root ~/Music --root ~/Audiobooks
```

Open `http://<computer-ip>:8000` in a browser on any device on your network.

| Option | What it does |
|---|---|
| `--root PATH` | A folder you can browse (repeat it for more). Default: your home folder. Use `--root /` to browse everything. |
| `--port 8000` | Which port to listen on |
| `--host 0.0.0.0` | Bind address. Use `127.0.0.1` for this machine only. |
| `--password X` | Turns on a browser login (any username, this password). You can set `AUDIOSTREAM_PASSWORD` instead. |
| `--data FILE` | Where positions are saved (default `progress.json` next to `server.py`) |
| `--show-hidden` | Shows dot-folders |

## Features

- **Separate listeners:** the first time you open the page, you pick or create your name under "Who's listening?". The browser remembers your choice in a cookie for 10 years. Each listener has their own saved places, Resume buttons and Recent list. Tap **👤 name** at the top to switch listeners or delete one.
  - Profiles go by name, not device. Pick the same name on your phone and your laptop and you continue where you left off on either one.
  - Profiles aren't passwords. Anyone who can reach the server can choose any name, so `--password` is still what keeps strangers out.
  - Positions saved by an older version are kept under a listener called **Default**.

- Browse folders inside your roots, with breadcrumbs. Files sort naturally, so Chapter 2 comes before Chapter 10.
- **Resume:** the position is saved every 5 seconds, on pause and when you close the tab. Reopening a file starts 2 seconds before where you stopped.
  - Each folder has a **▶ Resume** button for the last file you played in it.
  - Folders you've listened in show an orange dot.
  - The **Recent** view lists your last 20 files with progress bars.
  - When you open the page, the last file you played is loaded and ready.
- **Controls:** back 30, back 20, play/pause, forward 20, forward 30, previous/next track, a seek bar, speed (0.75×–2×), a sleep timer (minutes or end of track), and restart.
- Plays the next file in the folder automatically, which suits audiobooks split into chapters.
- **Keyboard:** Space plays/pauses · ←/→ skips 20s · Shift+←/→ skips 30s · N/P goes to the next/previous track.
- **Lock screen, headphones and car:** these work through the Media Session API (skip buttons there jump 30s).
- Formats: mp3, m4a, **m4b**, aac, ogg, opus, flac, wav, webm. What actually plays depends on the browser. Safari doesn't play ogg/opus.

## Start at boot (Linux, systemd)

```bash
sudo ./deploy/install.sh            # runs the service as you (the user who ran sudo)
sudo nano /etc/audiostream.env      # set your folders, port and password
sudo systemctl restart audiostream
```

The installer puts `deploy/audiostream.service` in `/etc/systemd/system/`, filling in your username and this folder's path, then enables it. After that it starts at every boot, even if nobody logs in.

Useful commands:

```bash
systemctl status audiostream        # is it running?
journalctl -u audiostream -f        # live log
sudo systemctl restart audiostream  # run this after a git pull or a settings change
sudo systemctl disable --now audiostream   # turn it off
```

Notes:
- The service can read your files but can't change them. It can only write `progress.json` in the app folder.
- If your music is on a USB drive or network share, uncomment `RequiresMountsFor=` in the service file so it waits for the mount.
- Folder paths in `AUDIOSTREAM_ROOTS` can't contain spaces. Symlink a folder with spaces in its name, e.g. `ln -s "/mnt/My Books" ~/books`.

## Security notes

- Only paths inside `--root` folders can be listed or streamed. `..` and symlinks that point outside a root are blocked.
- This is plain HTTP. On your home network it's fine. To reach it from the internet, put it behind a reverse proxy with HTTPS (Caddy, nginx) or use Tailscale, and always set `--password`.
