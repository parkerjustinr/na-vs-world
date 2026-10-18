'use strict';

var DAY_MS = 86400000;
var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];

// --- Pure helpers (also loadable in node for testing) ---

function parseYMD(ymd) {
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd || '');
  if (!m) return null;
  return { y: +m[1], m: +m[2], d: +m[3] };
}

// Whole calendar days between two ET Y-M-D strings. UTC math only, so DST can't shift it.
function daysSince(winDateET, todayET) {
  var a = parseYMD(winDateET);
  var b = parseYMD(todayET);
  if (!a || !b) return NaN;
  return Math.round((Date.UTC(b.y, b.m - 1, b.d) - Date.UTC(a.y, a.m - 1, a.d)) / DAY_MS);
}

// Y-M-D of the given instant in America/New_York.
function todayETFor(date) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit'
  }).format(date);
}

// '2026-07-11' -> 'July 11, 2026'
function formatDate(ymd) {
  var p = parseYMD(ymd);
  if (!p) return ymd || '';
  return MONTHS[p.m - 1] + ' ' + p.d + ', ' + p.y;
}

function gamesWonText(games) {
  if (!games || !games.length) return '';
  if (games.length === 1) return 'game ' + games[0];
  var head = games.slice(0, -1).join(', ');
  return 'games ' + head + ' and ' + games[games.length - 1];
}

function isCelebrating(lastWin, todayET, nowMs, forced) {
  if (forced) return true;
  if (lastWin.game_end_utc) {
    var end = Date.parse(lastWin.game_end_utc);
    var elapsed = nowMs - end;
    return !isNaN(end) && elapsed >= 0 && elapsed < DAY_MS;
  }
  return todayET === lastWin.date_et;
}

function expectedFormat(wins, losses) {
  return (wins === 2 || losses === 2) ? 'Bo3' : 'Bo1';
}

// --- DOM ---

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function loadJSON(path) {
  return fetch(path, { cache: 'no-cache' }).then(function (res) {
    if (!res.ok) throw new Error(path + ' ' + res.status);
    return res.json();
  });
}

function renderCounter(data, params) {
  var w = data.last_win;
  var todayParam = params.get('today');
  var todayET = parseYMD(todayParam) ? todayParam : todayETFor(new Date());
  var celebrating = isCelebrating(w, todayET, Date.now(), params.get('celebrate') === '1');

  var n = celebrating ? 0 : daysSince(w.date_et, todayET);
  document.getElementById('count').textContent = String(n);

  document.getElementById('anchor-1').textContent =
    w.na_team + ' took ' + gamesWonText(w.games_won) + ' off ' + w.opponent + '.';
  var result = w.series_result === 'win' ? 'Won' : 'Lost';
  document.getElementById('anchor-2').textContent =
    w.event + ', ' + w.stage + ', ' + formatDate(w.date_et) + '. ' +
    result + ' the series ' + w.series_score + '.';

  var ctx = data.context;
  if (ctx && ctx.verified === true) {
    var line = document.getElementById('context-line');
    line.textContent = 'The last drought ran ' + ctx.previous_drought_days + ' days.';
    line.hidden = false;
  }

  if (celebrating) {
    document.body.classList.add('celebrate');
    document.getElementById('celebrate-heading').hidden = false;
    var box = document.getElementById('confetti');
    for (var i = 0; i < 36; i++) {
      var s = el('span');
      s.style.left = ((i * 37) % 100) + '%';
      s.style.animationDelay = ((i * 0.13) % 3).toFixed(2) + 's';
      s.style.animationDuration = (2.5 + (i % 5) * 0.4).toFixed(1) + 's';
      box.appendChild(s);
    }
  }
}

function renderWorlds(data) {
  var list = document.getElementById('teams');
  data.teams.forEach(function (t) {
    var card = el('article', 'card');
    card.appendChild(el('h3', 'team', t.name));
    card.appendChild(el('p', 'record', t.wins + '-' + t.losses));
    card.appendChild(el('p', 'status', t.status));

    if (t.status === 'alive' && t.next) {
      var next = el('p', 'next');
      next.appendChild(document.createTextNode(
        'Next: ' + (t.next.opponent || t.next.placeholder) + ' '));
      next.appendChild(el('span', 'chip', t.next.format));
      next.appendChild(document.createTextNode(' ' + formatDate(t.next.date)));
      card.appendChild(next);

      var want = expectedFormat(t.wins, t.losses);
      if (t.next.format !== want) {
        console.warn(t.name + ': next format is ' + t.next.format +
          ' but Swiss rule says ' + want + ' at ' + t.wins + '-' + t.losses + '.');
      }
    } else if (t.status === 'advanced') {
      card.appendChild(el('p', 'next', 'Into the knockouts.'));
    } else if (t.status === 'eliminated') {
      card.appendChild(el('p', 'next', 'Out at ' + t.wins + '-' + t.losses + '.'));
    }
    list.appendChild(card);
  });
  document.getElementById('worlds-updated').textContent = 'Updated ' + data.updated;
}

function fail(section, err) {
  console.error(err);
  if (section === 'counter') document.getElementById('counter-body').hidden = true;
  document.getElementById(section + '-error').hidden = false;
}

function init() {
  var params = new URLSearchParams(window.location.search);
  loadJSON('data/counter.json')
    .then(function (d) { renderCounter(d, params); })
    .catch(function (e) { fail('counter', e); });
  loadJSON('data/worlds2026.json')
    .then(renderWorlds)
    .catch(function (e) { fail('worlds', e); });
}

if (typeof document !== 'undefined') {
  init();
} else if (typeof module !== 'undefined') {
  module.exports = {
    daysSince: daysSince, todayETFor: todayETFor, formatDate: formatDate,
    gamesWonText: gamesWonText, isCelebrating: isCelebrating, expectedFormat: expectedFormat
  };
}
