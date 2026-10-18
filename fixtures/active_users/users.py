def active_emails(records):
    """Return unique active email addresses in first-active occurrence order."""
    seen = set()
    result = []
    for record in records:
        email = record["email"]
        if email in seen:
            continue
        seen.add(email)
        if record["active"]:
            result.append(email)
    return result
