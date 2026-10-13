# Gmail and Google Workspace email accounts

A Gmail or Google Workspace mailbox is added like any other in Settings > Email (IMAP
`imap.gmail.com:993`, SMTP `smtp.gmail.com:465` or `587`, your address, and a Google
[app password](https://myaccount.google.com/apppasswords)).

## The assistant asks before it changes your Gmail

Gmail is part of your Google account, so it follows "Ask before writing to my Google account
(and Gmail)" in Settings > Google (on by default; only an admin can turn it off). While it is
on, these email tools do not run on a Gmail mailbox until you press Approve on the card in the
chat:

- `send_email` and `reply_to_email` (the card shows the recipients, subject and the full text)
- `delete_email` (to Trash, or permanently), `archive_email`, `mark_email_read`
- `bulk_email` (mark read or unread, archive, delete, move to Junk, on many messages at once)

Reading never asks: listing, reading and searching email, downloading attachments, and drafts.
`draft_email`, `draft_email_reply` and `ai_draft_email_reply` save a draft document in
Odysseus for you to review and send yourself; they do not touch the mailbox.

A mailbox counts as Gmail when its IMAP or SMTP server is Google's (`imap.gmail.com`,
`smtp.gmail.com`, `googlemail.com`, so a Google Workspace address on your own domain counts
too) or its address is `@gmail.com`. Other mailboxes (Fastmail, Outlook, your own server) are
not asked about. The mailbox is picked the same way the email tools pick it: the `account`
the assistant names, or your default mailbox.

Approve sends exactly what the card shows, once. Deny tells the assistant you said no, with
your note. Unanswered requests expire after 30 minutes. Scheduled tasks and the mail listener
are held the same way and send you a notification instead of sending mail unseen.

Full details, including Google Calendar and Docs: [docs/google.md](google.md#ask-before-writing-to-my-google-account-and-gmail).
