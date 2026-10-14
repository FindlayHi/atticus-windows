<p align="center"><img src="docs/banner-windows.png" alt="Atticus. Your whole PC, one sentence away." width="100%"></p>

<p align="center">
  Hold <kbd>Ctrl</kbd> <kbd>Alt</kbd> <kbd>Space</kbd>, say what you need, let go.<br>
  Atticus moves your windows, finds your files, reads your screen and gets it done.
</p>

<p align="center">
  <b>⚡ Fast</b> &nbsp;·&nbsp; <b>🔑 No API key</b> &nbsp;·&nbsp; <b>🧠 Remembers you</b> &nbsp;·&nbsp; <b>🔌 Integrations</b> &nbsp;·&nbsp; <b>Free and open source</b>
</p>

- **Fast.** Simple asks skip the AI: an app opens in a blink. Ask for an action once and Atticus learns it, so next time it runs with no AI and for free.
- **No API key.** It runs on the Claude Pro or Max plan you already have, through Claude Code. Nothing to sign up for, no key to paste, no extra bill.
- **Remembers you.** Say "remember…" and every ask after that knows it. It picks up who you are (your name, your apps) and fixes its own mistakes when you correct it.
- **Integrations.** Jellyfin, Home Assistant, Immich, the arr stack and more, by voice. Or add your own in a minute, no code.

<p align="center">
  <img alt="Windows" src="https://img.shields.io/badge/Windows_11-1d1d1f?style=for-the-badge&logo=windows11&logoColor=white">
  <img alt="C#" src="https://img.shields.io/badge/.NET_10-1d1d1f?style=for-the-badge&logo=dotnet&logoColor=white">
  <img alt="Python" src="https://img.shields.io/badge/Python-1d1d1f?style=for-the-badge&logo=python&logoColor=white">
  <img alt="Claude" src="https://img.shields.io/badge/Claude_Haiku-1d1d1f?style=for-the-badge&logo=anthropic&logoColor=white">
  <img alt="Cost" src="https://img.shields.io/badge/~0.1%C2%A2_per_ask-1d1d1f?style=for-the-badge">
</p>

<p align="center">A Windows port of <a href="https://github.com/SaarthurR/atticus-mac">Atticus for Mac</a> by SaarthurR. Same brain, same box, same idea.</p>

<br>

## Just ask

| You say | Atticus |
|---|---|
| *"Code on my big screen, Chrome and Terminal on the laptop."* | Arranges three windows across two displays. |
| *"Open Spotify."* · *"Notepad on the left."* | Runs instantly, with no AI at all. |
| *"Find my notes about the Tokyo trip."* | Searches your files by what's inside them and opens the match. If several fit, you pick from previews. |
| *"What's wrong with this code?"* | Looks at your screen and answers in a line. |
| *"Reply: sounds good, see you at 7."* | Types it into the app you're in. It never sends. |
| *"Make this friendlier."* (with text highlighted) | Rewrites it. You review, then Replace, Try again or Copy. |
| *"Message Sam on Discord: running 10 minutes late."* | Opens the chat and types it. You Allow the send. |
| *"In Spotify, turn on shuffle."* | Finds the button and presses it. Your mouse stays put. |
| *"Fill in this form with my name and pick Blue."* | Reads the page in Chrome or Edge and fills it in. A click on Send or Buy asks first. |
| *"What should I keep watching?"* | Checks Jellyfin and picks up where you left off. |
| *"Is there motion in the office?"* | Asks Home Assistant. |
| *"When I say focus mode, close Slack and open my to-do list."* | Remembers it. Next time, two words do it all. |

## Why Atticus

