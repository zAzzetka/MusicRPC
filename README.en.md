<p align="left">
  <img src="assets/app.png" alt="" width="96">
</p>

# Music RPC

[Русский](README.md)

A small Windows program that sits in the tray and shows what you're listening to on your Discord profile: track, artist, cover art and a progress bar.

It reads the track from the Windows media centre, the same panel that pops up when you press the volume keys. That means it works with Yandex Music, Spotify, VK Music, a browser, or any other player that shows up there. You don't have to log in anywhere.

<p align="center">
  <img src="docs/discord-profile.png" alt="The 'Listening to MusicRPC' card in a Discord profile" width="290">
  &nbsp;&nbsp;&nbsp;
  <img src="docs/settings-main.png" alt="Settings window" width="305">
</p>

There is also a "listen together" mode: a friend starts broadcasting, you enter their code, and your Discord shows what they are playing. If you have the same track open, your player copies their pause and seeking. More on that below.

The interface is in Russian for now. Where this page mentions a Russian label, the English meaning is in brackets. Translations are welcome: the strings live in `ymrpc/ui.py`, `ymrpc/tray.py` and `ymrpc/status.py`.

## What you need

Windows 10 (version 1809 or newer) or Windows 11. It won't run on macOS or Linux, because it relies on the Windows media centre and the registry.

Discord has to be the desktop app. A browser tab won't receive the status.

You don't have to install Python. `install.bat` picks a suitable one that's already there (3.10 to 3.14 with tkinter, 3.13 preferred). If there isn't one, it downloads its own Python 3.13 into the `.tools` folder (about 50 MB) and leaves your system Python alone. That download needs access to GitHub. If you run from source by hand, you need Python 3.9 or newer.

The internet is only needed for cover art and for "listen together".

## Installation

First you have to create an application in Discord. That's how Rich Presence works: a status is always shown on behalf of some application.

1. Open <https://discord.com/developers/applications> and click New Application.
2. Pick a name. Your friends will see it in the status: "Listening to *name*".
3. If you like, upload an icon under General Information. A ready one is in `assets/app.png`.
4. Copy the Application ID, a number of 17-20 digits. You don't need the public key.

Now the program itself.

1. Download the repository (Code, then Download ZIP) and unpack it into a permanent folder, for example `C:\Programs\MusicRPC`. If you turn on autostart, it's best not to move the folder afterwards.
2. Run `install.bat`. It creates a `.venv` environment and installs the dependencies, once.
3. Run `start.bat`. The icon shows up in the tray, maybe among the hidden icons next to the clock.
4. On the first launch the settings window opens. Paste the Application ID and click "Сохранить" (Save).

Discord itself has to allow sharing your activity (the activity privacy section; the exact name depends on the Discord version).

Play a track. The status should appear in your profile within a couple of seconds. If it doesn't, run `diagnose.bat` (described below).

### Updating

Close the program (right-click the icon, "Выход" / Exit), replace the files with the new ones and run `install.bat` again so it can fetch any missing packages. Leave the `.venv` and `.tools` folders alone. Then run `start.bat`. Your settings are kept.

### From the command line

```bat
git clone <URL from the Code button>
cd MusicRPC

py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: no console window
start "" .venv\Scripts\pythonw.exe music_rpc.pyw

:: with a console and a verbose log
.venv\Scripts\python.exe music_rpc.pyw --debug
```

Flags: `--diagnose` does what `diagnose.bat` does, `--debug` turns on the verbose log, and `--autostart` is internal, the autostart entry adds it.

## Using it

The tray icon colour tells you the state: purple when something is playing, muted purple when paused, grey when there's no music or the program is off, red when something is wrong (hover over it and the tooltip says what).

Right-clicking opens a menu. At the top is the current state, then some switches: whether to show the status, the cover, the progress bar, the link button, and the status while paused. The "Треки" (Tracks) submenu lets you open the current track in the browser, copy its link and see the last ten tracks (clicking one opens it, or copies the name if there is no link). The list is kept in memory only. The "Слушать вместе" (Listen together) submenu is covered below. Double-clicking the icon opens the settings.

## Settings

<p align="center">
  <img src="docs/settings-main.png" alt="The 'Основное' (General) tab" width="32%">
  <img src="docs/settings-texts.png" alt="The 'Тексты и фильтр' (Texts and filter) tab" width="32%">
  <img src="docs/settings-together.png" alt="The 'Слушать вместе' (Listen together) tab" width="32%">
</p>

Everything is stored in `%APPDATA%\MusicRPC\config.json`, but it's easier to use the window. If you edit the file by hand, close the program first, otherwise it will overwrite the file with its own values.

