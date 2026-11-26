'use strict';

const $ = id => document.getElementById(id);
const tg = window.Telegram?.WebApp;
const search = $('search');
const dialog = $('dates');
const form = $('date-form');
const feed = { items: [], next: '', pending: null, loaded: false, request: null };
let profile = null;
let searchTimer, searchRequest;
let lastRefresh = 0;
let homeScroll = 0;
let searchScroll = 0;

function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text) el.textContent = text;
  return el;
}

function action(cls, label, run) {
  const el = node('button', cls, label);
  el.type = 'button';
  el.addEventListener('click', run);
  return el;
}

function setStatus(id, message, retry) {
  const el = $(id);
  el.hidden = !message;
  el.textContent = message || '';
  if (retry) el.append(action('text-button', '重试', retry));
}

async function api(path, signal) {
  if (!tg?.initData) throw new Error('请从 Telegram 机器人打开小程序。');
  const res = await fetch(path, { signal, headers: { Authorization: `Telegram ${tg.initData}` } });
  const payload = await res.json();
  if (!res.ok) {
    const error = new Error(payload.message || '暂时无法读取车评。');
    error.status = res.status;
    throw error;
  }
  return payload;
}

function identity(person) {
  return [person.cluster, person.username && `@${person.username}`].filter(Boolean).join(' · ');
}

function initial(name) {
  return Array.from(name.trim())[0] || '师';
}

function card(review, showTeacher) {
  const item = node('article', 'card');
  const top = node('div', 'card-head');
  const title = node('div');
  const date = review.date || '日期未记录';
  const heading = node('h3', 'card-title');
  if (showTeacher) {
    heading.append(action('teacher-link', review.teacher, () => openTeacher({
      clusterId: review.clusterId, id: review.teacherId, name: review.teacher, cluster: review.cluster,
    })));
    title.append(heading, node('p', 'card-meta', `${review.cluster} · ${date}`));
  } else {
    heading.textContent = date;
    title.append(heading, node('p', 'card-meta', `车评 #${review.id}`));
  }
  top.append(title);
  if (review.score) {
    const score = node('span', 'score', review.score);
    score.setAttribute('aria-label', `综合评分 ${review.score}`);
    top.append(score);
  }
  item.append(top);
  const foot = node('div', 'card-foot');
  if (review.text) {
    const text = node('p', 'review-text clipped', review.text);
    item.append(text);
    const expand = action('text-button', '展开全文', () => {
      const expanded = !text.classList.toggle('clipped');
      expand.textContent = expanded ? '收起' : '展开全文';
      expand.setAttribute('aria-expanded', String(expanded));
    });
    expand.setAttribute('aria-expanded', 'false');
    expand.hidden = true;
    foot.append(expand);
    requestAnimationFrame(() => { expand.hidden = text.scrollHeight <= text.clientHeight + 1; });
  }
  if (review.link) {
    const link = node('a', '', 'Telegram 原帖 ↗');
    link.href = review.link;
    link.target = '_blank';
    link.rel = 'noreferrer';
    if (tg?.openTelegramLink) link.addEventListener('click', e => {
      e.preventDefault();
      tg.openTelegramLink(link.href);
    });
    foot.append(link);
  } else {
    foot.append(node('span', 'muted', '暂无可用原帖'));
  }
  item.append(foot);
  return item;
}

function appendCards(id, items, showTeacher) {
  const target = $(id);
  for (const item of items) target.append(card(item, showTeacher));
}

function showScreen() {
  const searching = !profile && search.value.trim() !== '';
  $('search-bar').hidden = Boolean(profile);
  $('teacher-bar').hidden = !profile;
  $('home').hidden = Boolean(profile) || searching;
  $('results').hidden = !searching;
  $('profile').hidden = !profile;
  $('clear-search').hidden = !search.value;
  if (profile) tg?.BackButton?.show();
  else tg?.BackButton?.hide();
}

function showFeed(page) {
  feed.items = page.items;
  feed.next = page.next || '';
  feed.pending = null;
  feed.first = JSON.stringify(page);
  feed.loaded = true;
  $('new-reviews').hidden = true;
  $('feed').replaceChildren();
  appendCards('feed', feed.items, true);
  $('more-feed').hidden = !feed.next;
  setStatus('feed-status', feed.items.length ? '' : '还没有可查看的车评。');
}

