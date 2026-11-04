# Requirements profiles: one manifest, more than one shape

`agent-requirements.yaml` in a project repo declares what tasks in that project
need (MCP servers, env vars, credential files). The runner refuses to start a
task while anything declared is unconfigured (`runner.check_requirements`).

Some repos run in more than one shape. The moment-agents repo is the sales
pipeline on one runner account — it needs the production admin credentials and
its MCP servers — and the customer-facing build/edit sandbox on another, where
it must run with **none** of them. Same code, same manifest, different
requirements.

## Tag requirements with the profiles they belong to

```yaml
version: 1
requirements:
  - id: firebase-service-account
    kind: file
    path: credentials/live-service-account.json
    profiles: [pipeline]            # only the pipeline shape needs the admin key
  - id: google-geocoding
    kind: env
    vars: [GOOGLE_GEOCODING_API_KEY]
    profiles: [pipeline, scoped]    # both shapes
  - id: repo-notes
    kind: file
    path: CLAUDE.md                 # no profiles: applies to every shape
```

## Tell the runner which shape a project is

`config/projects.yaml`:

```yaml
projects:
  moment-agents:
    directory: /workspace/moment-agents
    allowed_users: [alice]
    pull_before_task: true
    requirements_profile: scoped
```

On ai-task-runner.com this is the **Requirements profile** field on the
project's card (Projects page); the platform writes it into `projects.yaml`,
which the runner re-reads every tick — no restart.

## Rules

- A requirement **without** `profiles` applies to every project, as before.
- A requirement **with** `profiles` applies only when the project's
  `requirements_profile` is one of them.
- A project with **no** `requirements_profile` sees **every** requirement,
  tagged or not — a repo that has not opted into profiles behaves exactly as
  it did.
- A malformed `profiles` value (not a non-empty list of names) fails the task
  with a message pointing at the manifest. Silently skipping it would make the
  requirement vanish.
