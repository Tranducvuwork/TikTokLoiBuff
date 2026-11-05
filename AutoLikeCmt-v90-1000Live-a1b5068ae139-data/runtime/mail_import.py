def normalize(lines, account_type):
    if account_type != 'email':
        return lines
    output = []
    for number, raw in enumerate(str(lines).splitlines(), 1):
        if not raw.strip():
            continue
        fields = [part.strip() for part in raw.split('|')]
        if fields[0].lower() in ('user', 'username', 'login_user'):
            continue
        if len(fields) != 4 or not all(fields) or '@' not in fields[2]:
            raise ValueError(f'Dòng {number}: cần user|mật khẩu TikTok|mail|mật khẩu mail.')
        # Existing email importer consumes the fifth field as an optional TOTP seed.
        # Mail.tm accounts have no seed; verification comes from their mailbox.
        output.append('|'.join(fields) + '|')
    return '\n'.join(output)

def install():
    from client.app.bridge import Bridge
    for method in ('import_accounts', 'import_comment_accounts'):
        original = getattr(Bridge, method)
        def wrap(function):
            def imported(self, lines, account_type='totp'):
                try:
                    value = normalize(lines, account_type)
                except ValueError as error:
                    return {'ok': False, 'error': str(error)}
                return function(self, value, account_type)
            return imported
        setattr(Bridge, method, wrap(original))