async function loadFeed(mode = 'initial') {
  if (feed.request || (mode === 'more' && !feed.next) || $('home').hidden) return;
  if (mode === 'auto' && (!feed.loaded || document.hidden)) return;
  const more = mode === 'more';
  const controller = new AbortController();
  feed.request = controller;
  const control = more ? $('more-feed') : $('refresh');
  if (mode !== 'auto') control.disabled = true;
  if (!feed.loaded) setStatus('feed-status', '正在加载车评…');
  try {
    const page = await api(more ? `/api/feed?${new URLSearchParams({cursor: feed.next})}` : '/api/feed', controller.signal);
    if (more) {
      feed.items.push(...page.items);
      feed.next = page.next || '';
      appendCards('feed', page.items, true);
      $('more-feed').hidden = !feed.next;
      setStatus('feed-status', '');
    } else {
      lastRefresh = Date.now();
      if (mode !== 'auto') showFeed(page);
      else if (JSON.stringify(page) !== feed.first) {
        if (window.scrollY < 36 && !$('home').hidden && !$('feed').contains(document.activeElement)) showFeed(page);
        else {
          feed.pending = page;
          $('new-reviews').hidden = false;
        }
      }
    }
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      showFeed({items: []});
      feed.loaded = false;
    }
    if ((mode !== 'auto' || !feed.loaded) && !controller.signal.aborted) {
      setStatus('feed-status', err.message, () => loadFeed(feed.loaded ? mode : 'refresh'));
    }
  } finally {
    control.disabled = false;
    feed.request = null;
  }
}

function queueSearch() {
  clearTimeout(searchTimer);
  searchRequest?.abort();
  showScreen();
  const q = search.value.trim();
  $('teachers').replaceChildren();
  if (!q) {
    if (!feed.loaded) loadFeed();
    return;
  }
  window.scrollTo(0, 0);
  if (q === '@') {
    setStatus('search-status', '请输入 @ 后面的用户名。');
    return;
  }
  setStatus('search-status', '正在搜索老师…');
  searchTimer = setTimeout(() => findTeachers(q), 260);
}

async function findTeachers(q) {
  const controller = new AbortController();
  searchRequest = controller;
  try {
    const {items} = await api(`/api/teachers?${new URLSearchParams({q})}`, controller.signal);
    if (controller.signal.aborted || search.value.trim() !== q) return;
    $('teachers').replaceChildren();
    for (const person of items || []) {
      const li = node('li');
      const button = action('teacher-row', '', () => openTeacher(person));
      const details = node('span', 'identity');
      details.append(node('strong', '', person.name), node('small', 'muted', identity(person)));
      const mark = node('span', 'initial', initial(person.name));
      const arrow = node('span', 'chevron', '›');
      mark.setAttribute('aria-hidden', 'true');
      arrow.setAttribute('aria-hidden', 'true');
      button.append(mark, details, arrow);
      li.append(button);
      $('teachers').append(li);
    }
    setStatus('search-status', items?.length ? '' : '没有找到老师，试试其他姓名或用户名。');
  } catch (err) {
    if (!controller.signal.aborted) setStatus('search-status', err.message, () => findTeachers(q));
  }
}

function openTeacher(person) {
  if (!$('home').hidden) homeScroll = window.scrollY;
  if (!$('results').hidden) searchScroll = window.scrollY;
  search.blur();
  history.pushState({ teacher: person }, '');
  enterTeacher(person);
}

function profileHeader(person) {
  $('teacher-initial').textContent = initial(person.name);
  $('teacher-name').textContent = person.name;
  $('teacher-meta').textContent = identity(person);
}

function enterTeacher(person) {
  profile?.request?.abort();
  profile = { person, order: 'desc', mode: 'all', from: '', to: '', next: '' };
  $('order').value = 'desc';
  $('open-dates').textContent = '全部日期';
  profileHeader(person);
  showScreen();
  window.scrollTo(0, 0);
  loadTeacher();
}

