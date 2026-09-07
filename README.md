# bronson

A terminal UI for GitHub Actions.

Bronson shows recent workflow runs, their jobs, and job logs. It uses the GitHub CLI for
authentication and API access.

## Requirements

- Python 3.10 or newer
- [GitHub CLI](https://cli.github.com/) (`gh`)
- [Textual](https://textual.textualize.io/)

Sign in to GitHub and install Textual:

```sh
gh auth login
python -m pip install textual
```

## Install and run

```sh
gh repo clone mitander/bronson
cd bronson
python -m bronson
```

Bronson uses the current repository's `origin` remote. Use `--repo` to choose one explicitly:

```sh
python -m bronson --repo owner/repository
```

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
| `o`                 | Open the selected run or job on GitHub         |
| `r`                 | Rerun failed jobs                              |
| `R`                 | Rerun all jobs                                 |
| `c`                 | Cancel a queued or running run                 |
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
| `o`                            | Open live output on GitHub         |
| `Esc` / `q`                    | Close the log                      |

Logs become available when each job finishes. GitHub's log API does not stream running jobs, so `o`
opens live output in the browser.

## Behavior

On wide terminals, the run list starts at 45% of the width. Commit subjects occupy the first line,
with workflow, branch, job counts, and duration underneath. Enter expands one run's jobs inline.
Selecting a job previews its log in the right pane, and Enter opens the full log. Esc collapses the
jobs. Use `[` / `]` to adjust the split between 30% and 65%.

Small terminals use full-width views. Resizing back to a small terminal keeps the selected job's
parent run selected. Diagnostic blocks in a successful job's output are labeled separately from its
GitHub result.

Runs refresh every 5 seconds while active and every 20 seconds while idle. Failed refreshes leave
the last successful result on screen.

Reopening shows a cached dashboard immediately while the run list refreshes. Unchanged completed job
details are reused. Active runs, reruns, and changed runs fetch fresh details. Cached data is
labeled until the first successful refresh.

Quitting cancels local CLI requests rather than waiting for network timeouts. This does not undo a
rerun or cancellation already accepted by GitHub.

Completed job logs are cached in `~/.cache/bronson`, or `$XDG_CACHE_HOME/bronson` when set.
