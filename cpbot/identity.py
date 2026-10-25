"""Telegram identity extraction adapted from xcbot; no runtime dependency."""
import re


def member_present(member):
    status = member.get('status')
    if status in ('creator', 'administrator', 'member'):
        return True
    if status == 'restricted':
        return bool(member.get('is_member'))
    if status in ('left', 'kicked'):
        return False
    return None


def targets(message):
    result = []
    origin = message.get('forward_origin', {})
    if origin.get('type') == 'user':
        result.append((origin['sender_user'], None, origin.get('date', 0)))
    elif message.get('forward_from'):
        result.append((message['forward_from'], None, message.get('forward_date', 0)))
    elif origin.get('type') == 'hidden_user' or message.get('forward_sender_name'):
        result.append((None, None, 0))
    for field, entities in (('text', 'entities'), ('caption', 'caption_entities')):
        text = message.get(field, '')
        for entity in message.get(entities, []):
            if entity.get('type') == 'text_mention':
                result.append((entity['user'], None, message.get('date', 0)))
        # Also tolerate text fixtures and clients with missing mention entities.
        for username in re.findall(r'(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})(?![\w])', text):
            result.append((None, username.lower(), 0))
    unique = {}
    for user, username, stamp in result:
        if user and user.get('is_bot'):
            continue
        key = ('id', user['id']) if user else ('name', username)
        unique.setdefault(key, (user, username, stamp))
    return list(unique.values())[:5]
