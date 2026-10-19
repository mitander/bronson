# bronson

A terminal UI for GitHub Actions and read-only GitLab CI.

Bronson shows recent workflow runs or pipelines, their jobs, and completed job logs. It uses `gh`
for GitHub and `glab` for GitLab authentication and API access.

## Requirements

- Python 3.10 or newer
- [GitHub CLI](https://cli.github.com/) (`gh`) for GitHub, or
  [GitLab CLI](https://gitlab.com/gitlab-org/cli) (`glab`) for GitLab
- [pipx](https://pipx.pypa.io/) for an isolated command-line installation

## Install and run

Clone Bronson and install it with its Python dependencies:

```sh
git clone https://github.com/mitander/bronson.git
pipx install ./bronson
```

Ensure pipx's application directory is on your `PATH` (`pipx ensurepath` can set this up). For
GitHub, authenticate with `gh auth login`, then run `bronson` from a checkout. For GitLab, see
[Self-hosted GitLab](#self-hosted-gitlab).

Bronson uses the current repository's `origin` remote. Use `--repo` to choose one explicitly from
any directory:

```sh
bronson --repo owner/repository
```

For a live development checkout, use `pipx install -e ./bronson` instead. Python edits then take
effect on the next launch; reinstall after changing dependencies or package metadata. Uninstall with
`pipx uninstall bronson`. Nothing needs to be added to `PYTHONPATH`.

You can also install with `python -m pip install .` inside a virtual environment. Module startup
(`python -m bronson`) remains supported.

## Self-hosted GitLab

Authenticate with your instance, then run Bronson from a GitLab checkout:

```sh
glab auth login --hostname gitlab.example.com
bronson --provider gitlab
```

Replace `gitlab.example.com` with your instance's hostname. Bronson reads the host and full project
path from `origin`, including nested groups. Outside a checkout, select both explicitly:

```sh
bronson --provider gitlab --host gitlab.example.com --repo group/subgroup/project
```

`--host` accepts a hostname with an optional HTTPS port, not a URL. Configure HTTPS, custom CA
trust, and any instance-specific API settings in `glab`. SSH origin ports do not change the HTTPS
API port. GitHub Enterprise and GitLab installations under a URL subpath are not qualified.

GitHub and `gitlab.com` origins select their provider automatically. Other hosts require
`--provider gitlab`. An explicit `--repo` ignores the current directory's origin. Without `--host`,
`--provider gitlab --repo group/project` selects `gitlab.com`.

GitLab is read-only. Bronson sends only GET requests. Retry, cancel, and manual-job actions are not
available. The same navigation, inline jobs, search, failed-job logs, caching, and refresh behavior
work with pipelines. The list initially shows pipeline names, then job details supply commit titles
and pipeline timing when available. Unknown statuses stay visible. Manual and scheduled jobs remain
waiting, and failed jobs with `allow_failure` are labeled **allowed failure**, not success.

Bronson uses `glab`'s saved login for the selected host. It does not forward `GITLAB_TOKEN`,
`GITLAB_ACCESS_TOKEN`, `OAUTH_TOKEN`, or CI job-token overrides. Sign in with `glab auth login`
instead of relying on ambient tokens. Ambient API-host and protocol overrides are also ignored.
Configure those per host in `glab`. Bronson does not store CLI authentication credentials. Job logs
and dashboard metadata are cached locally, including for private projects, so use a machine and
cache location approved for that data. The GitLab backend needs no GitHub credentials.

Only the latest 100 top-level pipelines are fetched. Job pagination includes current attempts, not
retried jobs. Child pipelines and bridge/trigger jobs are not expanded. Completed traces are shown,
not streamed. Compatibility with a particular self-hosted GitLab version still needs a read-only
smoke test on that instance.

## Themes

Choose a built-in theme with `bronson --theme textual-dark` or the `t` key.

Bronson reads [Flume](https://github.com/mitander/flume.nvim)'s semantic palette exports directly.
Link Flume's `extras` directory:

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/bronson"
ln -s /path/to/flume.nvim/extras "${XDG_CONFIG_HOME:-$HOME/.config}/bronson/flume"
```

Bronson loads `flume/palette/flume-*.json` and follows `flume/current/schema`, including live
`:FlumeSync` changes. After editing palette colors, use Flume's normal compile/sync command and
restart Bronson. No separate Textual export or application rebuild is needed.

If no configuration link exists, Bronson also looks in
`${XDG_DATA_HOME:-$HOME/.local/share}/nvim/lazy/flume.nvim/extras`. Older generated Textual palettes
remain supported through `flume-themes` in the Bronson configuration directory or `tracker-tui`
under extras when canonical palette exports are absent. Missing or invalid palettes leave built-in
themes available.

Use `BRONSON_FLUME_DIR` to point to a different extras directory. The previous
`FLUME_TRACKER_THEME_DIR` and `FLUME_SCHEMA_FILE` overrides remain supported. No external Python
bridge or launcher wrapper is needed.

An explicit `--theme flume-dusk` or selection with `t` pins that theme; choose **Follow Flume sync**
in the theme picker to resume following the active palette.

## Keybindings

### Dashboard

| Key                 | Action                                         |
| ------------------- | ---------------------------------------------- |
| `j` / `k`, arrows   | Move through runs or jobs                      |
| `Tab` / `Shift+Tab` | Switch panes                                   |
| `Enter`             | Expand/collapse a run or open a job log        |
| `Esc`               | Collapse jobs, return to runs, or close a view |
| `[` / `]`           | Narrow / widen the run pane                    |
| `/`                 | Filter the fetched runs                        |
| `g`                 | Show logs for the selected run                 |
| `G`                 | Show failed job logs                           |
| `o`                 | Open the selected run or job in browser        |
| `r`                 | Rerun failed jobs (GitHub only)                |
| `R`                 | Rerun all jobs (GitHub only)                   |
| `c`                 | Cancel a queued or running run (GitHub only)   |
| `Ctrl+r`            | Refresh now                                    |
| `t`                 | Choose a theme                                 |
| `?`                 | Show help                                      |
| `q`                 | Quit                                           |

The run filter matches commit message, branch, workflow, SHA, run ID, and status. It searches only
the runs currently fetched by Bronson.

### Log viewer

| Key                            | Action                             |
| ------------------------------ | ---------------------------------- |
| `j` / `k`                      | Scroll down or up                  |
| `d` / `u`, `Ctrl+d` / `Ctrl+u` | Page down or up                    |
| `h` / `l`                      | Scroll left or right               |
| `g` / `G`                      | Go to the top or bottom            |
| `/`                            | Search the log                     |
| `n` / `N`                      | Go to the next or previous match   |
| `]d` / `[d`                    | Go to the next or previous failure |
| `o`                            | Open live output in browser        |
| `Esc` / `q`                    | Close the log                      |

Logs become available in Bronson when each job finishes. Press `o` to open running output in the
provider's browser view.

## Behavior

On wide terminals, the run list starts at 45% of the width. Commit subjects occupy the first line,
with workflow, branch, job counts, and duration underneath. Enter expands one run's jobs inline.
Selecting a job previews its log in the right pane, and Enter opens the full log. Esc collapses the
jobs. Use `[` / `]` to adjust the split between 30% and 65%.

Small terminals use full-width views. Resizing back to a small terminal keeps the selected job's
parent run selected. Diagnostic blocks in a successful job's output are labeled separately from its
provider result.

Runs refresh every 5 seconds while active and every 20 seconds while idle. Failed refreshes leave
the last successful result on screen.

Reopening shows a cached dashboard immediately while the run list refreshes. Unchanged completed job
details are reused. Active runs, reruns, and changed runs fetch fresh details. Cached data is
labeled until the first successful refresh.

Quitting cancels local CLI requests rather than waiting for network timeouts. This does not undo a
rerun or cancellation already accepted by GitHub.

Completed job logs are cached in `~/.cache/bronson`, or `$XDG_CACHE_HOME/bronson` when set. GitLab
cache identities include provider, hostname, and full project path. Existing GitHub caches remain
usable and are never reinterpreted as GitLab data.
