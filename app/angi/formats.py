"""The Angi payload variants we know, written as data.

Angi runs two lead feeds with different JSON shapes:

* **Angi Ads** (formerly Angie's List) - the "Angi JSON Lead Feed" spec
  (``Angi_Standard_Lead_API_Requirements.pdf``). PascalCase, nested ``PostalAddress``.
* **Angi Leads** (formerly HomeAdvisor) - the "Angi Lead Integration API" help-centre
  article. Flat camelCase, numeric ids, an ``interview`` array.

Each format is a table of ``payload key -> canonical field, accepted JSON types,
always present?``. The parser uses the same table three ways:

* to map a payload onto ``NormalizedLead``;
* to notice drift (a key we have never seen, a documented key that vanished, a value
  whose type changed);
* as living documentation of what Angi sends.

Supporting a new variant, or a changed one, is an edit to this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STRING = ("string",)
# Ids, phone numbers, postal codes and money are documented as strings in one feed and
# numbers in the other; both are accepted everywhere and are not treated as drift.
SCALAR = ("string", "number")
NUMBER = ("number", "string")
ARRAY = ("array",)
# Angi's own samples send this flag both as true and as "false".
BOOLEAN = ("boolean", "string")


def norm_key(key: str) -> str:
    """``spEntityId`` / ``SP_Entity_Id`` / ``sp-entity-id`` -> ``spentityid``."""
    return re.sub(r"[^a-z0-9]", "", key.lower())


@dataclass(frozen=True)
class FieldSpec:
    key: str  # exact key; nested objects use a dotted path ("PostalAddress.City")
    target: str | None  # NormalizedLead attribute, or None = recognised but kept in `extras`
    types: tuple[str, ...] = STRING
    required: bool = False  # documented as always present; its absence is drift


@dataclass(frozen=True)
class FormatSpec:
    name: str
    title: str
    fields: tuple[FieldSpec, ...]
    signature_keys: frozenset[str]  # normalised top-level keys that identify the format
    # The signature keys nothing but this feed would use. "Urgency" and "Category" could
    # be any JSON at all, so at least one of these has to be present as well.
    distinctive_keys: frozenset[str]

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.key: f for f in self.fields}


# ---------------------------------------------------------------------------------------
# Angi Ads - "Angi JSON Lead Feed" (the spec this integration was built against).
#   CorrelationId - unique id of the lead; stays the same across Angi's retries
#   ALAccountId   - Angi's id for the advertiser's account -> our tenant mapping
# The spec marks every tabled field Required=Y; CorrelationId and ALAccountId appear in
# its example only, but we cannot dedupe or route without them, so they are required too.
# ---------------------------------------------------------------------------------------
ANGI_ADS = FormatSpec(
    name="angi_ads",
    title="Angi Ads - JSON Lead Feed (formerly Angie's List)",
    fields=(
        FieldSpec("FirstName", "first_name", required=True),
        FieldSpec("LastName", "last_name", required=True),
        FieldSpec("PhoneNumber", "phone", SCALAR, required=True),
        FieldSpec("PostalAddress.AddressFirstLine", "address_line1", required=True),
        FieldSpec("PostalAddress.AddressSecondLine", "address_line2"),
        FieldSpec("PostalAddress.City", "city", required=True),
        FieldSpec("PostalAddress.State", "state", required=True),
        FieldSpec("PostalAddress.PostalCode", "postal_code", SCALAR, required=True),
        FieldSpec("Email", "email", required=True),
        FieldSpec("Source", "lead_source_detail", required=True),
        FieldSpec("Description", "comments", required=True),
        FieldSpec("Category", "task_name", required=True),
        FieldSpec("Urgency", "urgency", required=True),
        FieldSpec("CorrelationId", "external_lead_id", SCALAR, required=True),
        FieldSpec("ALAccountId", "external_account_id", SCALAR, required=True),
    ),
    signature_keys=frozenset(
        norm_key(k)
        for k in ("ALAccountId", "CorrelationId", "PostalAddress", "PhoneNumber", "Urgency", "Category")
    ),
    distinctive_keys=frozenset(norm_key(k) for k in ("ALAccountId", "CorrelationId", "PostalAddress")),
)

# ---------------------------------------------------------------------------------------
# Angi Leads - "Angi Lead Integration API".
#   leadOid     - unique id of this lead (one consumer request sold to one pro)
#   srOid       - id of the consumer's service request (shared by every pro it was sold to)
#   spEntityId  - Angi's id for the pro's account -> our tenant mapping ("Sometimes" sent)
#   crmKey      - a routing value WE choose per tenant; Angi echoes it back in every lead
# `required` follows the "Always" column of Angi's field table.
# ---------------------------------------------------------------------------------------
ANGI_LEADS = FormatSpec(
    name="angi_leads",
    title="Angi Leads - Lead Integration API (formerly HomeAdvisor)",
    fields=(
        FieldSpec("name", "full_name", required=True),
        FieldSpec("firstName", "first_name"),
        FieldSpec("lastName", "last_name"),
        FieldSpec("address", "address_line1", required=True),
        FieldSpec("city", "city", required=True),
        FieldSpec("stateProvince", "state", required=True),
        FieldSpec("postalCode", "postal_code", SCALAR, required=True),
        FieldSpec("primaryPhone", "phone", SCALAR, required=True),
        FieldSpec("phoneExt", "phone_ext", SCALAR),
        FieldSpec("secondaryPhone", "secondary_phone", SCALAR),
        FieldSpec("secondaryPhoneExt", None, SCALAR),
        FieldSpec("email", "email", required=True),
        FieldSpec("srOid", "external_request_id", NUMBER, required=True),
        FieldSpec("leadOid", "external_lead_id", NUMBER, required=True),
        FieldSpec("fee", "fee", NUMBER, required=True),
        FieldSpec("taskName", "task_name", required=True),
        FieldSpec("comments", "comments", required=True),
        FieldSpec("matchType", "match_type", required=True),
        FieldSpec("leadDescription", "lead_description", required=True),
        FieldSpec("spEntityId", "external_account_id", NUMBER),
        FieldSpec("spCompanyName", "external_account_name", required=True),
        FieldSpec("contactStatus", "contact_status"),
        FieldSpec("crmKey", "crm_key", SCALAR),
        FieldSpec("leadSource", "lead_source_detail"),
        FieldSpec("trustedFormUrl", "trusted_form_url"),
        FieldSpec("trustedFormURL", "trusted_form_url"),  # Angi's table and example disagree on casing
        FieldSpec("primaryPhoneDetails.maskedNumber", "phone_is_masked", BOOLEAN),
        FieldSpec("primaryPhoneDetails.smsEnabled", None, BOOLEAN),
        FieldSpec("primaryPhoneDetails.expirationDateTime", None),
        FieldSpec("automatedContactCompliant", "automated_contact_ok", BOOLEAN),
        FieldSpec("automatedContactConsentId", None),
        FieldSpec("interview", "interview", ARRAY),
        # Sent when the homeowner already picked a time on Angi.
        FieldSpec("appointment.appointmentOid", None, NUMBER),
        FieldSpec("appointment.status", None),
        FieldSpec("appointment.type", None),
        FieldSpec("appointment.start", None),
        FieldSpec("appointment.end", None),
        FieldSpec("appointment.external_id", None),
    ),
    signature_keys=frozenset(
        norm_key(k)
        for k in (
            "spEntityId",
            "spCompanyName",
            "leadOid",
            "srOid",
            "primaryPhone",
            "stateProvince",
            "taskName",
            "matchType",
            "leadDescription",
        )
    ),
    distinctive_keys=frozenset(norm_key(k) for k in ("spEntityId", "leadOid", "srOid", "spCompanyName")),
)

FORMATS: tuple[FormatSpec, ...] = (ANGI_ADS, ANGI_LEADS)
FORMATS_BY_NAME = {spec.name: spec for spec in FORMATS}

# ---------------------------------------------------------------------------------------
# Tolerant fallback. When a payload stops matching the tables above (renamed key, new
# casing, an extra wrapper object) we still try to find the fields that matter by
# alias, so the lead gets answered immediately while a human looks at the drift alert.
# Aliases are normalised with `norm_key`, so casing and separators do not matter.
# ---------------------------------------------------------------------------------------
ALIASES: dict[str, tuple[str, ...]] = {
    "external_lead_id": ("leadoid", "correlationid", "leadid", "leadnumber", "leadguid"),
    "external_request_id": ("sroid", "servicerequestid", "srid", "requestid", "projectid"),
    "external_account_id": (
        "alaccountid",
        "spentityid",
        "accountid",
        "spid",
        "serviceproviderid",
        "providerid",
        "businessid",
        "companyid",
    ),
    "external_account_name": ("spcompanyname", "companyname", "businessname", "accountname"),
    "crm_key": ("crmkey",),
    "first_name": ("firstname", "fname", "givenname"),
    "last_name": ("lastname", "lname", "surname", "familyname"),
    "full_name": ("name", "fullname", "customername", "contactname", "consumername"),
    "email": ("email", "emailaddress", "customeremail", "contactemail", "consumeremail"),
    "phone": (
        "phonenumber",
        "primaryphone",
        "phone",
        "mobilephone",
        "mobile",
        "cellphone",
        "telephone",
        "customerphone",
        "contactphone",
    ),
    "secondary_phone": ("secondaryphone", "alternatephone", "altphone", "phone2"),
    "phone_ext": ("phoneext", "primaryphoneext", "extension"),
    "address_line1": (
        "addressfirstline",
        "address",
        "address1",
        "addressline1",
        "street",
        "streetaddress",
        "postaladdress",  # the whole address sent as one string instead of an object
    ),
    "address_line2": ("addresssecondline", "address2", "addressline2", "unit", "apt"),
    "city": ("city", "town"),
    "state": ("state", "stateprovince", "province", "region"),
    "postal_code": ("postalcode", "zipcode", "zip", "postcode"),
    "task_name": ("category", "taskname", "task", "servicetype", "servicename", "service", "projecttype"),
    "comments": ("description", "comments", "message", "notes", "details", "projectdescription"),
    "urgency": ("urgency", "timeframe", "timing"),
    "fee": ("fee", "leadfee", "price", "cost"),
    "match_type": ("matchtype",),
    "lead_description": ("leaddescription",),
    "lead_source_detail": ("source", "leadsource"),
    "contact_status": ("contactstatus",),
    "trusted_form_url": ("trustedformurl", "trustedformcerturl"),
    "phone_is_masked": ("maskednumber", "phoneismasked", "ismasked"),
    "automated_contact_ok": ("automatedcontactcompliant",),
    "interview": ("interview", "questions", "answers"),
}

# A payload we have to search by alias can describe two parties: the consumer who wants
# the work done and the business the lead was sold to. Both have a name, a phone and an
# email, so where a value sits decides whose it is ("company.email" is not the
# consumer's address; emailing it would send the business its own introduction).
CONSUMER_TARGETS = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "email",
        "phone",
        "secondary_phone",
        "phone_ext",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
    }
)
ACCOUNT_TARGETS = frozenset({"external_account_id", "external_account_name"})
# Words in the name of an enclosing object ("serviceProvider", "sp_contact", "customerInfo").
BUSINESS_WORDS = frozenset(
    {"provider", "pro", "sp", "business", "company", "vendor", "advertiser", "account", "tenant", "partner"}
)
CONSUMER_WORDS = frozenset({"customer", "consumer", "homeowner", "contact", "lead", "client"})