| Parameter | Default | What it is |
|---|---|---|
| `client_id` | empty | Application ID from Discord |
| `enabled` | `true` | master switch for the status |
| `app_filters` | `["yandex", "яндекс"]` | which players to take music from, see below. An empty list means "any playing player" |
| `show_cover` | `true` | cover art |
| `show_progress` | `true` | progress bar |
| `show_button` | `true` | the track link button |
| `show_paused` | `false` | show the status while paused. When off, the status disappears on pause |
| `button_label` | `"Открыть трек"` ("Open track") | button caption, up to 32 characters |
| `details_format` | `"{title}"` | first line |
| `state_format` | `"{artist}"` | second line |
| `large_text_format` | `"{album}"` | tooltip when hovering over the cover |
| `status_display` | `"name"` | what to show in the server member list: `name` (the application name), `details` (first line) or `state` (second) |
| `hide_keywords` | `[]` | words for the "don't show" filter |
| `poll_interval` | `2.0` | how often to poll the player, 1 to 10 seconds |
| `autostart` | `false` | start with Windows |
| `yandex_token` | empty | optional Yandex OAuth token, in case covers can't be found |
| `together_*` | | "listen together" settings, see below |

The token is stored in the file as plain text, so don't publish your `config.json` anywhere.

### Line templates

Three lines accept the placeholders `{title}` (track), `{artist}` (artist) and `{album}` (album). For example, `{artist} — {title}`.

If a value is missing, dangling separators at the edges are trimmed: `{artist} — {title}` without an artist gives just the title. An unknown placeholder stays as it is, which makes a typo easy to spot. Discord accepts strings of 2 to 128 characters, so longer ones are cut off and single-character ones are padded with an invisible character. While paused (if showing while paused is on), ⏸ goes in front of the second line.

### Choosing where music comes from

Windows knows every player by a system identifier. In the "Приложения" (Applications) field it's enough to enter a piece of it, case doesn't matter: `yandex`, `spotify`, `chrome`. Separate several players with commas.

If you don't know what your player is called, play a track in it and click "Проверить" (Check) next to the field, or run `diagnose.bat`. The list of what Windows sees includes your player's identifier.

If several sources are playing, the one that is playing is chosen, with a preference for the one Windows considers "current". Browsers tell Windows about any playback, so if you add a browser, the status may pick up videos as well as music.

### Don't show

The "Тексты и фильтр" tab has a "Не показывать" (Don't show) field. Enter words separated by commas: if any of them appears in the track title, the artist, the album or the player name, the status isn't shown and the track doesn't go to "listen together" either. The icon turns grey and the tooltip says "Скрыто вашим фильтром" (Hidden by your filter).

## Listen together

One person (the host) broadcasts what they're listening to, and the others (the guests) listen to the same thing. You don't need extra accounts or servers of your own: participants exchange short encrypted messages through a shared MQTT server.

A caveat right away: what gets synchronised is the state, not the sound. Everyone plays it in their own player with their own subscription. The program can't start the right track in someone else's player. It can open a link to the track in the browser, though, and once both of you have the same track playing, it repeats the host's pause and seeking.

### Turning it on

Host: in the tray choose "Слушать вместе", then "Я транслирую (хост)" (I broadcast (host)). The program creates a room code like `ABCD-EFGH-JKLM` and copies it to the clipboard. Send that code to your friends.

Guest: in the tray choose "Слушать вместе", then "Я слушаю друга (гость)…" (I listen to a friend (guest)…), or use the "Слушать вместе" tab in the settings. Paste the code, pick "guest" mode, save.

While the host is listening to music, the guests see their track. In the host's menu it says how many people are listening and what they're called. To leave, choose "Выключено" (Off). The host doesn't need Discord or a Client ID, broadcasting works without them.

### What the guest gets

Two things are on by default. First, the host's track shows up in your Discord (cover, time, button) with the caption "· вместе с *name*" ("together with name"). That works even if nothing is playing on your side. Second, if you have the same track open, your player repeats the host's pause and play, and seeks when you drift apart by more than three seconds.

If you pause it yourself, the program won't argue until the host toggles something. Not every player allows outside control. If yours refuses (most often it can't seek), the program stops trying until the track changes.

