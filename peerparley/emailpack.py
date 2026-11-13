"""Generate ready-to-send email files and auto-send packs.

Two flavours, both downloadable as a zip:

* **.eml pack** — one .eml per student; each opens in Outlook / Apple Mail /
  Thunderbird as a pre-filled draft (with the feedback PDF attached for results).
  You review and press Send.

* **Auto-send pack** — the attachment PDFs plus ready-to-run scripts, with every
  message baked in. The user just DOUBLE-CLICKS one file: `Send emails
  (Windows).cmd` on Windows (a tiny launcher that runs the `send_all_windows.ps1`
  engine with the execution policy bypassed, so there's no right-click / policy
  fuss) or `send_all_mac.applescript` on Mac. Their installed Outlook / Apple Mail
  then sends the whole batch (the OS asks permission the first time). A web app
  can't do this itself — the browser sandbox forbids driving your desktop mail
  app — so the work is handed to a script that runs on your machine.
"""
from __future__ import annotations

import base64
import io
import re
import zipfile
from email.message import EmailMessage
from typing import List, Tuple

from . import email_delivery as mail
from . import pdfgen


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safe(s) -> str:
    return re.sub(r"[^\w]+", "_", str(s)).strip("_") or "student"


def _strip_html(h: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", h or "")
    t = re.sub(r"</p>", "\n\n", t)
    return re.sub(r"<[^>]+>", "", t).replace("&nbsp;", " ").strip()


def _b64(s: str) -> str:
    return base64.b64encode((s or "").encode("utf-8")).decode("ascii")


def _ctx(name: str, team: str, eval_no: str, course: str, link: str = "") -> dict:
    parts = (name or "").split()
    return {"first_name": parts[0] if parts else "", "team": team,
            "last_name": parts[-1] if len(parts) > 1 else "", "name": name or "",
            "eval_no": eval_no, "class": course, "link": link}


# --------------------------------------------------------------------------- #
# Message parts (transport-neutral: feed both .eml and the scripts)
#   part = {"to", "name", "subject", "body" (html), "attachments":[(filename,bytes)]}
# --------------------------------------------------------------------------- #
def invite_parts(recipients, subject_t: str, body_t: str, course: str, eval_no: str) -> List[dict]:
    out = []
    for r in recipients:
        if not r.get("email"):
            continue
        ctx = _ctx(r.get("name", ""), r.get("team", ""), eval_no, course, r.get("link", ""))
        out.append({"to": r["email"], "name": r.get("name", ""),
                    "subject": mail.render_template(subject_t, ctx),
                    "body": mail.render_template(body_t, ctx), "attachments": []})
    return out


def results_parts(teams, roster, subject_t: str, body_t: str, attach_team: bool,
                  course: str, eval_no: str, report: dict = None,
                  narratives: dict = None,
                  reports: dict = None) -> List[dict]:
    """`narratives` maps student key -> approved AI narrative (v2). A student
    missing from the map gets a PDF without that section — the .eml and
    auto-send packs must honour the same approvals as the direct send, or the
    instructor's review could be bypassed just by choosing a different delivery
    method from the dropdown."""
    narratives = narratives or {}
    reports = reports or {}
    out = []
    team_pdf = {}
    for t in teams:
        if attach_team and t.team not in team_pdf:
            team_pdf[t.team] = pdfgen.build_team_contribution_pdf(t, eval_no)
        for m in t.members:
            email = ""
            if roster is not None:
                rec = roster.match(m.name)
                email = rec["email"] if rec else ""
            ctx = _ctx(m.name, m.team, eval_no, course)
            atts = [(f"{_safe(m.name)}_feedback.pdf",
                     pdfgen.build_individual_pdf(
                         m, eval_no, course, report=reports.get(m.key, report),
                         narrative=narratives.get(m.key, "")))]
            if attach_team:
                atts.append((f"team_{t.team}_contribution.pdf", team_pdf[t.team]))
            out.append({"to": email, "name": m.name,
                        "subject": mail.render_template(subject_t, ctx),
                        "body": mail.render_template(body_t, ctx), "attachments": atts})
    return out


# --------------------------------------------------------------------------- #
# .eml pack (manual: open + Send)
# --------------------------------------------------------------------------- #
def _eml(part: dict) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = part["subject"]
    msg["To"] = part["to"] or ""
    msg.set_content(_strip_html(part["body"]) or " ")
    msg.add_alternative(part["body"] or "", subtype="html")
    for fname, data in part["attachments"]:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=fname)
    return msg.as_bytes()


