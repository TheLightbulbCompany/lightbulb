"""Competition sign-up sheet emails — the join confirmation ("You're #N")
sent when an owner joins the sheet, and the close variants sent by the close
action (``core.services.launch_close``): the two winner ones, the paying
non-winner ("still in") and the dropped non-winner ("not this round").

Mirrors ``welcome_email.py``'s shape: best-effort, HTML + text, never raises.
No pot numbers in any email — the pot is admin-only.

**The copy is admin-editable.** ``_DEFAULT_COPY`` below stays the source of
truth; the launch-settings row only ever holds OVERRIDES (a blank or absent
field falls straight back to the default here), written from the admin
console via ``launch_settings.update_email_copy``. Overrides are PLAIN TEXT
authored by a platform admin and are escaped into the HTML shell exactly like
the user-supplied first name — admin copy never carries markup. Placeholders
are filled by ``_fill``, which leaves an unknown one standing literally
rather than raising: a typo in admin copy must never break a close.
"""

import html
import logging
from datetime import datetime
from string import Formatter
from zoneinfo import ZoneInfo

from core.config import settings
from core.services import email_service

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

_STYLE = (
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    "font-size:16px;line-height:1.6;color:#1a1a1a;max-width:520px;margin:0 auto"
)

FIELDS = ("subject", "lead", "detail")

# The shipped copy — every variant, every field. The settings row overrides
# these; it never replaces them.
_DEFAULT_COPY: dict[str, dict[str, str]] = {
    "join": {
        "subject": "You're #{position} for the Lightbulb launch competition",
        "lead": "You're #{position} on the Lightbulb launch competition sheet.",
        "detail": "The first {comp_slots} on the sheet get their first month free.",
    },
    "winner_paid": {
        "subject": "You won a free month of Lightbulb",
        "lead": "Your free month is set — your next invoice is $0.",
        "detail": "You made the top of the launch competition sheet, so your next month of Lightbulb is on us.",
    },
    "winner_unpaid": {
        "subject": "You won a free month of Lightbulb",
        "lead": "Your first month of Workspace is free — applied automatically at checkout.",
        "detail": (
            "You made the top of the launch competition sheet, so your first month of Lightbulb Workspace is on us."
        ),
    },
    "competing": {
        "subject": "You're in the launch competition",
        "lead": "The sign-up sheet is closed, and you're in the competition.",
        "detail": (
            "The {comp_slots} free months went to the earliest spots on the sheet, so this one's on your own "
            "subscription — but you're competing on equal footing from October 1. The board ranks the dollar "
            "lift over your prior 30 days, so what matters now is what your company actually does between now "
            "and December 1."
        ),
    },
    "not_selected": {
        "subject": "The launch competition starts October 1",
        "lead": "The sign-up sheet is closed, and the {comp_slots} free months are taken.",
        "detail": (
            "You're not in the competition this round — those spots went to the earliest sign-ups and the "
            "people who brought others along. Nothing changes about your company: Friday and your agents keep "
            "working, and everything you've built stays yours. Subscribe any time and keep going; we'll be "
            "building in public the whole way through."
        ),
    },
}

# The shipped copy, read-only for the admin console's "reset to default".
DEFAULT_COPY = _DEFAULT_COPY
VARIANTS: tuple[str, ...] = tuple(_DEFAULT_COPY)

# What each variant can substitute — the admin write path validates against
# this, so a placeholder a variant cannot supply is a 422 at save time rather
# than a literal `{referral_link}` in a customer's inbox.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "join": ("position", "comp_slots", "referral_link", "sheet_closes"),
    "winner_paid": (),
    "winner_unpaid": (),
    "competing": ("comp_slots",),
    "not_selected": ("comp_slots",),
}

_KIND: dict[str, str] = {
    "join": "launch_sheet_join",
    "winner_paid": "launch_winner",
    "winner_unpaid": "launch_winner",
    "competing": "launch_competing",
    "not_selected": "launch_not_selected",
}


class _Safe(dict):
    """``{unknown}`` renders as itself instead of raising KeyError."""

    def __missing__(self, key):
        return "{" + key + "}"


