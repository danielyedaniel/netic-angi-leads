# Demo walkthrough

A run-through of about fifteen minutes that exercises every part of the system. The
reasoning behind each behaviour is in [DESIGN.md](DESIGN.md).

Two addresses are used below: `<reviewer's address>` (somebody in the room) and
`<your address>` (the presenter's). Each step uses its own Angi account, so the same
reviewer can receive every email without one step's lead being taken for a repeat of
another's. The one place the same address meets the same account again is step 5, where
that is the point.

## Before the demo

```bash
make test                      # everything green
make reset-db && make simulate # two weeks of realistic history (never sends real email)
make run                       # terminal 1
make tunnel                    # terminal 2: note the https URL
```

- `PUBLIC_BASE_URL` in `.env` must be the ngrok URL (restart `make run` after changing it),
  otherwise the booking links in emails point at localhost.
- `EMAIL_PROVIDER=smtp` with `SMTP_USERNAME` and the Gmail app password in `SMTP_PASSWORD`.
- `ERROR_RATE_MIN_EVENTS=10` in `.env` for the demo. The `ingest_errors` check goes critical
  when a fifth of the last hour's events did not become leads, counted from 3 events by
  default. On a quiet demo database the one quarantined lead of step 3 is 1 of 5 events,
  exactly that fifth, unless something else arrived in the hour before.
- Who gets mail from your own commands: only an address you pass with `EMAIL=` or
  `--email`. The samples' own addresses are @example.com, and the service never hands
  those to a real mail server; such a lead's email shows on its page as failed, "address
  is on a reserved example domain; nothing was sent". A lead somebody else posts is
  emailed at whatever address it carries, with one exception: the address printed in
  Angi's own example payload is refused the same way, because it may be a stranger's.
- While rehearsing with real addresses that are not yours (a colleague's, a reviewer's),
  set `EMAIL_REDIRECT_ALL_TO` to your own address in `.env`: every email then comes to
  you, its subject prefixed with `[for <the lead's address>]`. Remove it and restart
  before the live run, or the reviewers' emails come to you too. While it is set, the
  samples' @example.com leads are delivered to you as well, so the failed emails of
  steps 3, 4 and 6 and the `email_failures` warning only appear without it: do one
  rehearsal with it removed, using your own address as the reviewer's.
- After a full rehearsal, stop the service and run `make reset-db && make simulate` again.
  Steps 2 and 4 send leads with fixed ids; on a database that has already seen them the
  first answer would be `duplicate_delivery` instead of `processed`.
- Open three tabs: the dashboard (`/dashboard?live=1`), your inbox, and
  `http://localhost:4040` (ngrok's request inspector).
- Send yourself one lead and confirm the email arrives and the booking link opens:

  ```bash
  make send EMAIL=<your address> SAMPLE=angi_leads_example
  ```

  The first visit to an ngrok URL in a browser shows ngrok's "Visit site" interstitial
  once; API calls are not affected.
- Have the reviewers' details ready: the webhook URL
  `https://<ngrok-domain>/webhooks/angi/leads` and the API key for the `X-API-KEY` header.
  Ask them to put their own address in `Email` and a new value in `CorrelationId`: a lead
  is emailed at the address it carries, and the same `CorrelationId` again is the same
  lead (answered `duplicate_delivery`, or `updated` if something in it changed), not a new
  one. Angi's printed example can be posted as it is (its account, `123456`, is mapped to
  Sparkle Home Cleaning): the lead is stored and routed, and its page shows that the email
  was held back because the address is the one from Angi's documentation.
- If a reviewer has already sent a lead of their own for account `123456` from the
  address you use in step 1, step 1's lead is, correctly, a repeat request (a `duplicate`
  block and the short acknowledgement instead of the introduction). Use another address
  for step 1, or show it as step 5 early. A phone number counts the same way: a lead that
  keeps the printed example's phone is flagged as a duplicate of an earlier lead with
  that phone on the account (after step 4, the salvaged one).
- Follow-ups are real: every lead that got an introduction and has not booked gets a
  nudge after an hour and another the next day (between 8 AM and 8 PM local), for as long
  as the service runs. The nudge for your own check lead can arrive during the demo (show
  it). Stop the service when you are done, or the reviewers get theirs; `make reset-db`
  drops whatever is still queued.

## The demo

### 1. The happy path — 3 min

> "Angi posts a lead. We store it, work out which business it is for, and email the
> homeowner within a couple of seconds."

```bash
make send EMAIL=<reviewer's address>
```

Show, in order:

1. The **response**: `status: success`, `result: processed`, which tenant, how it was
   routed, `duplicate: null`, `email: queued`.
2. The **email** arriving: it names the service and city, lists three real arrival
   windows, has a button.
3. Click a window → **booking page** with that window selected → confirm → the
   confirmation email.
4. The **lead's page** in the dashboard: the timeline (Lead received from Angi → Email
   queued → Email sent, with the seconds since it was received → Booking link opened →
   Appointment booked), the exact email that was sent, the raw payload.

Point to make: *the webhook only writes to the database. The email is sent by a worker
that is woken the instant the transaction commits, so Angi gets its answer in
milliseconds and a slow mail server can never make Angi time out.*

### 2. Angi's contract and idempotency — 2 min

> "Angi retries three times, fifteen minutes apart, on anything that is not a 200 with a
> success body. So two things follow."

