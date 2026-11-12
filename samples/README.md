# Sample payloads

Send any of them with `uv run python scripts/send_lead.py --sample <name>` (or
`make send SAMPLE=<name>`). Add `--email <your address>` (`EMAIL=<your address>`) to have the
lead's email sent to you; four samples are sent without it, see the notes. Every sample is
answered with HTTP 200 and `"status": "success"`, which is the only answer Angi counts as
delivered; `result` says what happened. The table is checked by `tests/test_tooling.py`,
which posts each file to a fresh database and compares the `result`.

| File | What it demonstrates | Expected `result` |
| --- | --- | --- |
| `angi_ads_spec_example.json` | The example from Angi's JSON Lead Feed spec, as valid JSON, with one change: the email address is on example.com. ALAccountId 123456 maps to Sparkle Home Cleaning; "Indianapolis – House Cleaning" is split into market and service; the fee comes from the account default ($28). | `processed` |
| `angi_ads_emergency_hvac.json` | Angi Ads lead for Summit Heating & Air (ALAccountId 200001), Urgency "Emergency", furnace not heating. | `processed` |
| `angi_ads_plumbing.json` | Angi Ads lead for BlueLine Plumbing (ALAccountId 200002). | `processed` |
| `angi_leads_example.json` | The second feed: Angi Leads (camelCase, numeric ids). spEntityId 12345678 maps to Summit; the fee ($42.50) and the interview answers come from the payload. | `processed` |
| `angi_leads_masked_with_appointment.json` | Angi Leads with a masked (temporary proxy) phone, the consent flag sent as the string "false", and an appointment object kept in `extras`. Routed by `crmKey` (tnt_summit); its unseen spEntityId 23456789 is learned as a new mapping. | `processed` |
| `drift_new_fields.json` | Angi Ads plus two fields Angi never documented (`PreferredContactTime`, `TrustedFormCertUrl`). The lead is processed normally, both values are kept under "other fields in the payload", and a schema-drift alert lists the new fields. | `processed` |
| `drift_renamed_and_recased.json` | Angi Ads content with camelCase keys, `Email` renamed to `emailAddress` and the phone sent as a number. Every field is recovered by alias (parse mode "tolerant"), the lead is emailed, and a drift alert is raised. | `processed` |
| `unknown_format.json` | A completely different shape (`{"lead": {"contact": {...}}}`). Name, email, phone, service and account id are still found by alias, so the consumer is answered; a critical "unknown format" alert is raised. It has no lead id, so sending the same file again is a `duplicate_delivery`. | `processed` |
| `malformed_spec_example.txt` | The same example with the three defects of Angi's PDF left in: curly quotes, an unquoted email, a missing comma. Not valid JSON; the fields are salvaged. Two things differ from the PDF: the address is on example.com and the CorrelationId is its own, so it is a separate lead from `angi_ads_spec_example.json`. Send it with `--raw`. | `processed` |
| `missing_email.json` | Angi Ads with `Email` empty. The lead is stored and flagged for review; no email is queued (`email.status` is "skipped"). | `processed` |
| `missing_account_id.json` | Angi Ads with no `ALAccountId` at all. No mapping can route it, so it is stored and quarantined (`reason` is "no_account_id") until an operator names the tenant: Monitoring page, "Assign and replay" under the event. Nobody is emailed before that. The missing key is also reported as format drift. | `quarantined` |
| `unmapped_account.json` | ALAccountId 000000 is not mapped to any tenant. Stored, alerted on and left waiting; it is processed automatically when the mapping is added. Nobody is emailed. | `quarantined` |
| `unrecognised.json` | A heartbeat object with no contact details at all. Stored for review; no lead is created. | `quarantined` |

Notes

- By default `send_lead.py` makes each send a new lead from a new consumer: a fresh lead id
  and a fresh fictional phone number. Two people trying the same sample are therefore not
  taken for one homeowner.
- `--same-consumer` keeps the sample's phone. Sending a sample twice that way shows
  duplicate-lead detection (same consumer, new lead id: `processed` with a `duplicate` block
  and a credit claim). The same `--email` on two leads for one Angi account has the same
  effect, because the address is the same consumer too.
- `--same-id` keeps the sample's lead id (and phone): the second send is Angi delivering
  the same lead again, `duplicate_delivery`.
- Four samples are sent without `--email`. `unknown_format.json` and `unrecognised.json`
  are not shaped like either Angi feed, so `send_lead.py` cannot tell which field to
  replace and refuses (exit code 2, nothing is sent). `missing_email.json` would get its
  empty `Email` filled in and become an ordinary lead that is emailed.
- `malformed_spec_example.txt` cannot take `--email` either (it is not JSON, so it is sent
  untouched). Its lead is addressed to `bob.builder@example.com`. Posted after
  `angi_ads_spec_example.json` exactly as that file is, it is the same consumer with a new
  lead id: a duplicate lead, not a redelivery.
- People and addresses are fictional. Every email address is @example.com, a domain
  reserved for documentation: the service never hands such an address to a real mail
  server (with `EMAIL_PROVIDER` set to `smtp` or `resend` the message is marked failed with
  "address is on a reserved example domain; nothing was sent"; with `console` it is written
  to `data/outbox/` like any other), and `send_lead.py` refuses to send a payload that
  contains any other address unless it is the one given with `--email`. Phone numbers are in the 555-01xx
  range; the exception is the number in Angi's own example (555-433-2646), whose area code
  555 does not exist.