def placeholders_in(text: str) -> set[str]:
    """The placeholder names a copy string uses — bare names only. ``""``
    comes back for a bare ``{}``, which no variant supplies.

    Raises ``ValueError``, with a message fit to show an admin, on anything
    ``str.format`` would accept but we will not:

    - **unbalanced braces** — what ``str.format`` itself rejects;
    - **attribute / index traversal** (``{comp_slots.__class__}``,
      ``{x[0]}``) — ``format_map`` walks those at RENDER time, so a value's
      internals would reach a customer's inbox;
    - **format specs and conversions** (``{comp_slots:>999999999}``,
      ``{x!r}``) — a width spec is a string-amplification bomb: it renders
      before any guard can run, on the single non-autoscaled backend task.

    ``_fill`` runs this on every render, so the fence lives here rather than
    only in the admin write path — a future caller of ``render()`` that
    bypasses validation is safe too."""
    try:
        parsed = list(Formatter().parse(text))
    except ValueError:
        raise ValueError("has an unbalanced { or } — use {{ or }} for a literal brace") from None
    names = set()
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if spec or conversion:
            raise ValueError(f"format specs and conversions are not allowed: {{{name}}}")
        if "." in name or "[" in name:
            raise ValueError(f"attribute and index lookups are not allowed: {{{name}}}")
        names.add(name)
    return names


def _fill(template: str, values: dict) -> str:
    try:
        placeholders_in(template)  # the gate — traversal, specs, bad braces
        return template.format_map(_Safe(values))
    except Exception:  # noqa: BLE001 — bad admin copy renders raw, never explodes a close
        logger.warning("launch email copy has unusable placeholders; rendering it raw")
        return template


def _closes_date(sheet_closes_at: datetime) -> str:
    local = sheet_closes_at.astimezone(_ET)
    return f"{local.strftime('%A, %B %-d')} at {local.strftime('%-I:%M %p')} ET"


def _greeting(first_name: str | None, *, escape: bool) -> str:
    if not first_name:
        return "Hi there,"
    # first_name is user-controlled (from Clerk); escape it so a name with
    # markup can't inject HTML into mail sent from the official sender.
    return f"Hi {html.escape(first_name) if escape else first_name},"


def resolve_copy(variant: str, overrides: dict | None, values: dict) -> dict[str, str]:
    """The three copy strings for a variant: a non-blank override from the
    settings row wins, else the shipped default, placeholders filled."""
    stored = (overrides or {}).get(variant) or {}
    out: dict[str, str] = {}
    for field in FIELDS:
        raw = stored.get(field)
        text = raw.strip() if isinstance(raw, str) and raw.strip() else _DEFAULT_COPY[variant][field]
        out[field] = _fill(text, values)
    return out


def render(variant: str, *, copy: dict | None = None, first_name: str | None = None, values: dict) -> dict[str, str]:
    """``{subject, html, text}`` for one variant. Copy is escaped into the
    HTML with ``quote=False`` — these are text nodes, so apostrophes and
    quotes stay readable while ``& < >`` can never become markup."""
    c = resolve_copy(variant, copy, values)
    extra_html = extra_text = ""
    if variant == "join":
        referral_line = f"Every friend who subscribes moves you up {values['referral_jump']} spots. Share your link:"
        link = values["referral_link"]
        extra_html = f"""
  <p>{html.escape(referral_line, quote=False)}</p>
  <p>
    <a href="{html.escape(link, quote=True)}"
       style="display:inline-block;padding:12px 20px;background:#111;color:#fff;text-decoration:none;border-radius:8px">
      {html.escape(link, quote=False)}
    </a>
  </p>
  <p style="color:#666;font-size:14px">The sheet closes {html.escape(values["sheet_closes"], quote=False)}.</p>"""
        extra_text = f"\n{referral_line}\n{link}\n\nThe sheet closes {values['sheet_closes']}.\n"
    html_body = f"""\
<div style="{_STYLE}">
  <p>{_greeting(first_name, escape=True)}</p>
  <p><strong>{html.escape(c["lead"], quote=False)}</strong></p>
  <p>{html.escape(c["detail"], quote=False)}</p>{extra_html}
  <p style="color:#999;font-size:13px">— The Lightbulb team</p>
</div>"""
    text_body = f"""\
{_greeting(first_name, escape=False)}

{c["lead"]}

{c["detail"]}
{extra_text}
— The Lightbulb team"""
    return {"subject": c["subject"], "html": html_body, "text": text_body}