```bash
uv run python scripts/send_lead.py --sample angi_ads_plumbing --email <reviewer's address> --same-id --repeat 2
```

The same lead id is sent twice. First response: `result: processed` (BlueLine Plumbing).
Second response: `result: duplicate_delivery`, no second email, one lead in the database.

Point to make: *idempotent on Angi's lead id within the Angi account, enforced by a
unique index inside a `BEGIN IMMEDIATE` transaction, so even simultaneous retries produce
one lead and one email.* And: *we answer success for anything we have safely stored,
because a retry of the same payload cannot fix a problem on our side.*

### 3. Tenant mapping — 2.5 min

```bash
make send EMAIL=<reviewer's address> SAMPLE=angi_ads_emergency_hvac   # a different business
make send EMAIL=<reviewer's address> SAMPLE=unmapped_account          # an Angi account we do not know
```

The first email comes from a different tenant (Summit Heating & Air). The second
responds `result: quarantined`, `reason: unmapped_account`; show the critical alert and
the waiting event on the Monitoring page. Then on the Tenants page, which names the
account that is waiting, add the mapping for account `000000` to BlueLine Plumbing (type
a default fee such as 39 in the fee box: the Angi Ads feed carries no price, and without
one these leads count as $0 on the Overview). The
leads waiting for that account are processed immediately (two of them: this one, whose
email goes out now, and one the simulated history left there).

```bash
make send SAMPLE=missing_account_id          # no Angi account id in the payload at all
```

`result: quarantined`, `reason: no_account_id`. No mapping can route this one, so its
row on the Monitoring page has a tenant picker: choose Summit Heating & Air, press
**Assign and replay**, and it becomes that tenant's lead. (It keeps the sample's own
@example.com address, so its email is not handed to the mail server. And with no Angi
account it has no fee: the Overview's note in step 6 about one lead without a fee is
this one.)

Point to make: *we never guess which business a homeowner contacted, because the email
goes out under that business's name. Nothing is lost while the mapping is missing, and a
URL never overrules a mapping: if the two disagree, the lead waits for a person.*

### 4. Angi changes the format — 3 min

> "The brief says Angi has changed the format without warning. Here is what happens."

```bash
make send EMAIL=<your address> SAMPLE=drift_renamed_and_recased
```

Keys are camelCase, `Email` is now `emailAddress`, the phone is a number. The lead is
**still processed and still emailed**; the response lists what was recovered by alias,
and Monitoring shows a new `schema_drift` alert naming the fields, next to the one that
step 3's lead without an account id raised. (It goes to your address
because the reviewer already has a lead with this business from step 1; the same address
again would, correctly, be flagged as a repeat request.)

```bash
uv run python scripts/send_lead.py --sample malformed_spec_example --raw
```

> "This is the example from Angi's own spec with its defects left in. It is not valid
> JSON: curly quotes, an unquoted email, a missing comma. We salvage it."

`result: processed`, `parseMode: tolerant`, and the warnings say what was salvaged (a
third `schema_drift` alert opens for it). Only
the address (example.com instead of the one Angi printed) and the lead id differ from
the spec, so this lead's email is not sent to anyone; its page shows why. (The
`email_failures` health check counts the messages stopped this way, here and in step 3;
that warning is expected.)

```bash
make send SAMPLE=unrecognised                # a heartbeat: nobody to contact
```

`result: quarantined`, `reason: unrecognised_payload`. Show **Payload shapes seen**, then
the event's row: **Replay** (for after a parser fix) and **Discard** (this one will never
be a lead). Discard it: its critical alert closes, the quarantine health check goes back
to ok, and the raw request stays on record under the "discarded" filter.

Point to make: *store raw first, parse tolerantly, alert on every deviation, replay after
the fix. A format change costs an edit to one table in `formats.py`, not lost leads.*

### 5. Duplicates and rebates — 1.5 min

```bash
make send EMAIL=<reviewer's address>      # the homeowner from step 1 again, as a new lead
```

Response: `duplicate: { matchedOn: [email], confidence: high, creditClaimId: ... }`, and
the email is the "We got your new request" note, not a second introduction, because that
address already heard from this business. Open **Credits**: the claim, its deadline, the
eligibility check against Angi's conditions, the ready-to-paste request text, and the CSV
export. (The list is sorted by deadline, soonest first, so the new claim is its last row;
choosing Status "detected" shortens the list to the claims still to be filed.)

Point to make: *Angi's rule is "charged twice for the same homeowner within 45 days",
with a request deadline and a requirement that the pro tried to call them within 24
hours (we record an email, and the claim says so). There is no API for claims, so the
product is a worklist that makes filing a thirty-second job and never misses a deadline.*

### 6. Analytics and monitoring — 2 min

Overview page with the simulated history: funnel (a won job counts as booked), speed to
lead, cost per booking, return on spend, by service. Then Monitoring: the health checks,
especially **silence** (no leads when the tenant's own history says there should be) and
the **canary**.

```bash
make canary EMAIL=<your address>
```

It prints one line of JSON: `ok`, how long the webhook took and how long the email took.
If there is time, open a lead whose email failed (the salvaged one from step 4): the
Messages table has a **Retry** button, which is what an operator presses after fixing the
mail credentials. Pressed on this lead it is refused with a message, because the address
is @example.com; on a real address it queues the email again.
