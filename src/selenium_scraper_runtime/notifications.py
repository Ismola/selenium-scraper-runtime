"""Deliver durable request failure reports using the shared runtime image.

Run one worker per spool: python -m selenium_scraper_runtime.notifications
"""

import fcntl
import html
import json
import logging
import os
import shutil
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from .logging import configure_logging


def build_message(report_path, sender, recipients):
    report_path = Path(report_path)
    report = json.loads((report_path / "pending.json").read_text(encoding="utf-8"))
    logs = (report_path / "request.log").read_text(encoding="utf-8")
    run_id = report["run_id"]
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"[ERROR] {report['service']} · {report['method']} {report['path']} · {run_id}"
    message["Date"] = formatdate(localtime=False)
    # Stable across retries so receiving systems can detect duplicate deliveries.
    message["Message-ID"] = f"<scraper-{run_id}@{sender.rsplit('@', 1)[-1].rstrip('>')}>"
    message["X-Scraper-Run-ID"] = run_id
    summary = (
        f"Servicio: {report['service']}\n"
        f"Petición: {report['method']} {report['path']}\n"
        f"HTTP: {report['status']}\n"
        f"X-Run-ID: {run_id}\n"
        f"Inicio (UTC): {datetime.fromtimestamp(report['started_at'], timezone.utc).isoformat()}\n"
        f"Duración: {report['finished_at'] - report['started_at']:.3f} s\n"
        f"Error: {report['error']}\n"
    )
    screenshots = report.get("screenshots", [])
    if not screenshots:
        summary += "Captura: no disponible (no se llegó a crear el navegador o no respondió).\n"
    for error in report.get("screenshot_errors", []):
        summary += f"Error de captura: {error}\n"
    message.set_content(summary + "\nLogs completos de la petición:\n\n" + logs)
    images = "".join(f'<p><img alt="Captura del navegador al fallar" src="cid:{run_id}-{i}"></p>'
                     for i in range(len(screenshots)))
    message.add_alternative(
        "<html><body><h2>Petición fallida del scraper</h2><pre>"
        + html.escape(summary) + "</pre>" + images
        + "<h3>Logs completos de la petición</h3><pre style='white-space:pre-wrap'>"
        + html.escape(logs) + "</pre></body></html>", subtype="html",
    )
    html_part = message.get_payload()[-1]
    for index, name in enumerate(screenshots):
        path = report_path / name
        if path.resolve().parent != report_path.resolve() or path.suffix != ".png":
            raise ValueError("Invalid screenshot path in report")
        html_part.add_related(path.read_bytes(), maintype="image", subtype="png",
                              cid=f"<{run_id}-{index}>", filename=name, disposition="inline")
    # Mail clients sometimes clip long bodies; the attachment always has all logs.
    message.add_attachment(logs.encode("utf-8"), maintype="text", subtype="plain",
                           filename=f"{run_id}.log")
    return message


def send_message(message):
    host = os.environ["SCRAPER_SMTP_HOST"]
    port = int(os.getenv("SCRAPER_SMTP_PORT", "465"))
    security = os.getenv("SCRAPER_SMTP_SECURITY", "ssl")
    if security not in {"ssl", "starttls", "plain"}:
        raise ValueError("SCRAPER_SMTP_SECURITY must be ssl, starttls or plain")
    smtp_class = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    options = {"context": ssl.create_default_context()} if security == "ssl" else {}
    with smtp_class(host, port, timeout=30, **options) as smtp:
        if security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if os.getenv("SCRAPER_SMTP_USER"):
            smtp.login(os.environ["SCRAPER_SMTP_USER"], os.environ["SCRAPER_SMTP_PASSWORD"])
        rejected = smtp.send_message(message)
        if rejected:
            raise RuntimeError("SMTP rejected one or more recipients")


def process_pending(root, deliver=send_message, now=None):
    """Retry failures without blocking HTTP workers or dropping their evidence."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    sent = 0
    with (root / ".notifier.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        for pending in sorted(root.glob("*/pending.json")):
            retry_file = pending.parent / "retry.json"
            retry = {}
            try:
                retry = json.loads(retry_file.read_text()) if retry_file.exists() else {}
                if retry.get("next_attempt", 0) > now:
                    continue
                recipients = [value.strip() for value in os.environ["SCRAPER_ALERT_TO"].split(",") if value.strip()]
                if not recipients:
                    raise ValueError("SCRAPER_ALERT_TO needs at least one recipient")
                message = build_message(pending.parent, os.environ["SCRAPER_ALERT_FROM"], recipients)
                deliver(message)
                pending.rename(pending.parent / "sent.json")
                retry_file.unlink(missing_ok=True)
                sent += 1
                logging.info("Request failure email accepted by SMTP run_id=%s", pending.parent.name)
            except Exception:
                attempts = retry.get("attempts", 0) + 1
                temporary = retry_file.with_suffix(".tmp")
                temporary.write_text(json.dumps({"attempts": attempts,
                                                "next_attempt": now + min(3600, 5 * 2 ** min(attempts, 10))}))
                temporary.replace(retry_file)
                logging.exception("Could not deliver request report run_id=%s; will retry", pending.parent.name)
        retention = int(os.getenv("REQUEST_DIAGNOSTICS_RETENTION_DAYS", "7")) * 86400
        for marker in root.glob("*/sent.json"):
            if now - marker.stat().st_mtime > retention:
                shutil.rmtree(marker.parent)
        # A killed worker may leave an unfinished log. Keep it for the same period.
        for log in root.glob("*/request.log"):
            if not (log.parent / "pending.json").exists() and not (log.parent / "sent.json").exists():
                if now - log.stat().st_mtime > retention:
                    shutil.rmtree(log.parent)
        (root / ".heartbeat").touch()
    return sent


def main():
    configure_logging()
    root = os.environ["REQUEST_DIAGNOSTICS_DIR"]
    while True:
        try:
            process_pending(root)
        except Exception:
            logging.exception("Request notification worker failed; will retry")
        time.sleep(5)


if __name__ == "__main__":
    main()