async def _send(
    variant: str, *, to_email: str, first_name: str | None, values: dict, copy: dict | None, kind_suffix: str = ""
) -> str | None:
    body = render(variant, copy=copy, first_name=first_name, values=values)
    try:
        return await email_service.send_email(
            to=to_email,
            subject=body["subject"],
            html=body["html"],
            text=body["text"],
            from_addr=settings.EMAIL_FROM_WELCOME,
            reply_to=settings.EMAIL_SUPPORT_ADDRESS,
            kind=_KIND[variant] + kind_suffix,
        )
    except Exception:  # noqa: BLE001 — a failed email must never fail the join or the close
        logger.exception("launch %s email send raised for %s", variant, to_email)
        return None


def _join_values(
    *, position: int, referral_link: str, referral_jump: int, comp_slots: int, sheet_closes_at: datetime
) -> dict:
    return {
        "position": position,
        "comp_slots": comp_slots,
        "referral_link": referral_link,
        "sheet_closes": _closes_date(sheet_closes_at),
        # Not a documented placeholder (PLACEHOLDERS["join"]) — it only feeds
        # the fixed referral line, so `{referral_jump}` in admin copy renders
        # literally, exactly like any other unknown name.
        "referral_jump": referral_jump,
    }


async def send_join_email(
    *,
    to_email: str,
    first_name: str | None,
    position: int,
    referral_link: str,
    referral_jump: int,
    comp_slots: int,
    sheet_closes_at: datetime,
    copy: dict | None = None,
) -> str | None:
    """The join confirmation. Best-effort: returns the SES MessageId or
    None. Never raises — the join must succeed even if SES hiccups."""
    values = _join_values(
        position=position,
        referral_link=referral_link,
        referral_jump=referral_jump,
        comp_slots=comp_slots,
        sheet_closes_at=sheet_closes_at,
    )
    return await _send("join", to_email=to_email, first_name=first_name, values=values, copy=copy)


async def send_winner_paid_email(*, to_email: str, first_name: str | None, copy: dict | None = None) -> str | None:
    """Winner who already pays: the coupon is on their subscription."""
    return await _send("winner_paid", to_email=to_email, first_name=first_name, values={}, copy=copy)


async def send_winner_unpaid_email(*, to_email: str, first_name: str | None, copy: dict | None = None) -> str | None:
    """Winner not yet subscribed: the coupon pre-applies at checkout. The
    free month is a Workspace month — the coupon is product-restricted, so the
    copy says so."""
    return await _send("winner_unpaid", to_email=to_email, first_name=first_name, values={}, copy=copy)


async def send_still_competing_email(
    *, to_email: str, first_name: str | None, comp_slots: int, copy: dict | None = None
) -> str | None:
    """Not a winner, but paying — they stay in the competition, just without
    the free month. ``comp_slots`` is passed in (like the join email's) so the
    number always matches the close that produced this send."""
    return await _send(
        "competing", to_email=to_email, first_name=first_name, values={"comp_slots": comp_slots}, copy=copy
    )


async def send_not_selected_email(
    *, to_email: str, first_name: str | None, comp_slots: int, copy: dict | None = None
) -> str | None:
    """Neither a winner nor paying — out of the competition, keeps the
    product."""
    return await _send(
        "not_selected", to_email=to_email, first_name=first_name, values={"comp_slots": comp_slots}, copy=copy
    )


# --- admin console: preview + test send -------------------------------------

SAMPLE_POSITION = 7
SAMPLE_REFERRAL_LINK = "https://flickmeon.ai/?ref=SAMPLE12"


def sample_values(*, comp_slots: int, referral_jump: int, sheet_closes_at: datetime | None) -> dict:
    """Placeholder values for the admin preview + test send. Real settings
    where we have them (so the preview shows the number a real send would),
    a sample position/link where there is no real one."""
    closes = sheet_closes_at or datetime(2026, 9, 30, 16, 0, tzinfo=ZoneInfo("UTC"))
    return _join_values(
        position=SAMPLE_POSITION,
        referral_link=SAMPLE_REFERRAL_LINK,
        referral_jump=referral_jump,
        comp_slots=comp_slots,
        sheet_closes_at=closes,
    )