<table>
<tr>
<td width="33%" valign="top"><h3>Fast</h3>Opening apps and sites, moving windows, web searches and music skip the AI entirely. Actions you ask for once (opening apps, moving windows, playing music) become learned shortcuts: replayed with no AI and no wait, even said in another order or about another app. Longer tasks save a procedure the AI follows next time.</td>
<td width="33%" valign="top"><h3>No API key</h3>Runs on the Claude plan you already have, through <code>claude -p</code>. About 0.1¢ of plan usage per ask, priced at API list rates. Atticus itself is free.</td>
<td width="33%" valign="top"><h3>Remembers you</h3>"Remember: open means bring it to the front." Saved rules go with every ask. It learns lasting facts about you, and when you correct it, it saves a rule so it gets it right next time. Edit all of it in Settings.</td>
</tr>
<tr>
<td valign="top"><h3>Private ears</h3>Whisper turns your voice into text on your PC. Audio never leaves it.</td>
<td valign="top"><h3>Your whole PC</h3>Apps, windows across every display, files (Windows Search), the clipboard, the web and the screen. Hands inside apps through Windows' own UI Automation: it reads the front window as a compact map, then clicks, opens menu paths ("File > Save as"), scrolls, types, presses keys and drags, mostly with no mouse moves. Anything that sends, buys or deletes waits for your Allow.</td>
<td valign="top"><h3>Safe by default</h3>Before anything deletes, sends, buys, changes a file or runs a system command, Atticus asks you first. Pressing a button named "Send" or "Delete" asks too. <code>rm</code>, <code>del</code>, <code>format</code> and shutdown are always blocked. Deletes go to the Recycle Bin. Every exact command is in the ask log.</td>
</tr>
</table>

<details>
<summary><b>How the shortcut and the box work</b></summary>
<br>

Choose **Hold to talk** or **Press to toggle** in Settings → General → Shortcut behavior. Hold mode sends when you release. Toggle mode starts on the first press and sends on the second; pauses keep listening. Pressing the shortcut while Atticus answers stops it; during spoken playback it stops the voice. Esc cancels listening without sending. Toggle recordings reach a review screen after 60 seconds, rather than sending automatically.

Click any answer to copy it. **Try again** under the latest answer redoes it (and drops a learned shortcut that did the wrong thing). If a task can't finish, a card offers Retry, or Try Sonnet for just that task (Settings → Intelligence). Click your request to edit and ask again, or press ↑ in an empty typing box to recall the latest one. Drag the box anywhere; drag its edges to resize it. It never shows in screen shares, recordings or Atticus's own screenshots.

Your words appear live while you talk. When you let go, Whisper reads the whole recording once more, and that final text is what Atticus acts on: AI tools and learned shortcuts wait for it. Simple commands can run mid-sentence with Instant commands and Act while I'm talking on (Settings → Intelligence).

</details>

## Install

