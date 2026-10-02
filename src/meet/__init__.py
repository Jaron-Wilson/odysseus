"""Google Meet: the voice agent in a meeting, like the in-app call and the
phone line.

    config.py    the per-user settings (prefs store, "google_meet"); off by
                 default, no secrets of its own
    links.py     Meet links and dial-in numbers: checking them, and finding
                 them in calendar events
    session.py   one meeting: the shared call loop (src/telephony/call.py)
                 in "talk with me" or "meeting assistant" mode, the live
                 transcript, the announcement, leaving, the summary
    browser.py   joining through the cloud browser (src/cloud_browser.py):
                 the Meet page driven over CDP, its audio both ways through
                 hooks injected in the page (inject.js)
    inject.js    the page side: a synthetic microphone the agent speaks
                 into, and the meeting's audio captured off WebRTC
    dialin.py    joining by phone instead: the Twilio line from
                 src/telephony/ dials the meeting's number and keys in the PIN
    google_calendar.py  making a meeting from the user's own Google account
                 (Calendar API with a Meet, invites, meeting access set to Open)
    tool.py      the agent's google_meet tool: start, schedule, join, status

routes/meet_routes.py wires these to the Settings card and the Join a Meet
panel. docs/google-meet.md is the research behind the choice of paths and
the setup.
"""
