---
name: tv
description: Carry out a goal on the remote TeamViewer screen (an Android device or a Windows PC) with a cheap model and return a short text verdict, keeping screenshots out of this conversation. Use for navigating the remote device once a session is open (teamviewer.connect).
argument-hint: <goal, e.g. "open the Settings app">
context: fork
agent: tv-driver
model: haiku
---

Goal: $ARGUMENTS

Follow your rules and finish with the report block.
