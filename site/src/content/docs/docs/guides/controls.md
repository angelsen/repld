---
title: Controls guide
description: Expose your app's UI state as a typed remote API the agent can discover, invoke, and observe.
---

Controls expose your app's UI state machines as typed, invocable APIs. Instead of clicking through forms, the agent calls `auth.login()`, reads `thread.threads`, invokes `ai.accept()`. Every action records its state diff and pushes an observation to repld's channel.

## The protocol

Your app exposes a `window.controls` object with three methods:

| Method | Returns | Purpose |
|--------|---------|---------|
| `describeAll()` | `Record<string, ControlDescription>` | Schema for all controls — actions, params, properties, current state |
| `invoke(control, action, args)` | `{returned, stateBefore, stateAfter, duration}` | Execute an action with state diffing |
| `list()` | `[{name, title, state}, ...]` | Quick summary of registered controls |

repld auto-detects `window.controls` on attached tabs and exposes two MCP tools: `browser_controls` (discovery) and `browser_invoke` (invocation with full observation pipeline).

> **Note:** [WebMCP](https://webmachinelearning.github.io/webmcp/) is a W3C draft standardizing the same pattern — `document.modelContext.registerTool()` with a nearly identical shape (`name`, `description`, `inputSchema`, `execute`). Controls work today via CDP and add state diffing and observation telemetry; if WebMCP ships in browsers, the two could be bridged with a thin adapter.

## Defining a control

A control has a name, actions with typed parameters, and observable properties:

```typescript
import { defineControl } from './controls/core/define';

const { control, def } = defineControl({
  name: 'auth',
  title: 'Auth',
  description: 'User authentication and session',

  // state() returns a string summarizing current state
  state: () => currentUser ? currentUser.email : 'anonymous',

  actions: {
    login: {
      description: 'Login with email and password',
      params: {
        type: 'object',
        properties: {
          email: { type: 'string' },
          password: { type: 'string' },
        },
        required: ['email', 'password'],
      },
      execute: async (args) => {
        await signIn(args.email, args.password);
        return { ok: true };
      },
    },
    logout: {
      description: 'Log out current user',
      params: {},
      execute: async () => {
        await signOut();
        return { ok: true };
      },
    },
  },

  properties: {
    userId: { description: 'Current user ID', get: () => currentUser?.id ?? null },
    userName: { description: 'Display name', get: () => currentUser?.name ?? null },
  },
});
```

Register it in a global registry and expose on `window`:

```typescript
import { ControlRegistry } from './controls/core/registry';

const registry = new ControlRegistry();
registry.register(control, def);
window.controls = registry;
```

### What `defineControl()` creates

The factory builds a runtime object with:
- **Data properties:** `name`, `title`, `description`
- **Computed `state`:** calls your `state()` getter
- **Property getters:** each calls the corresponding `get()` function
- **Action methods:** each validates params, executes, emits an observation

Required params are validated before execution. Unknown params are rejected (catches typos). Async actions are awaited before the observation is emitted.

## Discovery from repld

The `browser_controls` MCP tool calls `window.controls.describeAll()`:

```python
# MCP tool call
browser_controls(target="9222:a1b2c3")
```

Returns the full schema for every registered control:

```json
{
  "auth": {
    "name": "auth",
    "title": "Auth",
    "description": "User authentication and session",
    "state": "anonymous",
    "actions": {
      "login": {
        "description": "Login with email and password",
        "params": {
          "type": "object",
          "properties": {
            "email": { "type": "string" },
            "password": { "type": "string" }
          },
          "required": ["email", "password"]
        }
      },
      "logout": {
        "description": "Log out current user",
        "params": {}
      }
    },
    "properties": {
      "userId": { "description": "Current user ID", "value": null },
      "userName": { "description": "Display name", "value": null }
    }
  }
}
```

From Python (inside repld exec):

```python
tab = await browser.get("*localhost:3000*")
schema = await tab.controls()
# schema["auth"]["actions"]["login"]["params"]
```

## Invocation from repld

The `browser_invoke` MCP tool runs the full observation pipeline — settle, accessibility tree, network delta, console delta:

```python
# MCP tool call
browser_invoke(target="9222:a1b2c3", control="auth", action="login",
               args={"email": "user@example.com", "password": "secret"})
```

Returns the action result plus state diff:

```json
{
  "returned": { "ok": true },
  "stateBefore": "anonymous",
  "stateAfter": "user@example.com",
  "duration": 342
}
```

Plus the observation text (tree + network + console delta), same as any browser mutation.

From Python:

```python
result = await tab.invoke("auth", "login", {"email": "user@example.com", "password": "secret"})
# result["returned"], result["stateBefore"], result["stateAfter"], result["duration"]
```

## Observations

Every action automatically emits a structured observation. The default sink is `console.debug`:

```
console.debug("__controls__", JSON.stringify({
  type: "observation",
  source: "controls",
  control: "auth",
  event: "action",
  action: "login",
  args: { email: "user@example.com" },
  result: { ok: true },
  stateBefore: "anonymous",
  stateAfter: "user@example.com",
  duration: 342,
  summary: "auth.login(email: \"user@example.com\")"
}))
```

repld intercepts this in the CDP event stream and pushes a channel notification:

```
⚡ channel · [controls] auth.login(email: "user@example.com") — state: "anonymous" → "user@example.com" (342ms)
```

### Custom sink

Replace the default sink if you need to route observations elsewhere:

```typescript
import { setObservationSink } from './controls/core/observe';

setObservationSink((obs) => {
  // send to your own telemetry, logging, etc.
  myLogger.info(obs.summary, obs);
  // still emit to console for repld
  console.debug('__controls__', JSON.stringify(obs));
});
```

## The diagnostic loop

Controls + channel notifications give you an end-to-end feedback loop:

```python
# 1. act via controls
browser_invoke(target, "thread", "send", {"message": "test"})

# 2. channel notifications arrive in real-time:
#    [controls] thread.send(message: "test") — 201 (142ms)
#    [ai] classify: reply (0.92)
#    [ai] posted suggestion in branch 6107

# 3. verify via controls
browser_invoke(target, "ai", "count")
#    → 1
```

Multiple notifications give you a causal chain with timestamps:

```
09:25:34  [controls]  thread.send → 201
09:25:37  [ai]        classify: reply (0.92)
09:25:38  [error]     API 400: oneOf not supported
09:25:38  [ai]        no suggestion posted
```

You don't search logs — the cause-effect is obvious. Errors arrive the instant they happen, before you notice the page is broken.

## What's next

- [Browser guide](/repld/docs/guides/browser/) — CDP integration, tab API, observe pipeline
- [Gists guide](/repld/docs/guides/gists/) — turn browser patterns into reusable modules
