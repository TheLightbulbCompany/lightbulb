"""Headers every agent-authored mail carries.

RFC 3834: everything an agent writes is machine-generated. Compliant autoresponders (out-of-office,
helpdesk ticketing) do not answer such mail, which stops ping-pong, and our own inbound loop guard
(`agent_mail_inbound.is_auto_submitted`) drops it if it ever comes back. Set on owner notices and RSVP mail.
"""

AGENT_AUTHORED_HEADERS = {"Auto-Submitted": "auto-generated"}