You need:
- **Windows 11** (Windows 10 works too, without the glass look)
- **A paid Claude plan** (Pro or Max) and [Claude Code](https://claude.com/claude-code). No Claude Code yet? The setup window installs it and signs you in.

Paste this in PowerShell:

```
irm https://raw.githubusercontent.com/FindlayHi/atticus-windows/main/install.ps1 | iex
```

It checks your PC (getting Git and Python with winget if they're missing), downloads Atticus to `%USERPROFILE%\.atticus`, builds it, and opens it. No admin needed. Atticus starts at login and updates itself from there: every few hours it pulls the new version, builds it beside the running one, and swaps it in when you're not using it (turn that off in Settings → About & Updates).

The first launch opens a setup window: your Claude plan, the microphone, and the speech model (about 150 MB, once). After that, Atticus lives in the tray. Open it from Start ("Atticus") for Settings: shortcut, sounds, model, speed, screen, memory, integrations and usage.

Working on the code instead? Clone the repo anywhere and run `.\build.ps1` after each change.

Remove: quit Atticus from the tray, then delete `%LOCALAPPDATA%\Programs\Atticus`, `%LOCALAPPDATA%\Atticus`, `%APPDATA%\Atticus` (your settings and history), the Start menu shortcut, and the `Atticus` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

## Integrations

Settings → Integrations lists every service Atticus knows. Fill in the address, click **Where's my key?** to open the right page on your server, paste the key, **Save & Test**.

| Service | Say |
|---|---|
| **Home Assistant** | "Turn off the office lights" · "Is the garage open?" |
| **Jellyfin** | "What should I keep watching?" · "Play Breaking Bad on the living room TV" |
| **Seerr** | "Add Severance season 2" · "What have I requested?" |
| **Sonarr / Radarr** | "What's downloading?" · "When's the next Abbott Elementary?" |
| **Immich** | "Show me photos from the cottage last summer" |
| **Nextcloud** | "Find my lease PDF" · "What's on my calendar this week?" |
| **Paperless** | "Find my car insurance policy" |
| **n8n** | "Run the morning report" (any workflow with a Webhook trigger) |
| **Joplin** | "Note this: call the landlord about the heater" |

A service is only mentioned to the AI when your ask is about it, so each one costs nothing until you use it. Keys stay in Windows Credential Manager.

**Add your own, no code.** Settings → Integrations → Add your own takes any **MCP server** or any app with a **web API**: its address, its key, one line about what it is, and the words that mean it. Claude already knows the APIs of most self-hosted apps, so "Mealie, at http://server:9000" is usually all it needs. Reading runs freely; anything that changes something asks first.

**Or write one.** A Python file in your integrations folder (linked from Settings) loads next to the built-in ones and survives updates. Copy the shape of any file in `integrations/`. Good ones belong in a pull request.

## Web pages

In Chrome or Edge, Atticus reads the page you're on as text with numbered buttons, links and boxes, then clicks or fills them by number (`atticus --web read`, `click 3`, `fill 2 "Sam"`, `wait`). It goes through the browser's DevTools connection, not the mouse, so your cursor and focus stay put, and the numbers stay pinned to the same tab and the same controls through the whole ask. In safe mode, a click on anything that sends, buys, deletes, submits or confirms shows an Allow card first; from a saved shortcut it never clicks those at all.

Setting it up, once per browser: Settings → Screen & Windows → Set up web pages → Chrome or Edge. That opens the browser's own `chrome://inspect/#remote-debugging` (or `edge://…`) page; turn on **Allow remote debugging for this browser instance** (Chrome 144 or later). Your browser isn't restarted and keeps your profile and sign-ins. The first time Atticus reads a page, the browser asks you to Allow it and shows a "being controlled" bar; Atticus keeps that one connection for 10 idle minutes, so you click Allow once per stretch of use. The switch lets any program on your PC ask for the same access, so turn it off in the same place if you stop using this. A browser you start yourself with `--remote-debugging-port=<port>` (on its own profile) works too: put that port in `settings.json` as `"webPort"`. Firefox isn't supported; Atticus uses the screen there.

## Selected text and drafts

Highlight text in any app, then hold the shortcut: Atticus reads the selection (up to 6,000 characters, sent as quoted data, never as instructions) and you can ask about it or have it rewritten. A rewrite shows Replace, Try again and Copy; "delete this" offers Delete. With nothing highlighted but your cursor in a text box, a written reply or email can go right there: Insert, or straight in with Settings → Drafts at my cursor → Insert right away.

It reads with Windows' UI Automation the moment the shortcut goes down (classic Win32 boxes directly), never from password fields. Replace pastes into the same field and checks the text actually landed; your clipboard (text, pictures, files) is put back after, and the draft stays out of clipboard history. If the selection moved or the app won't take it, the box says so and Copy still works. It works in apps that share their text with Windows, which standard text boxes, Notepad, Office and the big browsers do. Apps that draw their own text without accessibility (some games, terminals, older Electron apps) show nothing selected. Turn it off in Settings → Include selected text.

## Works behind you

Unless you ask to open, show or switch to something, Atticus opens pages and apps behind the app you're in and works there (UI Automation clicks don't need the window in front). A live mini screen in the box shows the window it's working in; click it to bring that window forward. A focus guard puts back any window that jumps in front by itself while a task runs. Settings → Screen & Windows: **Work on sites** (in the background or in front) and **Mini screen**. The mini screen is a DWM thumbnail, so it is live, costs nothing, and is hidden from screen sharing along with the box.

## Watch for me

Say "tell me when the download is done" or "watch this page until it says shipped". Atticus sets it up once, then checks every 5 seconds on your PC with no AI, so waiting is free. It reads the window with Windows' own text recognition, so the window must stay open (covered is fine, minimized is not). When it fires you get a notification, and it runs a follow-up if you asked for one. Coding agents like Codex are watched through their own log, with no screen at all. Say "tell me when Claude Code finishes" and Atticus waits for that terminal window to stop changing. Say "stop watching" to cancel.

## Budget

Atticus has to stay fast and nearly free, so new features must fit under these limits:

- Instant commands: no AI, $0.
- AI asks: median under $0.002 and 6 s over the last 50. Check with `python atticus.py --budget`.
- System prompt: under 10,000 characters with every option on. Every ask pays for it, and a test enforces it.
- Integrations: $0 until your ask is about them.
- Anything that waits or runs in the background: $0 while it waits. AI only to set it up and to act at the end.

## Safety, honestly

Safe mode is on by default. On Windows it is the Allow check alone: Claude Code's sandbox, which the Mac version also uses, isn't available on native Windows. Everything that deletes, sends, buys, changes a file, runs a system command, unlocks a door or runs a workflow waits for your Allow; turning safe mode off removes those questions.

## Usage count

This version sends nothing. The daily anonymous user count and the opt-in failure reports (Settings → Account & Access → Help improve Atticus) from the Mac version stay off unless an `ATTICUS_POSTHOG_KEY` is set at build time, and none is.

## Files

- `win/Atticus/`: the app (hotkey, speech, overlay, selected text, setup and Settings windows, tray, updater), in C# with WebView2
- `atticus.py`: the brain (fast commands, system prompt, safety checks), kept close to the Mac version so fixes carry over
- `winctl.py`: Windows' hands (displays, windows, apps, clipboard and typing, screenshots, files, UI Automation)
- `integrations/`: one file per service, plus `_custom.py` for the ones you add in Settings
- `ui/ui.html`, `ui/onboarding.html`, `ui/settings.html`: the overlay, the welcome flow, and Settings
- `ui/web.js`: the script `atticus --web` runs inside the browser tab (from the Mac version)
- `ui/orbs.js`: the [thinking-orbs](https://github.com/Jakubantalik/thinking-orbs) engine (MIT, Jakub Antalik)

## License

MIT. See `LICENSE`. Original Atticus by [SaarthurR](https://github.com/SaarthurR/atticus-mac). `ui/orbs.js` is from [thinking-orbs](https://github.com/Jakubantalik/thinking-orbs), also MIT. Speech by [whisper.cpp](https://github.com/ggerganov/whisper.cpp) through [Whisper.net](https://github.com/sandrohanea/whisper.net), MIT.

## Contributing

Run `python -m unittest discover -s tests -v` and `python atticus.py --selftest` (with `PYTHONUTF8=1`), and `Atticus.exe --selftest` after building; CI runs all three on Windows. Use a scratch `ATTICUS_DIR` so tests stay off your own chat and rules. Handy while working: `ATTICUS_DEMO="<request>"` runs one request at launch without the mic, `ATTICUS_CAPTURABLE=1` lets screenshots see the box, `Atticus.exe --transcribe <wav>` runs a file through speech, and `Atticus.exe --text "<app>"` prints what text recognition reads in a window.

Fixes to the brain or the pages that aren't Windows-specific are worth sending upstream to [atticus-mac](https://github.com/SaarthurR/atticus-mac) too.

### Not on Windows yet

Merged from atticus-mac up to `eba8597` (2026-10-13). These Mac features weren't ported; their files are left out of this fork so a merge never ships them half-working:

- Meeting Copilot (call audio needs WASAPI loopback instead of ScreenCaptureKit)
- Hands extras: the top-right menu bar icons (no system tray equivalent yet); Electron apps that hide their accessibility tree get pictures instead of a map
- Contacts lookup, the Report feedback window, and the Apple speech model picker

When merging upstream again, resolve `atticus.py`, `ui/settings.html` and `ui/onboarding.html` by hand, and `git rm` the Mac-only files.
