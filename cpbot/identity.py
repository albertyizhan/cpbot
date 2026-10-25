"""Telegram identity extraction adapted from xcbot; no runtime dependency."""
import re
from typing import NamedTuple, Optional


class Target(NamedTuple):
    user: Optional[dict]
    username: Optional[str]
    stamp: float
    hidden_name: str = ''


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
        result.append(Target(origin['sender_user'], None, origin.get('date', 0)))
    elif message.get('forward_from'):
        result.append(Target(message['forward_from'], None, message.get('forward_date', 0)))
    elif origin.get('type') == 'hidden_user' or message.get('forward_sender_name'):
        result.append(Target(None, None, 0, origin.get('sender_user_name') or message.get('forward_sender_name') or ''))
    for field, entities in (('text', 'entities'), ('caption', 'caption_entities')):
        text = message.get(field, '')
        for entity in message.get(entities, []):
            if entity.get('type') == 'text_mention':
                result.append(Target(entity['user'], None, message.get('date', 0)))
        # Also tolerate text fixtures and clients with missing mention entities.
        for username in re.findall(r'(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})(?![\w])', text):
            result.append(Target(None, username.lower(), 0))
    unique = {}
    for target in result:
        user, username = target.user, target.username
        if user and user.get('is_bot'):
            continue
        key = ('id', user['id']) if user else ('username', username) if username else ('hidden', target.hidden_name)
        unique.setdefault(key, target)
    return list(unique.values())[:5]