A third option is off: opening the host's track in the browser when you're playing a different one. It opens the track page in Yandex Music, once per track. The menu item "Открыть трек хоста" (Open the host's track) does the same by hand.

### Checking that it works

You can do it without a friend and even without Discord. Open the settings, go to the "Слушать вместе" tab and click "Проверить связь" (Check connection), which takes up to 30 seconds. The program starts a host and a guest on your computer in a random room and passes a test track through the real server. It also checks encryption and that a guest who joins later gets the current track. The third section of `diagnose.bat` does the same.

This checks the connection. How a particular player reacts to pause and seeking you can only see in a real test with two people.

### How it's protected

The room code produces (PBKDF2-HMAC-SHA256, 100,000 iterations) the channel name on the server and the keys. The code is 12 characters out of 32, which is 60 bits, so it can't be found by brute force. Messages are encrypted and signed (HMAC-SHA256 in counter mode plus an HMAC-SHA256 tag). The server sees only ciphertext and your IP.

This protects against casual onlookers on a public server. If you have something truly secret, it is not a replacement for well-reviewed cryptography. Whoever knows the code is in the room, so send it only to friends; you can change the lock with the "Новый код" (New code) button. Links coming from the host are accepted only from Yandex sites. Everything else is dropped.

### Servers

By default the public brokers `broker.hivemq.com` and `broker.emqx.io` (port 1883) are used. If one is down, the program switches to the other. They're free and come with no guarantees. If your ISP blocks them or you want independence, run your own (any MQTT 3.1.1 broker will do, for example Mosquitto) and enter its address on the "Слушать вместе" tab: `mqtt://address:1883` or `mqtts://address:8883`. Everyone has to use the same address.

### Parameters

| Parameter | Default | What it is |
|---|---|---|
| `together_mode` | `"off"` | `off`, `host` or `guest` |
| `together_room` | empty | room code |
| `together_name` | empty | how friends see you (otherwise "Хост" or "Гость", Host or Guest) |
| `together_broker` | empty | server address, empty means the public ones, several may be comma-separated |
| `together_mirror` | `true` | guest: show the host's track in your own Discord |
| `together_sync_player` | `true` | guest: repeat the host's pause and seeking |
| `together_auto_open` | `false` | guest: open the host's track in the browser |
| `together_suffix` | `"· вместе с {name}"` | caption in the guest's second status line |

## Autostart

Turn it on with the checkbox in the settings or the tray menu item. The program writes itself into `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` (no administrator rights needed) with the `--autostart` flag. When it gets the flag, it waits for the taskbar to appear (up to 90 seconds) and five more seconds so that Discord and the network can come up. While Discord is starting it keeps trying to connect by itself, so the status shows up 10-20 seconds after you sign in to Windows.

If there's no status after a reboot:

1. Open Task Manager (Ctrl+Shift+Esc), the Startup tab. The entry should be enabled.
2. Look at `%APPDATA%\MusicRPC\app.log`. A line "Запуск при входе в Windows" ("Started at Windows sign-in") with the boot time means the program started. If it isn't there, check `crash.log` next to it.
3. Don't move the program folder. If you did, run the program by hand once and the path in the registry will be updated.

## Building an .exe

Optional. Run `install.bat`, then `build_exe.bat`, and `MusicRPC.exe` appears in `dist`, with the icon from `assets/app.ico`. It doesn't need Python next to it. Autostart from the `.exe` works the same way. If the file still has an old icon, that's the Windows icon cache: rename the file or restart Explorer.

## How it works

A player tells Windows the title, artist, album, position and state. The program reads all of that through `GlobalSystemMediaTransportControlsSessionManager` (the `winrt-*` packages), picks sessions by the `app_filters` setting and takes the one that's playing. The position is recomputed: Windows records it at the moment `last_updated_time`, and the program adds the time elapsed since then. If the player reports neither a position nor a duration, the time is counted by the program's own clock, pauses included, and Discord shows an elapsed counter instead of a bar.

Discord can only display an image by URL, while Windows hands over the cover as a data stream. So the cover is looked up by title and artist: first in Yandex Music (which also gives the track link), then Deezer, then iTunes. What's found is checked against the title and artist, looking at the first six results. The lookup runs in a separate thread, the first display of a track waits for the cover for at most 4 seconds, and results are cached. Only the Python standard library is used.

The status goes through Discord's local channel (`discord-ipc-N`) using `pypresence`. An update is sent when the track changes, on pause, on seeking (the position drifted by more than 4 seconds, but not more than once every 15 seconds), and never more than once every 2 seconds. Discord's limits are strict, so nothing extra is sent. If Discord rejects a status outright, a simplified one without images and buttons goes out. When a track briefly disappears (players lose the title for a moment when switching), the status isn't removed right away but waits 4 seconds.

The Discord connection is careful. The program tries channels 0 to 9 and waits at most 8 seconds for an answer, because right after Windows starts Discord creates the channel before it starts answering on it, and the library would wait forever. No connection means a retry every 10 seconds. A wrong Client ID is checked again after 30 seconds, or immediately if you fixed it in the settings.

"Listen together" uses two topics on the MQTT server: the host writes to one (the message is retained on the server, so a guest who joins later sees the track at once) and the guests write to the other. The host sends its state on changes and every 10 seconds, a guest reports in every 20. A state older than 35 seconds counts as stale. If the host loses internet, the server itself notifies the guests (MQTT last will). Messages whose time differs from local time by more than 10 minutes are dropped. The host's position is recomputed on the guest's side, so the bar moves smoothly.

Three threads: the main one (tkinter, the settings window), the tray (`pystray`) and the worker (`asyncio`, all polling and talking to Discord). A second copy can't be started, the program takes the mutex `Local\MusicRPC_singleton`.

## Files

```
music_rpc.pyw          entry point, no console; writes crash.log on failure
install.bat            environment and dependencies
start.bat              launch without a console
diagnose.bat           diagnostics
build_exe.bat          builds MusicRPC.exe
requirements.txt
assets/                icon (app.png, app.ico)
docs/                  screenshots
tests/                 automated tests
ymrpc/
  app.py               startup, logging, --diagnose
  config.py            settings and the data folder
  media.py             reading the Windows media centre, controlling the player
  covers.py            cover and link lookup
  textmatch.py         comparing titles
  rpc.py               building the status and the worker loop
  together.py          "listen together"
  history.py           recent tracks
  status.py            states and icon colours
  tray.py              tray icon and menu
  ui.py                settings window
  icon.py              the icon (drawn in code)
  autostart.py         autostart
```

The name in the tray and windows is changed in one line: `DISPLAY_NAME` in `ymrpc/__init__.py`. The icon is drawn by `ymrpc/icon.py`, and `python -m ymrpc.icon` regenerates the files in `assets`.

## Data and privacy

Everything lives in `%APPDATA%\MusicRPC`: `config.json`, `app.log` (up to 512 KB, two older copies) and `crash.log` if the program couldn't start. Settings from earlier versions (`%APPDATA%\YandexMusicRPC`) are picked up automatically.

Discord gets what you see in the status. Yandex Music, Deezer and iTunes get a search query "artist title", and only for the cover: if you turn off both the cover and the button, there are no requests. The exception is a host in "listen together" mode, which looks up the cover and link anyway to hand them to the guests. While "listen together" is on, encrypted messages also go to the MQTT server and your IP is visible. There's no analytics, no telemetry and no accounts.

## When something doesn't work

First close the program in the tray and run `diagnose.bat`. It shows which players Windows sees, checks the connection to Discord (it sends a test status for 6 seconds) and checks the servers for "listen together".

- Red icon "Укажите Client ID" (Enter a Client ID): the field in the settings is empty.
- "Неверный Client ID" (Invalid Client ID): Discord doesn't know that application. Copy the Application ID, not the public key.
- "Discord не найден" (Discord not found): it isn't running, it's open in a browser tab, or it runs with different rights (for example only one of the two "as administrator"). Restart both as a normal user.
- "Discord не отвечает" (Discord not responding): normal in the first seconds after Windows starts, the program reconnects by itself. If it doesn't pass, a full Discord restart helps. Details are in `app.log`.
- Grey icon while music plays: the player isn't visible or doesn't match `app_filters`. Click "Проверить" in the settings.
- The player isn't in the list at all: it doesn't tell Windows about playback. In Yandex Music, for example, turning off crossfade helps.
- A status but no cover: the track wasn't found in the catalogues, or there's no network. Normal for rare tracks.
- You can't see the button in your own profile: that's how Discord works, only other people see buttons.
- The progress bar jumps: the player reports its position badly. Turn the bar off.
- A YouTube video shows up in the status: a browser is in `app_filters`.
- "Слушать вместе: нет связи с сервером" (no connection to the server): the public servers are unreachable (port 1883 may be closed by your ISP or firewall). Check `diagnose.bat`, try a VPN or set your own server.
- "Слушать вместе: жду хоста…" (waiting for the host): the host isn't running, nothing is playing on their side, the code was entered wrongly, or the computers' clocks differ by more than 10 minutes.
- The guest's player doesn't react: control only works if the same track is playing on your side (title and artist are compared). Open the track from the host's link. Not every player accepts seeking, and a refusal leaves a line in `app.log`.

If none of that helps, send the last 30 lines of `app.log` and the output of `diagnose.bat`.

## Automated tests

The `tests` folder has about ninety tests. They need no Windows, Discord or internet: the media centre, Discord and the MQTT server are replaced with fakes.

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

Some more tests run when the system allows: ones with the real `pypresence` library against a fake Discord (not on Windows), with a real Mosquitto (if it's on `PATH`), and ones for the settings window and tray menu (they need a display; on Linux use `xvfb-run`). The suite passes on Python 3.9-3.14 and 3.15 alpha, with both `paho-mqtt` 1.x and 2.x.

## Credits and disclaimer

Thanks to the authors of [pypresence](https://github.com/qwertyquerty/pypresence), [pystray](https://github.com/moses-palmer/pystray), [pywinrt](https://github.com/pywinrt/pywinrt), [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) and [Pillow](https://python-pillow.org/).

The project is unofficial and isn't affiliated with Discord, Yandex, Spotify, Deezer, Apple, HiveMQ or EMQ. All names belong to their owners and are mentioned only to describe compatibility.

## License

[MIT](LICENSE).
