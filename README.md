<p align="center"><img src="assets/logo.png" width="128" height="128" alt="Sessionmate logo"></p>

# Sessionmate™ for FL Studio 2026 — public alpha

Sessionmate is an AI assistant that lives inside FL Studio. Type or say what you need —
it finds sounds in your own sample library, builds drum kits, writes chords and beats,
and checks your mix. It works in FL Studio, not next to it.

<!-- H1: GIF or short video here: request → result in FL -->

> **Alpha.** Expect bugs. Save your project before you try something big.
> Not affiliated with Image-Line. FL Studio is a trademark of Image-Line Software.

## What it does

- **Finds sounds in your library:** "4 dark long kicks", "a snare like this one but brighter".
  Click to listen, drag into the Channel rack or the Playlist.
- **Builds drum kits:** "make a trap kit at 140", "another beat variant", "a different pack".
- **Writes notes:** "chords i-VI-III-VII for 4 bars" — through a piano roll script you accept or undo.
- **Checks the mix:** "mix the drums", "check the mix" — peaks, levels, what to fix.
- **Undo:** "undo" puts back what the last request changed.
- **Voice:** click the microphone and speak. Speech is recognized on your PC.
- **Learns your taste:** rate sounds ✓ / ✗; after 60 ratings the search tunes to you.

Requests work in English and Russian.

## Requirements

- Windows 10 or 11, 64-bit
- FL Studio 2026 (version 26) or newer
- A sample folder on an NTFS drive (not a USB stick, not a network drive)
- A model for requests in words — pick one in Setup:
  - **Free:** Z.ai GLM Flash, Groq, or Ollama (runs on your PC; needs 16 GB RAM and an 8 GB GPU)
  - **Your own key:** Claude, ChatGPT, DeepSeek, Gemini, Qwen, YandexGPT
  - **Claude Code** (if you already use it)

Sessionmate does not ship with a key. You use your own; requests go from your PC
straight to the provider you picked.

## Install

1. Close FL Studio.
2. Download `Setup_Sessionmate_<version>.exe` from [Releases](../../releases).
3. Run it. If Windows SmartScreen says "Windows protected your PC": click **More info → Run anyway**.
   The installer is not code-signed yet. You can check the SHA-256 from the release page first.
4. Windows asks once for administrator rights: that is loopMIDI, the virtual MIDI port Sessionmate uses.
5. Pick a model and click **Check**. Setup goes on only when the model answers.
6. Start FL Studio. Add **Sessionmate** as an effect on the Master track of the Mixer.
7. In its window, click **Choose folder…** and pick your samples.
   Restart FL once after the first sort.

No "Sessionmate" in the plugin list? *Options → Manage plugins → Find installed plugins.*
FL does not react? *Options → MIDI settings:* input `fl-mcp` is on, controller type
"Test Controller", port 77.

To remove it: close FL Studio, then *Windows Settings → Apps → Sessionmate → Uninstall*.
To remove every trace (FL settings it added, data, logs, loopMIDI if Setup installed it):
run **Uninstall Sessionmate (full cleanup)** from the program folder.
Your samples are not touched.

## Known limitations

See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). The short list:

- Windows only, FL Studio 2026 only.
- The window speaks English or Russian (Settings → Window language). Answers come in the language of your request.
- No playlist arrangement editing, no rendering, no loading of third-party plugins yet.

## Report a bug or share an idea

- **Bug:** [open a bug report](../../issues/new?template=bug_report.yml).
  Attach the report file: Sessionmate window → **Settings** → **Diagnostics** → **Collect report**.
- **Idea:** [share an idea](../../issues/new?template=idea.yml).
- **Chat:** [join the Discord](https://discord.gg/psbnWQw7JE) — help with setup, bugs, ideas, new releases.

## Privacy

No accounts, no analytics, no telemetry. Your samples and your voice stay on your PC.
Requests in words go straight to the model provider you picked. We get only what you send us
yourself (a bug report, a message). Details: `PRIVACY.txt` in the program folder.

## License

Sessionmate is proprietary software, free to use during the alpha.
See [EULA.txt](EULA.txt). The source code is not public.
Third-party components and their licenses: `THIRD_PARTY_NOTICES.txt` in the program folder.

"Sessionmate" is a trademark of Nikita Ivlev. A fork or a copy may not use this name.

© 2026 Nikita Ivlev. All rights reserved.
