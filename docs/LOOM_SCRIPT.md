# Loom script (about 2 minutes)

Read it off your phone. One line per breath. Use the sample-data mode, so no real personal data shows.
Do not say or show a live URL until you have confirmed it works.

Setup before you press record:
- Fresh sandbox: open the app in a private window so the chat is empty.
- Browser zoom 110 percent, notifications off, other tabs closed.
- Keep `/approvals` and `/audit` ready in two tabs.

---

**0:00 Hook (on the chat page)**

> AI assistants are great at planning.
> They are scary when they can send, book or pay on their own.
> So I built one that cannot.

**0:12 Morning briefing (click "Morning briefing", then "Open today's plan")**

> I ask for my morning briefing.
> It reads my calendar and my mail by itself.
> It found two meetings that overlap, and a day that is too full.
> It ranked the email that needs me first.
> And look here.
> One email tried to give the assistant orders.
> It was flagged, ignored, and nothing was done.

**0:42 Plan a dinner (back to chat, click "Plan dinner with Sam on Friday")**

> Now I ask it to plan dinner with a friend.
> It checked my free slots, picked three places, and drafted a message.
> But watch.
> Nothing was sent.
> Nothing was booked.

**1:00 Approval (open Approvals)**

> Both actions are waiting for me.
> I can edit the message before I approve.
> I approve the message.
> Done. And I click again, by mistake.
> It does not send twice.
> Approvals run exactly once.

**1:22 Choose another place (open the dinner options, click "Choose this one" on another)**

> Changed my mind about the restaurant.
> The old proposals are cancelled.
> New ones are waiting for approval.

**1:35 Audit log (open Audit log)**

> Every step is recorded here.
> What it read.
> What it proposed.
> Who approved it, and when.
> The log is hash chained.
> If anyone edits it, this badge turns red.

**1:52 Close**

> Reads are automatic.
> Sends, bookings and payments need a person.
> Built with LangGraph, Claude, FastAPI and Supabase.
> If you want an assistant your team can trust, send me a message.

---

Notes:
- Sending, booking and paying are simulated in this version. If asked, say so plainly.
- Do not show the `.env` file, your API keys, or your real inbox.
- If a click lags, keep talking. Do not re-record for small pauses.
