"""Per-cluster keyboard layout and button contents for published reviews."""
import re
from urllib.parse import urlsplit

MAX_COMMENT_ROWS = 4
MAX_COMMENT_COLUMNS = 4
REVIEW_ACTION = 'review_current_teacher'
REVIEW_TEMPLATE = {'text': '评价当前老师', 'content': REVIEW_ACTION, 'style': None}
COMMENT_COLORS = {None: '无颜色', 'primary': '蓝色', 'success': '绿色', 'danger': '红色'}
COMMENT_LINK_FIELDS = ('cluster_id', 'teacher_id', 'review_id')


def expand_comment_link(url, cluster_id, teacher_id, review_id):
    for key, value in zip(COMMENT_LINK_FIELDS, (cluster_id, teacher_id, review_id)):
        url = url.replace('{' + key + '}', str(value))
    return url


def comment_layout(saved):
    if not isinstance(saved, list):
        return saved
    # The first editor stored one row as a flat array of two slots.
    if len(saved) != 2:
        raise ValueError('旧版评论按钮的按钮位无效。')
    buttons = {}
    for column, button in enumerate(saved, 1):
        if button is None:
            continue
        if column == 1 and button == REVIEW_TEMPLATE['text']:
            buttons['1:1'] = dict(REVIEW_TEMPLATE)
        elif isinstance(button, dict) and set(button) == {'text', 'url'}:
            buttons[f'1:{column}'] = {'text': button['text'], 'content': button['url'], 'style': None}
        else:
            raise ValueError('旧版评论按钮的内容无效。')
    return {'rows': 1, 'columns': 2, 'buttons': buttons}


def default_comment_layout():
    return {'rows': 1, 'columns': 2, 'buttons': {'1:1': dict(REVIEW_TEMPLATE)}}


def hidden_comment_positions(layout):
    hidden = set(layout.get('hidden', ()))
    for position in layout['buttons']:
        row, column = map(int, position.split(':'))
        if row > layout['rows'] or column > layout['columns']:
            hidden.add(position)
    return hidden


def visible_comment_rows(layout):
    buttons = layout['buttons']
    hidden = hidden_comment_positions(layout)
    rows = []
    for row in range(1, layout['rows'] + 1):
        visible = [buttons[key] for col in range(1, layout['columns'] + 1)
                   if (key := f'{row}:{col}') in buttons and key not in hidden]
        if visible:
            rows.append(visible)
    return rows


def parse_comment_color(text):
    text = text.strip().lower()
    if not text:
        return None
    for style, label in COMMENT_COLORS.items():
        if text == label or (style and text == style):
            return style
    raise ValueError('颜色可填蓝色、绿色、红色或无颜色。也可以省略第三行。')


def validate_comment_button(button):
    if not isinstance(button, dict) or set(button) != {'text', 'content', 'style'}:
        raise ValueError('请填写按钮的显示名称和内部内容。')
    label, content, style = button['text'], button['content'], button['style']
    if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 40 or label != label.strip()
            or re.search(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff\u2028\u2029]', label)):
        raise ValueError('显示名称需要一行，1～40 字。')
    if style is not None and (not isinstance(style, str) or style not in COMMENT_COLORS):
        raise ValueError('颜色可选蓝色、绿色、红色或无颜色。')
    if content == REVIEW_ACTION:
        return
    if (not isinstance(content, str) or len(content) > 1024 or '\\' in content
            or re.search(r'[\s\x00-\x1f\x7f-\x9f\ud800-\udfff]', content)):
        raise ValueError('内部内容需要一行有效的 HTTP 或 HTTPS 链接，最多 1024 字。')
    expanded = expand_comment_link(content, 1, 1, 1)
    if '{' in expanded or '}' in expanded:
        raise ValueError('链接变量只支持 {cluster_id}、{teacher_id} 和 {review_id}。')
    try:
        parts = urlsplit(expanded)
        if (parts.scheme in ('http', 'https') and parts.hostname and parts.username is None
                and (parts.port is None or parts.port > 0)):
            return
    except ValueError:
        pass
    raise ValueError('请输入有效的 HTTP 或 HTTPS 链接。')


def validate_comment_layout(layout):
    required = {'rows', 'columns', 'buttons'}
    if (not isinstance(layout, dict) or not required <= layout.keys() <= required | {'hidden'}
            or type(layout['rows']) is not int or not 1 <= layout['rows'] <= MAX_COMMENT_ROWS
            or type(layout['columns']) is not int or not 1 <= layout['columns'] <= MAX_COMMENT_COLUMNS
            or not isinstance(layout['buttons'], dict)):
        raise ValueError('评论按钮的行列设置无效。')
    positions = {f'{r}:{c}' for r in range(1, MAX_COMMENT_ROWS + 1) for c in range(1, MAX_COMMENT_COLUMNS + 1)}
    if not layout['buttons'].keys() <= positions:
        raise ValueError('评论按钮的位置无效。')
    hidden = layout.get('hidden', [])
    if (not isinstance(hidden, list) or any(not isinstance(key, str) or key not in layout['buttons'] for key in hidden)
            or len(set(hidden)) != len(hidden)):
        raise ValueError('隐藏的评论按钮位置无效。')
    for button in layout['buttons'].values():
        validate_comment_button(button)