def message_items(messages) -> List[Tuple[str, bytes]]:
    """Ready-to-send .eml files from already-rendered Message objects.

    This is what "prepare them for me to look over first" hands back on SMTP,
    which has no drafts folder of its own: the same promise as an Outlook draft
    — nothing has gone out yet — kept by a mechanism SMTP can actually honour.
    """
    out: List[Tuple[str, bytes]] = []
    seen: dict = {}
    for m in messages:
        name = _safe(getattr(m, "to_name", "") or getattr(m, "to_email", "") or "message")
        seen[name] = seen.get(name, 0) + 1
        suffix = "" if seen[name] == 1 else f"_{seen[name]}"
        out.append((f"{name}{suffix}.eml", _eml({
            "to": getattr(m, "to_email", ""),
            "subject": getattr(m, "subject", ""),
            "body": getattr(m, "body", ""),
            "attachments": list(getattr(m, "attachments", []) or []),
        })))
    return out


def invite_items(recipients, subject_t, body_t, course, eval_no, from_addr="") -> List[Tuple[str, bytes]]:
    return [(f"{_safe(p['name'])}.eml", _eml(p))
            for p in invite_parts(recipients, subject_t, body_t, course, eval_no)]


def results_items(teams, roster, subject_t, body_t, attach_team, course, eval_no,
                  report=None, from_addr="", narratives=None,
                  reports=None) -> List[Tuple[str, bytes]]:
    return [(f"{_safe(p['name'])}.eml", _eml(p))
            for p in results_parts(teams, roster, subject_t, body_t, attach_team,
                                   course, eval_no, report, narratives, reports)]


