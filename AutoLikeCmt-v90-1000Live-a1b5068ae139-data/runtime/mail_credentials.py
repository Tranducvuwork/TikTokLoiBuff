def resolve(email, seed, mail_password):
    """Recover a mailbox password misplaced by the legacy four-column parser."""
    if email and not mail_password and seed and '@' in seed and not seed.lower().startswith('otpauth://'):
        return '', seed
    return seed, mail_password
