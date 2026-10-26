"""HTML for the one-time secret form.

The page is sent with ``default-src 'none'`` and ``style-src 'unsafe-inline'``.
It must not load a script, a font, an image, or any other external resource.
"""

from html import escape

_DOCUMENT = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex">
<title>\x00TITLE\x00</title>
<style>
:root {
  color-scheme: light;
  --paper: #efeae2;
  --glow: #fbf8f4;
  --sheet: #f7f4ef;
  --ink: #1c1917;
  --soft: #4a453e;
  --line: #ddd4c8;
  --line-strong: #b7aa9a;
  --accent: #7a3e2e;
  --done: #1f4d3a;
  --focus: #1f4d3a;
  --notice: #f3ebe3;
  --field: #fffcf8;
  --button: #1c1917;
  --button-ink: #f7f4ef;
  --button-hover: #3a332c;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --paper: #12100e;
    --glow: #241f1b;
    --sheet: #1c1916;
    --ink: #f4efe8;
    --soft: #c4b8aa;
    --line: #3a342c;
    --line-strong: #6b5e52;
    --accent: #e4b08a;
    --done: #8fbfa8;
    --focus: #e4d2b0;
    --notice: #2a221c;
    --field: #14110f;
    --button: #f4efe8;
    --button-ink: #1c1916;
    --button-hover: #e4d7c8;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  min-height: 100vh;
  background-color: var(--paper);
  background-image: radial-gradient(ellipse 80% 50% at 50% -10%, var(--glow), transparent 62%);
  color: var(--ink);
  font-family: "Avenir Next", "Segoe UI", system-ui, "Helvetica Neue", "Noto Sans", sans-serif;
  font-size: 16px;
  line-height: 1.5;
}
.sheet {
  width: min(32rem, 100%);
  margin: 8vh auto 3rem;
  padding: 2.5rem 2.25rem 2.15rem;
  background: var(--sheet);
  border: 1px solid var(--line);
  box-shadow: 0 24px 48px -36px rgba(40, 28, 16, .55);
}
.mark {
  margin: 0;
  font-size: .68rem;
  font-weight: 600;
  letter-spacing: .28em;
  text-transform: uppercase;
  color: var(--accent);
}
.rule {
  width: 3.25rem;
  height: 1px;
  margin: .85rem 0 1.6rem;
  background: var(--line-strong);
}
.tone-done,
.tone-stop {
  margin-top: max(8vh, calc(50vh - 11rem));
  margin-bottom: max(3rem, calc(50vh - 11rem));
}
.tone-done .rule { background: var(--done); height: 2px; }
.tone-stop .rule { background: var(--accent); height: 2px; }
h1 {
  margin: 0;
  font-family: "Iowan Old Style", Palatino, "Palatino Linotype", Georgia, serif;
  font-size: 2rem;
  font-weight: 500;
  letter-spacing: -.02em;
  line-height: 1.15;
}
.variant { margin: .45rem 0 0; color: var(--soft); font-size: 1.05rem; }
.account { margin: 1.15rem 0 0; color: var(--soft); font-size: .92rem; }
.account .label {
  margin-right: .35rem;
  font-size: .72rem;
  font-weight: 600;
  letter-spacing: .14em;
  text-transform: uppercase;
}
code {
  font-family: ui-monospace, "SF Mono", "Segoe UI Mono", Menlo, Consolas, monospace;
  font-size: .92em;
}
.lede { margin: 1.15rem 0 0; max-width: 38em; }
.notice {
  margin: 1.5rem 0 0;
  padding: .85rem 1rem;
  background: var(--notice);
  border-left: 3px solid var(--accent);
}
form { margin: 0; }
fieldset {
  border: 0;
  margin: 1.85rem 0 0;
  padding: 0;
  min-inline-size: 0;
}
legend {
  padding: 0;
  margin: 0;
  font-size: .72rem;
  font-weight: 600;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--soft);
}
.field { margin-top: 1.15rem; }
label { display: block; }
.hint { display: block; }
.name {
  display: block;
  margin-top: .12rem;
  color: var(--soft);
  font-family: ui-monospace, "SF Mono", "Segoe UI Mono", Menlo, Consolas, monospace;
  font-size: .75rem;
}
input {
  display: block;
  width: 100%;
  margin-top: .45rem;
  padding: .7rem .8rem;
  border: 1px solid var(--line-strong);
  border-radius: 2px;
  background: var(--field);
  color: var(--ink);
  font: inherit;
  font-size: 1rem;
}
input[readonly] {
  margin-top: .2rem;
  padding: .35rem 0 .5rem;
  border: 0;
  border-bottom: 1px solid var(--line);
  border-radius: 0;
  background: transparent;
}
input:focus {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
input:-webkit-autofill {
  -webkit-text-fill-color: var(--ink);
  box-shadow: 0 0 0 1000px var(--field) inset;
}
button {
  margin-top: 1.85rem;
  min-height: 2.75rem;
  padding: .7rem 1.45rem;
  border: 0;
  border-radius: 2px;
  background: var(--button);
  color: var(--button-ink);
  font: inherit;
  letter-spacing: .04em;
  cursor: pointer;
}
button:hover { background: var(--button-hover); }
button:focus {
  outline: 2px solid var(--focus);
  outline-offset: 3px;
}
.footnote { margin: .85rem 0 0; color: var(--soft); font-size: .85rem; }
.message {
  margin: 0;
  font-family: "Iowan Old Style", Palatino, "Palatino Linotype", Georgia, serif;
  font-size: 1.45rem;
  font-weight: 500;
  line-height: 1.35;
}
@media (max-width: 40rem) {
  body { background-image: none; }
  .sheet {
    margin: 0;
    min-height: 100vh;
    border: 0;
    box-shadow: none;
    padding: 1.75rem 1.25rem 2.5rem;
  }
  button { width: 100%; }
}
</style>
</head>
<body>
<main class="sheet \x00TONE\x00">
<p class="mark">Keystone</p>
<div class="rule"></div>
\x00BODY\x00
</main>
</body>
</html>
"""

_LEDE = {
    "create": (
        "Enter the secrets for this account. The service checks them before "
        "anything is stored. This link works once."
    ),
    "rotate": (
        "Enter these secrets again. The ones already stored stay until the "
        "service accepts the new values. This link works once."
    ),
}


def render_page(body: str, *, title: str, tone: str) -> str:
    """Wrap ``body`` in the credential page. ``body`` is already HTML."""
    if tone not in {"tone-entry", "tone-done", "tone-stop"}:
        raise ValueError("unknown page tone")
    return (
        _DOCUMENT.replace("\x00TITLE\x00", escape(title), 1)
        .replace("\x00TONE\x00", tone, 1)
        .replace("\x00BODY\x00", body, 1)
    )


def render_message(text: str) -> str:
    """One sentence for a page that is not the form. ``text`` is already escaped."""
    return f'<p class="message">{text}</p>'


def render_form(
    *,
    provider_title: str,
    variant_title: str,
    profile_id: str,
    purpose: str,
    token: str,
    csrf: str,
    readonly: list[tuple[str, str, str]],
    secrets: list[tuple[str, str, int]],
    notice: str | None = None,
) -> str:
    """The entry form. Secret inputs stay ``type=password`` and ``autocomplete=off``."""
    parts = [
        f"<h1>{escape(provider_title)}</h1>",
        f'<p class="variant">{escape(variant_title)}</p>',
        (
            '<p class="account"><span class="label">Account</span> '
            f"<code>{escape(profile_id)}</code></p>"
        ),
        f'<p class="lede">{escape(_LEDE.get(purpose, _LEDE["create"]))}</p>',
    ]
    if notice:
        parts.append(f'<p class="notice">{escape(notice)}</p>')
    fields_html = []
    if readonly:
        rows = [_readonly_field(name, value, hint) for name, value, hint in readonly]
        fields_html.append(
            "<fieldset><legend>Already set</legend>"
            + "".join(rows)
            + '<p class="footnote">These cannot be changed on this page.</p></fieldset>'
        )
    secret_rows = [_secret_field(name, hint, max_length) for name, hint, max_length in secrets]
    fields_html.append("<fieldset><legend>Secrets</legend>" + "".join(secret_rows) + "</fieldset>")
    action = escape(f"/creds/{token}")
    parts.append(
        f'<form method="post" action="{action}" autocomplete="off">'
        f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
        + "".join(fields_html)
        + '<button type="submit">Save</button></form>'
    )
    return "".join(parts)


def _readonly_field(name: str, value: str, hint: str) -> str:
    field_id = f"field-{name}"
    return (
        '<div class="field">'
        f'<label for="{escape(field_id)}">'
        f'<span class="hint">{escape(hint)}</span>'
        f'<span class="name">{escape(name)}</span>'
        "</label>"
        f'<input id="{escape(field_id)}" type="text" value="{escape(value)}" '
        'readonly spellcheck="false" tabindex="-1">'
        "</div>"
    )


def _secret_field(name: str, hint: str, max_length: int) -> str:
    field_id = f"field-{name}"
    return (
        '<div class="field">'
        f'<label for="{escape(field_id)}">'
        f'<span class="hint">{escape(hint)}</span>'
        f'<span class="name">{escape(name)}</span>'
        "</label>"
        f'<input id="{escape(field_id)}" type="password" name="{escape(name)}" '
        f'maxlength="{max_length}" autocomplete="off" spellcheck="false" '
        'autocapitalize="off" autocorrect="off" required>'
        "</div>"
    )
