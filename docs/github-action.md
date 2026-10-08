# GitHub Action: classify a crash log

The repository root holds a composite action that runs `crashpilot classify` on a
log and writes the result to the job summary. It is offline: no account, no API key,
no network beyond installing the package.

## Use it on a log file

```yaml
jobs:
  triage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: CrashPilotX/crashpilot-agent@main   # pin a tag or sha once one exists
        with:
          file: artifacts/dmesg.txt
```

## Use it on a previous step's output

```yaml
      - id: capture
        run: |
          {
            echo 'log<<EOF_LOG'
            sudo dmesg | tail -n 500
            echo 'EOF_LOG'
          } >> "$GITHUB_OUTPUT"
      - uses: CrashPilotX/crashpilot-agent@main
        with:
          log: ${{ steps.capture.outputs.log }}
```

The log reaches the action through an environment variable, not shell interpolation,
so log text cannot inject commands.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `file` | | Log file to classify. Wins over `log` when both are set. |
| `log` | | Log text to classify. |
| `python-version` | `3.12` | Python used to install CrashPilot. |
| `fail-on-severity` | | Fail the step at this severity or worse (`critical`, `high`, `medium`, `low`). Empty never fails. |

## Outputs

`category` (for example `gpu_fault`), `severity`, `confidence` (0 to 1), and `json`
(path to the full JSON result, including `evidence` and `alternatives`).

## What it can and cannot tell you

It applies the agent's heuristic rules to the text it is given. A log with no crash
signature comes back as `unknown`. It does not classify PCIe AER lines on their own.
