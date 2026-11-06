# Choosing the model a task runs on

Three places can say which model a task uses. The first one that speaks wins:

1. **The task itself** — a line `/model <id>;` on its own within the first ten
   lines of the prompt:

   ```
   /model claude-opus-5-5;

   Refactor the importer so that …
   ```

   `<id>` is a model id (`claude-opus-5-5`, `claude-sonnet-5-5`) or a Claude Code
   alias (`opus`, `sonnet`, `haiku`). The trailing `;` is optional. The runner
   removes the line (and one blank line after it) before the prompt reaches the
   model.
2. **The account default** — one line in `config/.default_model` (the hosted
   platform writes it from Setup → Default model).
3. **Neither** — Claude Code picks its own default for the account's plan.

The runner passes the chosen id to the CLI as `--model <id>` and records it on
the task (`model:` in the task file's front matter; `Model:` in `task_status`).
A resumed task (after a credit hold) gets the same model again.

## Why the runner does this, and not a slash command

Until 2026-11-06 the runner left the `/model` line in the prompt and relied on
Claude Code to act on it. Headless `claude -p` does not run slash commands: the
prompt is only words to it, and the runner's own context comes before the task
text anyway. Every task therefore ran on the CLI's default model, whatever it
asked for, and the platform's "default model" setting did nothing. `--model` is
the only way to choose a model for a headless run.

## What can go wrong

- **An id the account cannot use** (a typo, a model the plan does not include):
  the CLI exits with an error and the task ends `failed` with that error as its
  result. Nothing falls back silently to another model.
- **A line that is not a directive** — `/model` followed by anything that is not
  a single id (`/model a b`, `/model --flag`), or a `/model` line after the
  tenth line — is left in the prompt as ordinary text and chooses nothing.
- **A dearer model uses the plan's allowance faster.** On a subscription token
  the usage limits are shared by every task on that Claude account; see
  [notifications.md](notifications.md) for the alert when a limit is hit.

To see what a finished task really ran on, read the first line of its log
(`logs/<task_id>.log`): the `init` event carries `"model"`.
