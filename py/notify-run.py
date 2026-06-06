#!/usr/bin/env python3
"""
Run a shell command and send an email when it finishes.

Usage:
    python notify-run.py "Email Title" "python train.py --log-file 'log/train.log'"
"""

import argparse
import datetime as _datetime
import html
import os
import subprocess
import sys
import time


MAX_CAPTURED_ERROR_CHARS = 2000
EMAIL_SCRIPT_PATH = "~/my/auto_email.py"


class NotifyRunLog:
    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self._email_written = False
        log_dir = os.path.dirname(self.path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as log_file:
            log_file.write(self.path + "\n\n")
            log_file.write("[log]\n")

    def log(self, message):
        with open(self.path, "a", encoding="utf-8") as log_file:
            log_file.write(message.rstrip("\n") + "\n")

    def email(self, title, body):
        with open(self.path, "a", encoding="utf-8") as log_file:
            if not self._email_written:
                log_file.write("\n[email]\n")
                self._email_written = True
            log_file.write("Title: {}\n\n".format(title))
            log_file.write(body.rstrip("\n") + "\n")


def format_duration(seconds):
    seconds = int(round(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return "{}h{}m".format(hours, minutes)
    if minutes:
        return "{}m{}s".format(minutes, secs)
    return "{}s".format(secs)


def find_email_script():
    email_script = os.path.expanduser(EMAIL_SCRIPT_PATH)
    if os.path.exists(email_script):
        return email_script

    raise RuntimeError(f"Cannot find sending email script at {EMAIL_SCRIPT_PATH}")


def clip_error_output(text):
    if len(text) <= MAX_CAPTURED_ERROR_CHARS:
        return text
    omitted = len(text) - MAX_CAPTURED_ERROR_CHARS
    return (
        "[Only showing the last {} characters; omitted {} earlier characters.]\n\n"
        .format(MAX_CAPTURED_ERROR_CHARS, omitted)
        + text[-MAX_CAPTURED_ERROR_CHARS:]
    )


def build_email_body(command, start_time, end_time, runtime, return_code, error_output):
    status_text = "Success" if return_code == 0 else "Failure"
    status_class = "success" if return_code == 0 else "error"

    rows = [
        ("Start time", start_time),
        ("Command", command),
        ("End time", end_time),
        ("Runtime", runtime),
        ("Status", status_text),
        ("Exit code", str(return_code)),
        ("Directory", os.getcwd()),
    ]

    parts = ['<div class="table-container"><table>']
    for label, value in rows:
        parts.append(
            "<tr><th>{}</th><td{}>{}</td></tr>".format(
                html.escape(label),
                ' class="{}"'.format(status_class) if label == "Status" else "",
                html.escape(value),
            )
        )
    parts.append("</table></div>")

    if return_code != 0:
        error_output = clip_error_output(error_output.strip() or "(No stderr output captured.)")
        parts.append("<h3>Error output</h3>")
        parts.append(
            '<pre style="white-space: pre-wrap; word-break: break-word; '
            'background: #f8f9fa; border: 1px solid #dee2e6; padding: 12px;">{}</pre>'
            .format(html.escape(error_output))
        )

    return "\n".join(parts)


def run_command(command, notify_log=None):
    stderr_chunks = []
    shell_path = "/bin/bash" if os.path.exists("/bin/bash") else None

    process = subprocess.Popen(
        command,
        shell=True,
        executable=shell_path,
        stdout=None,
        stderr=subprocess.PIPE,
    )

    try:
        for chunk in iter(process.stderr.readline, b""):
            os.write(2, chunk)
            stderr_chunks.append(chunk)
        return_code = process.wait()
    except KeyboardInterrupt:
        message = "Keyboard interrupt received; terminating command..."
        sys.stderr.write("\n" + message + "\n")
        sys.stderr.flush()
        if notify_log:
            notify_log.log(message)
        process.terminate()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            message = "Command did not terminate promptly; killing it..."
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
            if notify_log:
                notify_log.log(message)
            process.kill()
            return_code = process.wait()
        stderr_chunks.append(b"\nnotify-run.py interrupted by KeyboardInterrupt.\n")
        raise

    return return_code, b"".join(stderr_chunks).decode("utf-8", "replace")


def send_notification(title, body):
    email_script = find_email_script()
    subprocess.check_call(
        [
            sys.executable,
            email_script,
            title,
            body,
            "--footer", "Sent by notify-run",
            "--config-env", "~/my/_env/email.env",
            "--content-type", "html",
        ]
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a command and send a completion email.")
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path for notify-run's own log. Defaults to /tmp/notify-run_<id>.log.",
    )
    parser.add_argument("title", help="Email title")
    parser.add_argument("command", help="Command to run, quoted as one shell command string")
    args = parser.parse_args(argv)

    log_path = args.log_file or "/tmp/notify-run_{}.log".format(os.getpid())
    notify_log = NotifyRunLog(log_path)
    sys.stderr.write("notify-run log file: {}\n".format(notify_log.path))
    sys.stderr.flush()
    notify_log.log("notify-run log file: {}".format(notify_log.path))

    start_epoch = time.time()
    start_time = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return_code = 130
    error_output = ""

    try:
        return_code, error_output = run_command(args.command, notify_log)
    except KeyboardInterrupt:
        error_output += "\nnotify-run.py interrupted by KeyboardInterrupt.\n"
        message = "Command interrupted. Sending notification email..."
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
        notify_log.log(message)
    finally:
        end_epoch = time.time()
        end_time = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        runtime = format_duration(end_epoch - start_epoch)
        body = build_email_body(
            args.command,
            start_time,
            end_time,
            runtime,
            return_code,
            error_output,
        )

        try:
            send_notification(args.title, body)
            message = "Notification email sent successfully."
            sys.stderr.write(message + "\n")
            notify_log.log(message)
        except Exception as exc:
            message = "Failed to send notification email: {}".format(exc)
            sys.stderr.write(message + "\n")
            notify_log.log(message)
            if return_code == 0:
                return_code = 33
        finally:
            final_message = "notify-run log file: {}".format(notify_log.path)
            sys.stderr.write(final_message + "\n")
            notify_log.log(final_message)
            notify_log.email(args.title, body)
        sys.stderr.flush()

    return return_code


if __name__ == "__main__":
    sys.exit(main())
