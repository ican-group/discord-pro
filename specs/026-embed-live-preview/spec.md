# 026 — Embed Code Live Preview: stylesheets and live bot test

Extends the Embed Code page (spec 016). The Live Preview becomes a place to try a stylesheet and to
chat with the selected bot, including its GridScan Agent lookups (spec 024), without changing the
published embed.

## Preview mode

- The preview iframe loads the widget with `&preview=1`. The published embed and its snippets never
  include this parameter.
- **Session:** each preview load starts a new conversation that is kept in memory only. It doesn't
  read or write the widget's stored session, so it never resumes or replaces a visitor's chat.
  Follow-ups within one preview load use the same session, so the bot keeps their context.
- **Example exchange:** an empty preview shows "Example question" / "Example bot reply" so styles
  can be judged. It isn't sent or copyable.
- **Lookup footer:** when a reply used GridScan Agent, the preview shows it under the reply. For
  example "GridScan Agent · 1 API call", "· some calls failed", "no live lookup" or "unavailable".
  The published widget doesn't show this.

## Stylesheets in the preview

- **Stylesheet URL → Preview URL:** loads an unsaved direct HTTPS stylesheet into the preview only.
  - Reports "Stylesheet loaded in Live Preview.", or "Couldn't load the stylesheet. Check that the
    URL serves a CSS file." on a load error (such as a 404).
  - URLs that aren't plain HTTPS are refused before loading.
- **Import CSS for preview:** a local `.css` file up to 256 KB is applied to the preview only. It
  isn't uploaded or saved.
- **Use saved stylesheet** returns the preview to the saved one. Saving a URL also does this.
- The preview applies the saved stylesheet itself through the same channel, so only one stylesheet
  is active at a time.
- **Messages:** page and preview exchange `postMessage` messages restricted to the site's own
  origin and the preview frame.
- **Published embed unchanged:** `/widget` and the generated snippets apply only the saved
  stylesheet, as before.

## API

`POST /api/chat/send` adds `lookup` when GridScan Agent handled the message:
`{"provider": "GridScan Agent", "status": "ok" | "partial" | "no_call" | "unavailable", "calls": <n>}`.

- Web chat only, and only when the agent connection is in use.
- No arguments, addresses or hashes.
- Responses are otherwise unchanged.
