---
name: tv
description: Work on a remote PC or Android device through the TeamViewer web client: connect (finding an open session or asking for an ID and the current password), send a script or file, run it, fetch the result back, and hand screen navigation to the tv-driver agent. Use when asked to connect to a TeamViewer ID, do something on a remote PC or tablet, copy files to or from one, or "tv <goal>".
argument-hint: <goal, e.g. "koble til 895 833 413 og send swap.ps1" or "open the Settings app">
---

Goal: $ARGUMENTS

You run in the main conversation and drive the session through the `teamviewer` gist in `mcp__repld__exec`. Do
exact steps yourself (connect, files, one typed command); give open-ended screen navigation to the **tv-driver**
agent so its screenshots stay out of this context.

## 1. Find or open the session

```python
import teamviewer
await teamviewer.sessions()          # [{id, target}]: sessions already open in the browser
```

- One open, and it is the device the goal means: `dev = await teamviewer.connect(id)` reuses it, no password.
- Several open: ask which (AskUserQuestion, one option per ID).
- None: ask for the TeamViewer ID and the password shown on the device's screen now, in one AskUserQuestion.
  Offer `await teamviewer.recent()` (`connectId`, newest first) as options; the user picks one or types a new ID.
  Then `dev = await teamviewer.connect(id, password=pw)`. It waits ~20 s for the web client to start; that is
  normal.

Passwords are one-time (QuickSupport shows a new one after each session): ask every time, never store or reuse
one. `RuntimeError: the partner refused the password` means it went stale: ask for the current one, do not retry.
"signed out" means the user must log in to web.teamviewer.com in that Chrome window. No browser at all means the
debug Chrome is not running.

A free licence ends a session to a branded QuickSupport after 5 minutes: plan the work to fit, or reconnect.

## 2. Files and commands (Windows partner)

```python
await dev.send_file(path)                   # lands in %USERPROFILE%\Downloads; answers "overwrite?" itself
await dev.key("Meta+r")                     # the Run box
await dev.type(r'powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\Downloads\x.ps1"')
shot = await dev.screenshot(1600)           # check the Run box BEFORE Enter
await dev.key("Enter")
data = await dev.receive(r"C:\Users\<user>\Downloads\x.log")   # -> bytes
```

- **Look at the Run box before Enter.** It opens on its last command, selected; on one PC that was
  `shutdown /r /f /t 1`. Typing replaces it, so check the screenshot shows your command before pressing Enter.
- The Run box expands `%USERPROFILE%`; `$env:` only works inside PowerShell.
- `type` is ASCII-only on Windows. Build other characters in PowerShell, e.g. `[char]0xF8` for ø.
- PowerShell 5.1's `Out-File` writes UTF-16LE: decode received text with `data.decode("utf-16")`, or have the
  script write `-Encoding utf8`.
- Find the user name for a receive path with a script that prints `$env:USERPROFILE`, or read it off the screen.
- **One cell at a time on a Device.** A cell that runs past 2 s goes to the background; wait for its
  `task_done` before the next cell touches `dev`, or a click lands mid-`type` and keystrokes are lost.
- `await dev.info()` (route, display, partner version) and `await dev.dashboard()` (a Windows partner's
  hardware and health panel) are read-only and cheap.

Anything that changes the remote machine (running a script, replacing a file, closing a program) is done only when
the user's goal asks for it. Say what you are about to run before the Enter.

## 3. Screen navigation

For "find X on screen", clicking through dialogs, reading something only visible on the remote screen, spawn the
agent:

```
Agent(subagent_type="tv-driver", description="<3-5 words>",
      prompt="<the goal, what the screen should show at the end, and what it may not do>")
```

It navigates only (no writes, no installs, no settings) and returns a text report with screenshot paths. Read a
path yourself only when its one-line description is not enough. For one click or one key, act yourself:
`await dev.screenshot()` then `await dev.click(x, y)` (pixels of that screenshot).

## 4. End

Say what was done and what the remote screen shows now. Close the session only when asked:
`await dev.tab.close()`.