async function loadTeacher(more = false) {
  if (!profile || (more && (!profile.next || profile.request))) return;
  const current = profile;
  current.request?.abort();
  const controller = new AbortController();
  current.request = controller;
  const { person, order, from, to, next } = current;
  const query = new URLSearchParams({order});
  if (from) query.set('from', from);
  if (to) query.set('to', to);
  if (more) query.set('cursor', next);
  if (!more) {
    current.next = '';
    $('teacher-reviews').replaceChildren();
    $('more-teacher').hidden = true;
    setStatus('profile-status', '正在加载车评…');
  }
  $('more-teacher').disabled = true;
  try {
    const page = await api(`/api/teachers/${person.clusterId}/${person.id}?${query}`, controller.signal);
    if (profile !== current || controller.signal.aborted) return;
    current.next = page.next || '';
    profileHeader(page.teacher);
    appendCards('teacher-reviews', page.items, false);
    $('more-teacher').hidden = !current.next;
    setStatus('profile-status', !more && page.items.length === 0 ? (from ? '所选日期没有车评。' : '这位老师还没有车评。') : '');
  } catch (err) {
    if (profile === current && !controller.signal.aborted) setStatus('profile-status', err.message, () => loadTeacher(more));
  } finally {
    if (profile === current && current.request === controller) {
      current.request = null;
      $('more-teacher').disabled = false;
    }
  }
}

function dateFields() {
  const mode = form.elements.mode.value;
  $('date-inputs').hidden = mode === 'all';
  $('date-end-field').hidden = mode !== 'range';
  $('date-start-label').textContent = mode === 'single' ? '日期' : '开始日期';
  $('date-start').disabled = mode === 'all';
  $('date-end').disabled = mode !== 'range';
  $('date-error').hidden = true;
}

$('open-dates').addEventListener('click', () => {
  form.elements.mode.value = profile.mode;
  $('date-start').value = profile.from;
  $('date-end').value = profile.mode === 'range' ? profile.to : '';
  dateFields();
  dialog.showModal();
});
$('close-dates').addEventListener('click', () => dialog.close());
form.addEventListener('change', dateFields);
form.addEventListener('submit', e => {
  e.preventDefault();
  const mode = form.elements.mode.value;
  const from = mode === 'all' ? '' : $('date-start').value;
  const to = mode === 'range' ? $('date-end').value : from;
  if ((mode !== 'all' && !from) || (mode === 'range' && (!to || to < from))) {
    $('date-error').textContent = !from || !to ? '请选择日期。' : '结束日期不能早于开始日期。';
    $('date-error').hidden = false;
    return;
  }
  dialog.close();
  Object.assign(profile, { mode, from, to });
  $('open-dates').textContent = mode === 'all' ? '全部日期' : mode === 'single' ? from : `${from} 至 ${to}`;
  loadTeacher();
});

function back() {
  if (dialog.open) dialog.close();
  else if (profile) history.back();
}
window.addEventListener('popstate', e => {
  if (dialog.open) dialog.close();
  if (e.state?.teacher) { enterTeacher(e.state.teacher); return; }
  profile?.request?.abort();
  profile = null;
  showScreen();
  window.scrollTo(0, $('home').hidden ? searchScroll : homeScroll);
  if (!$('home').hidden && !feed.loaded) loadFeed();
});

$('back').addEventListener('click', back);
$('order').addEventListener('change', e => {
  profile.order = e.target.value;
  loadTeacher();
});
$('refresh').addEventListener('click', () => loadFeed('refresh'));
$('more-feed').addEventListener('click', () => loadFeed('more'));
$('more-teacher').addEventListener('click', () => loadTeacher(true));
$('new-reviews').addEventListener('click', () => {
  if (feed.pending) showFeed(feed.pending);
  window.scrollTo(0, 0);
  loadFeed('auto');
});
$('clear-search').addEventListener('click', () => {
  search.value = '';
  queueSearch();
  window.scrollTo(0, 0);
  search.focus();
});
search.addEventListener('input', e => { if (!e.isComposing) queueSearch(); });
search.addEventListener('compositionend', queueSearch);
search.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.isComposing) search.blur();
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && Date.now() - lastRefresh >= 10000) loadFeed('auto');
});
setInterval(() => loadFeed('auto'), 15000);

tg?.ready();
tg?.expand();
tg?.BackButton?.onClick(back);
if (history.state?.teacher) enterTeacher(history.state.teacher);
else { showScreen(); loadFeed(); }