def preview_document(body_html: str) -> str:
    """The admin preview's body wrapped in a minimal standalone document with
    a WHITE ground — what a real mail client provides, and what the body
    fragment already assumes (its text colour is a fixed dark hex, so on a
    dark ground it would be unreadable). This is email HTML rendered in an
    isolated sandboxed iframe, not app chrome: a literal hex belongs here,
    and putting it here rather than on the iframe element keeps the console's
    own styling on brand tokens."""
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        f'<body style="margin:0;padding:16px;background:#ffffff">{body_html}</body></html>'
    )


async def send_test(variant: str, *, to_email: str, copy: dict | None, values: dict) -> str | None:
    """Admin "send test to me": the real body with sample values, to the
    signed-in admin's own address. Touches no sheet row and stamps no claim —
    it is only ``_send`` with a distinguishable ``kind``."""
    return await _send(variant, to_email=to_email, first_name=None, values=values, copy=copy, kind_suffix="_test")


# --- team interest: the founders' own notification ---------------------------
#
# Deliberately NOT one of the VARIANTS above. Those are CUSTOMER copy: they are
# admin-editable, carry validated placeholders, and appear in the console's
# email editor and test-send. This one is internal ops mail — it goes to the
# founders' own inbox, has nothing an admin would want to reword, and putting
# it in VARIANTS would put an internal notice in the customer-copy editor.
# Shape follows the module anyway: HTML + text, _STYLE, its own `kind`,
# best-effort, never raises.

# The standing rule the founders would otherwise have to rederive at close:
# a winning team's free month covers the Workspace subscription.
TEAM_FREE_MONTH_NOTE = (
    "If this team wins, their free month covers the $50 Workspace plan. "
    "Ultra is $150/month; both plans include unlimited members and inference costs are separate."
)


async def send_team_interest_email(
    *,
    team_name: str,
    seats: int,
    note: str,
    submitter_email: str,
    submitter_user_id: str,
    submitter_org_slug: str | None,
    contribution_cents: int,
) -> str | None:
    """Tell the founders a team raised its hand. Best-effort: returns the SES
    MessageId or None, and NEVER raises — the submission is stored either way.
    Reply-To is the submitter, so a reply reaches them directly."""
    pot = f"${contribution_cents / 100:,.2f}"
    rows = [
        ("Team", team_name),
        ("Seats", str(seats)),
        ("Submitted by", submitter_email or submitter_user_id),
        ("Owner", submitter_org_slug or submitter_user_id),
        ("Pot contribution if counted", pot),
    ]
    row_html = "".join(f"<p><strong>{html.escape(k)}:</strong> {html.escape(v, quote=False)}</p>" for k, v in rows if v)
    note_html = (
        f'<p><strong>Note:</strong></p><blockquote style="border-left:3px solid #ddd;padding-left:12px;'
        f'white-space:pre-wrap">{html.escape(note, quote=False)}</blockquote>'
        if note
        else ""
    )
    body_html = f"""\
<div style="{_STYLE}">
  <p><strong>A team wants in on the launch competition.</strong></p>
  {row_html}{note_html}
  <p style="color:#666;font-size:14px">{html.escape(TEAM_FREE_MONTH_NOTE, quote=False)}</p>
  <p style="color:#666;font-size:14px">
    Team entry is manual — nothing has been entered. Mark it counted in
    /admin/launch for it to contribute to the pot.
  </p>
</div>"""
    body_text = (
        "A team wants in on the launch competition.\n\n"
        + "".join(f"{k}: {v}\n" for k, v in rows if v)
        + (f"\nNote:\n{note}\n" if note else "")
        + f"\n{TEAM_FREE_MONTH_NOTE}\n"
        + "\nTeam entry is manual — nothing has been entered. Mark it counted in "
        "/admin/launch for it to contribute to the pot.\n"
    )
    try:
        return await email_service.send_email(
            to=settings.EMAIL_SUPPORT_ADDRESS,
            subject=f"[Launch] Team entry: {team_name} ({seats} seats)",
            html=body_html,
            text=body_text,
            from_addr=settings.EMAIL_FROM_TICKETS,
            reply_to=submitter_email or settings.EMAIL_SUPPORT_ADDRESS,
            kind="launch_team_interest",
        )
    except Exception:  # noqa: BLE001 — a failed email must never fail the submission
        logger.exception("launch team-interest email send raised for %s", submitter_user_id)
        return None
