# Known limitations — v0.3.0-alpha.1

## Platform

- **Windows 10/11, 64-bit only.** No macOS.
- **FL Studio 2026 (version 26) or newer.** Setup stops on older versions.
- Close FL Studio before you run Setup: FL rewrites its MIDI settings when it exits.

## Install

- The installer is **not code-signed** yet. SmartScreen warns: *More info → Run anyway*.
  Check the SHA-256 on the release page if you want to be sure the file is ours.
- loopMIDI (a free virtual MIDI port by Tobias Erichsen) is needed. Setup offers to install it;
  this asks for administrator rights once.
- Setup goes on only when the model you picked answers. No internet or a wrong key → Setup waits.

## Window and language

- The window speaks **English or Russian** (Settings → Window language; by default, as in Windows).
  Requests and answers work in both.
- The first voice request downloads the speech model (~240 MB).

## Sample library

- The sample folder must be on an **NTFS** drive. USB sticks (FAT/exFAT) and network drives do not work:
  Sessionmate sorts the library with hard links, and these drives have none.
- After the first sort, restart FL once so its browser shows the sorted tree.

## What it cannot do yet

- Edit the Playlist arrangement (clips on tracks).
- Render or export audio.
- Load third-party plugins into channels or mixer slots.
- Some edits inside FL have no FL undo when made from a script (step sequencer).
  Use "undo" in the Sessionmate chat for those.

## Models

- **Ollama** needs 16 GB RAM and a GPU with 8 GB; on weaker PCs use a cloud model.
  The first answer can take up to 2 minutes while the model loads.
- **ChatGPT and Gemini** do not work from some countries (Belarus, Russia and others not on
  the providers' lists). Z.ai, Groq and Ollama are the free options.
- Free tiers have rate limits: when you hit one, wait a minute.

## Reporting

- Sessionmate sends nothing by itself. To report a bug, collect the report in the window
  (*Settings → Diagnostics → Collect report*) and attach it to an issue. The Windows user name in paths
  is replaced with `%USERPROFILE%`; sample names you rated stay in the file.
