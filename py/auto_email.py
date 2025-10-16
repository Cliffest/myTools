"""
Requirements:
    pip install python-dotenv
"""
import datetime
import os
import smtplib
from dotenv import load_dotenv
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

MAX_SUBJECT_LENGTH = 50

def load_config(config_env):
    env_path = os.path.expanduser(config_env or "~/my/_env/email.env")
    if not os.path.exists(env_path):
        raise FileNotFoundError(f"{env_path} dose not exist")
    load_dotenv(env_path)
    
    # Read email configurations
    device = os.getenv('DEVICE') or "unknown"
    smtp_server = os.getenv('SMTP_SERVER') or "smtp.163.com"
    port = int(os.getenv('SMTP_PORT') or 465)
    sender_email = os.getenv('SENDER_EMAIL')
    password = os.getenv('SENDER_PASSWORD')
    receiver_emails_str = os.getenv('RECEIVER_EMAILS') or os.getenv('RECEIVER_EMAIL')
    if receiver_emails_str:
        receiver_emails = [email.strip() for email in receiver_emails_str.split(',')]
    else:
        receiver_emails = []
    
    # Varify required configurations
    if not all([smtp_server, sender_email, password]) or not receiver_emails:
        raise ValueError("Missing required email configuration in environment variables")
    
    return device, smtp_server, port, sender_email, password, receiver_emails

def send_email(subject, content, config_env=None, content_type="plain"):
    device, smtp_server, port, sender_email, password, receiver_emails = load_config(config_env)

    # Set the email header
    full_subject = f"[{device}]" + " " + subject
    if len(full_subject) > MAX_SUBJECT_LENGTH:
        raise ValueError(f"Subject length exceeds {MAX_SUBJECT_LENGTH} characters")
    
    if content_type == "html":
        html_content = get_html_email(full_subject, content, device=device)
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("Please use an HTML supported email client to view this email.", "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))
    elif content_type == "plain":
        msg = MIMEText(content, "plain", "utf-8")
    else:
        raise ValueError("Unsupported content type. Use 'plain' or 'html'.")
    
    msg['Subject'] = full_subject
    msg['From'] = sender_email
    # Display the first recipient, actually sent to all
    msg['To'] = receiver_emails[0] if receiver_emails else ""

    # For testing: write email to local file instead of sending
    # with open("tmp.html", "w", encoding="utf-8") as f:
    #     if content_type == "html": f.write(html_content)
    # return

    try:
        # Select connection type based on port
        if port == 465:
            server = smtplib.SMTP_SSL(smtp_server, port)
        else:
            server = smtplib.SMTP(smtp_server, port)
            server.starttls()  # Enable TLS for non-SSL ports
        
        server.login(sender_email, password)
        server.sendmail(sender_email, receiver_emails, msg.as_string())
        server.quit()
    
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("Failed to authenticate with the SMTP server. "
                          f"Please enable SMTP service on {sender_email} and change password into authorization code.")
    except Exception as e:
        raise RuntimeError(f"Failed to send email - {str(e)}")

def get_html_email(subject, content, device=None):
    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            color: #333;
            background-color: #f5f7fa;
            margin: 0;
            padding: 20px;
        }}
        .email-container {{
            max-width: 600px;
            margin: 0 auto;
            background: white;
            border-radius: 5px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }}
        .email-header {{
            background: #e0dde9;
            color: #495057;
            padding: 15px 20px;
            border-bottom: 1px solid #e9ecef;
        }}
        .email-header h1 {{
            margin: 0;
            font-size: 18px;
            font-weight: 600;
            text-align: left;
        }}
        .email-body {{
            padding: 20px;
        }}
        .info-item {{
            margin: 10px 0;
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }}
        .info-item:last-child {{
            border-bottom: none;
        }}
        .label {{
            font-weight: 600;
            color: #555;
            display: inline-block;
            width: 120px;
        }}
        .value {{
            color: #333;
        }}
        .error {{
            color: #e74c3c;
            font-weight: bold;
        }}
        .success {{
            color: #27ae60;
            font-weight: bold;
        }}
        .warning {{
            color: #f39c12;
            font-weight: bold;
        }}
        code {{
            background: #f4f4f4;
            color: #333;
            padding: 2px 4px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
            font-size: 0.9em;
            border: 1px solid #ddd;
        }}
        .note {{
            background-color: #fff9db;
            border-left: 4px solid #ffd43b;
            padding: 15px;
            margin-top: 20px;
            border-radius: 0 4px 4px 0;
        }}
        .email-footer {{
            background: #f8f9fa;
            padding: 1px 20px;
            text-align: center;
            color: #666;
            font-size: 12px;
            border-top: 1px solid #e9ecef;
        }}
        .timestamp {{
            color: #999;
            font-size: 12px;
            text-align: right;
            margin-top: 20px;
        }}
        .table-container {{
            max-width: 100%;
            overflow-x: auto;
            margin: 15px 0;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            min-width: 500px;
        }}
        th {{
            background-color: #f0ebe5;
            font-weight: 600;
            text-align: left;
            padding: 10px 12px;
            border-bottom: 2px solid #dee2e6;
        }}
        td {{
            padding: 8px 12px;
            border-bottom: 1px solid #dee2e6;
            vertical-align: top;
        }}
        tr:nth-child(even) {{
            background-color: #fdfbf7;
        }}
        tr:hover {{
            background-color: #e9ecef;
        }}
        .id-cell {{
            min-width: 50px;
            max-width: 50px;
            word-wrap: break-word;
        }}
        .status-cell {{
            min-width: 100px;
            max-width: 100px;
            word-wrap: break-word;
        }}
        .command-cell {{
            min-width: 400px;
            max-width: 400px;
            word-break: break-all;
        }}
        .directory-cell {{
            min-width: 250px;
            max-width: 250px;
            word-break: break-all;
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <div class="email-header">
            <h1>{subject}</h1>
        </div>
        <div class="email-body">
            <div class="content">
                {content}
            </div>
            <div class="timestamp">
                Send time: {(device+' • ') if device is not None else ''}{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            </div>
        </div>
        <div class="email-footer">
            <p>This email was sent automatically</p>
        </div>
    </div>
</body>
</html>
"""
 

if __name__ == "__main__":
    import time
    send_email("Test", "This is a test email.")
    time.sleep(5)
    send_email("Test HTML", f"""
        <p>This is a test email with <strong>HTML</strong> content.</p>
    """, content_type="html")