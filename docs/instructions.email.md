## Email Notification

When there is `#email` mark at the beginning of user prompt, signifies that you should run in "experiment notification with email" mode--follow the rules below. If there is NO `#email` mark, DISMISS this whole part; if user ask you to send email but without the `#email` mark, ask for second confirmation. 

Instructions:

1. User may give a long-running multi-step task involving multiple experiments, arrange the running of experiments first.
2. Whenever the user says they want an email after a case, milestone, experiment, hyperparameter value, training run, evaluation run, or other long job, you MUST send an email at corresponding time using specific python script `~/my/auto_email.py`, or run the experiment command by auto-email notification script `~/my/notify-run.py` in advance.
3. When sending an email by `~/my/auto_email.py`, prepare a short email body summarizing:
   - what was/were done
   - whether it/they succeeded
   - if failures occurred, what error it/they reported
4. If you become blocked, need user input, encounter an unrecoverable error, or are about to stop before the overall task is done, send an email to notify user.
5. At the end of the whole task, send a final summary email: summary of all experiments, successes, failures, important results, and result locations.

Use a safe here-doc or temp file for multiline content. Example:
```bash
cat > /tmp/ai-email.txt <<'EOF_EMAIL'
# file content
<summary of completed task>
EOF_EMAIL
```

Example usage of `~/my/auto_email.py` script:
*This script requires 'python-dotenv' and 'PySocks' packages, install them if missing.
```bash
python $HOME/my/auto_email.py <title> <mainbody> [--footer <footer>]
python $HOME/my/auto_email.py "A Test Email" "<p>This is a test email with <strong>HTML</strong> content.</p>" --footer "Sent by <who you are>"
python $HOME/my/auto_email.py "Email title, should less than 50 characters" "$(cat /tmp/ai-email.txt)" --footer "Sent by <who you are>"
```

Example usage of `~/my/notify-run.py` script:
*This script requires 'python-dotenv' and 'PySocks' packages, install them if missing.
```bash
python $HOME/my/notify-run.py "Email Title" "python train.py --log-file 'log/train.log'" [--log-file <log file path>]
```
This script will automatically send an email after the running command done, containing start time, command, end time, runtime, status (success or failure), exit code, work directory, and stderr output only on failure. Its log file contains notify-run logs and email details, format like below. Default log file path is `/tmp/notify-run_<pid>.log`.
```text
/tmp/notify-run_473873.log

[log]
...
Notification email sent successfully.
notify-run log file: /tmp/notify-run_473873.log

[email]
Title: Email Title

<div class="table-container"><table>
...
```
The exit code of this script keeps same as the exit code of its given running command. Unless the command exit 0 but error when sending email, in this case its exit code is 33.

If the sandbox or approval mode blocks the command, ask the user for approval.