def zip_folders(folders: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        wrote = False
        for folder, items in folders.items():
            for fname, data in items:
                z.writestr(f"{folder}/{fname}", data)
                wrote = True
        if not wrote:
            z.writestr("README.txt", "No emails to generate for the current selection.")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Auto-send pack (scripts with the data baked in + attachment files)
# --------------------------------------------------------------------------- #
def _ps_sq(s: str) -> str:
    return "'" + (s or "").replace("'", "''") + "'"


def _as_str(s: str) -> str:
    """An AppleScript double-quoted string literal (handles quotes and newlines)."""
    return '"' + ((s or "").replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\r", "").replace("\n", "\\n")) + '"'


def _windows_launcher(drafts: bool = False) -> str:
    """A double-clickable .cmd that runs the PowerShell engine for the user.

    Double-clicking a .ps1 only opens Notepad, and running it by hand trips the
    execution policy — too much for most people. This launcher does it all:
    double-click, and it calls PowerShell with the policy bypassed for this one
    run, from its own folder. Mirrors the Mac 'double-click and Run' experience.
    """
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "echo.\r\n"
        + ("echo   PeerParley - preparing your emails as Outlook drafts...\r\n"
           if drafts else
           "echo   PeerParley - sending your emails through Outlook...\r\n")
        + "echo   (If Windows asks, choose \"More info\" then \"Run anyway\".)\r\n"
        "echo.\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0send_all_windows.ps1\"\r\n"
        "echo.\r\n"
        "echo   All done. You can close this window.\r\n"
        "pause\r\n")


def _powershell(parts: List[dict], drafts: bool = False) -> str:
    """The Windows engine. With ``drafts``, it fills the Drafts folder instead.

    Outlook's COM object sends with ``.Send()`` and drafts with ``.Save()`` —
    one call apart. So "let me look at these first" costs nothing here, and an
    instructor who wants to review does not have to abandon this delivery
    method to get it.
    """
    rows = []
    for p in parts:
        atts = ";".join(a[0] for a in p["attachments"])
        rows.append("  @{ to=%s; subject=%s; body64=%s; atts=%s }" % (
            _ps_sq(p["to"]), _ps_sq(p["subject"]), _ps_sq(_b64(p["body"])), _ps_sq(atts)))
    return (
        "# PeerParley — email engine (Outlook). You don't run this directly:\n"
        "# just double-click the .cmd file in this folder.\n"
        + ("# Outlook must be installed and signed in. Nothing is sent: each\n"
           "# message is saved to your Drafts folder.\n" if drafts else
           "# Outlook must be installed and signed in. It sends from your account.\n")
        + "$ErrorActionPreference = 'Stop'\n"
        "$dir = Split-Path -Parent $MyInvocation.MyCommand.Path\n"
        "try {\n"
        "  $outlook = New-Object -ComObject Outlook.Application\n"
        "} catch {\n"
        "  Write-Host ''\n"
        "  Write-Host 'Could not start Outlook. Make sure Microsoft Outlook is installed'\n"
        "  Write-Host 'and signed in, then run this again.' -ForegroundColor Yellow\n"
        "  return\n"
        "}\n"
        "$msgs = @(\n" + ",\n".join(rows) + "\n)\n"
        "$sent = 0; $skipped = 0\n"
        "foreach ($m in $msgs) {\n"
        "  if (-not $m.to) { $skipped++; continue }\n"
        "  try {\n"
        "    $mail = $outlook.CreateItem(0)\n"
        "    $mail.To = $m.to\n"
        "    $mail.Subject = $m.subject\n"
        "    $mail.HTMLBody = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($m.body64))\n"
        "    if ($m.atts) { foreach ($a in ($m.atts -split ';')) { $p = Join-Path $dir $a;"
        " if (Test-Path $p) { [void]$mail.Attachments.Add($p) } } }\n"
        + ("    $mail.Save(); $sent++\n"
           "    Write-Host \"  draft ready -> $($m.to)\"\n" if drafts else
           "    $mail.Send(); $sent++\n"
           "    Write-Host \"  sent -> $($m.to)\"\n")
        + "  } catch {\n"
        + ("    Write-Host \"  could not prepare $($m.to): "
           if drafts else "    Write-Host \"  could not send to $($m.to): ")
        + "$($_.Exception.Message)\" -ForegroundColor Yellow\n"
        "  }\n"
        "}\n"
        "Write-Host ''\n"
        + ("Write-Host \"$sent draft(s) are in your Outlook Drafts folder. "
           "Nothing has been sent.\" -ForegroundColor Green\n" if drafts else
           "Write-Host \"Sent $sent message(s) via Outlook.\" -ForegroundColor Green\n")
        + "if ($skipped) { Write-Host \"$skipped had no email address and were skipped.\" }\n")


def _applescript(parts: List[dict], drafts: bool = False) -> str:
    """The Mac engine. With ``drafts``, it saves each message instead of sending.

    Both mail apps can do either, so the review option exists on this delivery
    method too rather than only on the ones that go through the app's own mail
    settings.
    """
    has_att = any(p["attachments"] for p in parts)
    verb = "save" if drafts else "send"
    lines = [
        ('-- PeerParley — prepare every email as a draft in your Mac mail app.'
         if drafts else
         '-- PeerParley — send every email via your Mac mail app.'),
        '-- Open this file in Script Editor (double-click) and click Run (the ▶ button).',
        '-- You are asked which mail app to use — Microsoft Outlook by default.',
        ('-- Approve the one-time prompt that lets it control the app. Nothing is '
         'sent: each message is saved as a draft for you to read.' if drafts else
         '-- Approve the one-time prompt that lets it control the app. Sends from your account.'),
        '',
    ]
    if has_att:
        lines.append('set folderPath to (POSIX path of (choose folder with prompt '
                     '"Select the unzipped folder that holds this script and the PDF files"))')
    else:
        lines.append('set folderPath to ""')
    lines.append('set msgs to {}')
    for p in parts:
        atts = "{" + ", ".join(_as_str(a[0]) for a in p["attachments"]) + "}"
        lines.append("set end of msgs to {|to|:%s, |subj|:%s, |body|:%s, |atts|:%s}" % (
            _as_str(p["to"]), _as_str(p["subject"]), _as_str(_strip_html(p["body"])), atts))
    lines += [
        'set appPick to (choose from list {"Microsoft Outlook", "Apple Mail"} '
        'with prompt "' + ("Prepare drafts in which mail app?" if drafts
                            else "Send all of these using which mail app?") + '" '
        'default items {"Microsoft Outlook"})',
        'if appPick is false then return',
        'set appPick to item 1 of appPick',
        'set sentCount to 0',
        'if appPick is "Microsoft Outlook" then',
        '  tell application "Microsoft Outlook"',
        '    repeat with m in msgs',
        '      if (|to| of m) is not "" then',
        '        set newMsg to make new outgoing message with properties '
        '{subject:(|subj| of m), plain text content:(|body| of m)}',
        '        make new recipient at newMsg with properties '
        '{email address:{address:(|to| of m)}}',
        '        repeat with a in (|atts| of m)',
        '          try',
        '            make new attachment at newMsg with properties '
        '{file:(POSIX file (folderPath & (a as text)))}',
        '          end try',
        '        end repeat',
        '        ' + verb + ' newMsg',
        '        set sentCount to sentCount + 1',
        '      end if',
        '    end repeat',
        '  end tell',
        'else',
        '  tell application "Mail"',
        '    repeat with m in msgs',
        '      if (|to| of m) is not "" then',
        '        set newMsg to make new outgoing message with properties '
        '{subject:(|subj| of m), content:(|body| of m), visible:false}',
        '        tell newMsg',
        '          make new to recipient at end of to recipients with properties '
        '{address:(|to| of m)}',
        '          repeat with a in (|atts| of m)',
        '            try',
        '              make new attachment with properties '
        '{file name:(POSIX file (folderPath & (a as text)))} at after the last paragraph',
        '            end try',
        '          end repeat',
        '        end tell',
        '        ' + verb + ' newMsg',
        '        set sentCount to sentCount + 1',
        '      end if',
        '    end repeat',
        '  end tell',
        'end if',
        ('display dialog "Prepared " & sentCount & " draft(s) — nothing has been '
         'sent. They are in your Drafts folder." buttons {"OK"} default button "OK"'
         if drafts else
         'display dialog "Sent " & sentCount & " message(s)." buttons {"OK"} '
         'default button "OK"'),
    ]
    return "\n".join(lines) + "\n"


def _readme(drafts: bool = False, launcher: str = "Send emails (Windows).cmd") -> str:
    """The note in the zip. It has to match what the scripts actually do.

    A pack built for review used to ship a file called "Send emails" and a
    README about sending, which is the wrong instruction to find three days
    later — by then the checkbox that chose review is long forgotten and the
    folder is the only thing left saying what will happen.
    """
    if drafts:
        return (
            "PeerParley — draft pack (nothing is sent)\n"
            "=========================================\n\n"
            "This folder prepares every email as a DRAFT in the mail app on your\n"
            "computer, so you can read them before anybody receives anything.\n"
            "UNZIP it first (don't run anything from inside the .zip), then\n"
            "DOUBLE-CLICK the one file for your computer:\n\n"
            f"  WINDOWS (Outlook):            double-click  '{launcher}'\n"
            "  MAC (Outlook or Apple Mail):  double-click  'send_all_mac.applescript',\n"
            "                                then click Run. Pick your mail app when asked.\n\n"
            "Every message lands in your Drafts folder with the right address,\n"
            "subject and attachment. Read them, change anything you like, and send\n"
            "them when you are ready — from Outlook or Mail, not from here.\n\n"
            "On Windows, if you see a blue 'Windows protected your PC' box, click\n"
            "'More info' then 'Run anyway' — it is only because the file came from\n"
            "the web. The first time, your computer may ask permission to let it\n"
            "control the mail app; approve it.\n\n"
            "(Want them to go straight out instead? In PeerParley, untick \"prepare\n"
            "them for me to look over first\" and build the pack again.)\n"
        )
    return (
        "PeerParley — auto-send pack\n"
        "===========================\n\n"
        "This folder sends every email for you through the mail app already on your\n"
        "computer. UNZIP it first (don't run anything from inside the .zip), then just\n"
        "DOUBLE-CLICK the one file for your computer:\n\n"
        f"  WINDOWS (Outlook):            double-click  '{launcher}'\n"
        "  MAC (Outlook or Apple Mail):  double-click  'send_all_mac.applescript',\n"
        "                                then click Run. Pick your mail app when asked\n"
        "                                (Microsoft Outlook is the default).\n\n"
        "That's it. On Windows, if you see a blue 'Windows protected your PC' box, click\n"
        "'More info' then 'Run anyway' — it's just because the file came from the web.\n"
        "The first time, your computer may ask permission to let it control the mail\n"
        "app; approve it. Messages are sent from your own account, with the PDFs in this\n"
        "folder attached.\n\n"
        "(Advanced: 'send_all_windows.ps1' is the engine the Windows launcher runs — you\n"
        "don't need to open it. Prefer to read each one first? Tick \"prepare them for me\n"
        "to look over first\" in PeerParley and build the pack again — the scripts then\n"
        "fill your Drafts folder and send nothing.)\n"
    )


def send_all_pack(parts: List[dict], folder_label: str = "Emails",
                  drafts: bool = False) -> bytes:
    """Zip with attachment PDFs + a Windows (Outlook) and a Mac (Outlook/Mail) script.

    ``drafts`` builds the same pack in review mode: the scripts fill the Drafts
    folder and send nothing, and every file in the zip says so — the launcher's
    name included, because the one in the folder is what the instructor
    double-clicks three days later, long after the checkbox is forgotten.
    """
    launcher = ("Prepare drafts (Windows).cmd" if drafts
                else "Send emails (Windows).cmd")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        seen = set()
        for p in parts:
            for fname, data in p["attachments"]:
                if fname not in seen:
                    z.writestr(fname, data)
                    seen.add(fname)
        z.writestr(launcher, _windows_launcher(drafts))
        z.writestr("send_all_windows.ps1", _powershell(parts, drafts))
        z.writestr("send_all_mac.applescript", _applescript(parts, drafts))
        z.writestr("READ ME FIRST.txt", _readme(drafts, launcher))
    return buf.getvalue()
