(function () {
  ZhaoUI.setBrand('#2667ff');
  var themeSelect = document.getElementById('themeSelect');
  var colorScheme = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  var theme = 'system';
  try { theme = localStorage.getItem('sfd_theme') || 'system'; } catch (e) {}
  if (['system', 'light', 'dark'].indexOf(theme) < 0) theme = 'system';
  function applyTheme() {
    ZhaoUI.setTheme({ mode: theme === 'system' ? (colorScheme && colorScheme.matches ? 'dark' : 'light') : theme });
  }
  themeSelect.value = theme;
  themeSelect.onchange = function () {
    theme = themeSelect.value;
    try { localStorage.setItem('sfd_theme', theme); } catch (e) {}
    applyTheme();
  };
  if (colorScheme && colorScheme.addEventListener) colorScheme.addEventListener('change', applyTheme);
  applyTheme();

  function showError(text) { ZhaoUI.message(text, {type: 'error', duration: 4500}); }
  // VisualViewport also tracks the space left above the mobile keyboard.
  function setVh() {
    var viewport = window.visualViewport;
    var height = viewport && viewport.scale === 1 ? viewport.height : window.innerHeight;
    document.documentElement.style.setProperty('--vh', height * 0.01 + 'px');
    document.documentElement.style.setProperty('--sfd-viewport-height', Math.round(height) + 'px');
    var focused = document.activeElement;
    var editing = focused && /^(INPUT|TEXTAREA)$/.test(focused.tagName);
    document.documentElement.classList.toggle('keyboard-open', !!(editing && window.innerHeight - height > 140));
  }
  setVh();
  window.addEventListener('resize', setVh);
  window.addEventListener('orientationchange', function () { setTimeout(setVh, 250); });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', setVh);
    window.visualViewport.addEventListener('scroll', setVh);
  }
  document.addEventListener('focusin', setVh);
  document.addEventListener('focusout', function () { setTimeout(setVh, 0); });

  var feedEl = document.getElementById('feed');
  var favoritesList = document.getElementById('favoritesList');
  var sendersList = document.getElementById('sendersList');
  var tabs = document.querySelectorAll('.tab');
  var searchInput = document.getElementById('searchInput');
  var textInput = document.getElementById('textInput');
  var sendBtn = document.getElementById('sendBtn');
  var attachBtn = document.getElementById('attachBtn');
  var fileInput = document.getElementById('fileInput');
  var reshareInput = document.getElementById('reshareInput');
  var uploadQueue = document.getElementById('uploadQueue');
  var pendingFilesEl = document.getElementById('pendingFiles');
  var pendingFiles = [];
  var sending = false;
  var onlineCountEl = document.getElementById('onlineCount');
  var nicknamePill = document.getElementById('nicknamePill');
  var clearTextsBtn = document.getElementById('clearTexts');
  var menuToggle = document.getElementById('menuToggle');
  var sidebarEl = document.querySelector('.sidebar');
  var drawerBackdrop = document.getElementById('drawerBackdrop');
  var savedSidebarTab = 'favorites';
  try { savedSidebarTab = localStorage.getItem('sfd_sidebar_tab') || 'favorites'; } catch (e) {}
  var showSenders = savedSidebarTab === 'senders';
  document.getElementById('favoritesTab').setAttribute('aria-selected', String(!showSenders));
  document.getElementById('sendersTab').setAttribute('aria-selected', String(showSenders));
  document.getElementById('favoritesPanel').hidden = showSenders;
  document.getElementById('sendersPanel').hidden = !showSenders;
  document.getElementById('sidebarTabs').addEventListener('zh:tab-change', function (e) {
    try { localStorage.setItem('sfd_sidebar_tab', e.detail.index === 1 ? 'senders' : 'favorites'); } catch (err) {}
  });

  var currentSenderFilter = null;

  function openDrawer() { ZhaoUI.open(sidebarEl, menuToggle); drawerBackdrop.classList.add('show'); }
  function closeDrawer() { ZhaoUI.close(sidebarEl); drawerBackdrop.classList.remove('show'); }
  sidebarEl.addEventListener('zh:close', function () { drawerBackdrop.classList.remove('show'); });
  var narrowScreen = window.matchMedia ? window.matchMedia('(max-width: 720px)') : null;
  function syncSidebarLayout() {
    if (narrowScreen && narrowScreen.matches) {
      sidebarEl.classList.add('zh-drawer');
      if (!sidebarEl.classList.contains('open')) {
        sidebarEl.setAttribute('inert', '');
        sidebarEl.setAttribute('aria-hidden', 'true');
      }
    } else {
      closeDrawer();
      sidebarEl.classList.remove('zh-drawer');
      sidebarEl.removeAttribute('inert');
      sidebarEl.removeAttribute('aria-hidden');
    }
  }
  if (narrowScreen && narrowScreen.addEventListener) narrowScreen.addEventListener('change', syncSidebarLayout);
  syncSidebarLayout();
  if (menuToggle) {
    menuToggle.onclick = function () {
      if (sidebarEl.classList.contains('open')) closeDrawer(); else openDrawer();
    };
  }
  drawerBackdrop.onclick = closeDrawer;

  var currentFilter = 'all';
  var currentSearch = '';
  var currentTimeFilter = 'all';
  var lastVersion = null;
  var FEED_PAGE = 200;
  var feedLimit = FEED_PAGE;
  var pendingReshareId = null;
  var origTitle = document.title;

  function getNickname() {
    try { return localStorage.getItem('sfd_nickname') || ''; } catch (e) { return ''; }
  }
  function setNickname(name) {
    try { localStorage.setItem('sfd_nickname', name); } catch (e) {}
  }
  function nicknameHeader() {
    var n = getNickname();
    return n ? encodeURIComponent(n) : '';
  }
  function refreshNicknamePill() {
    nicknamePill.textContent = getNickname() || DEFAULT_NAME;
  }
  refreshNicknamePill();
  nicknamePill.onclick = function () {
    document.getElementById('nicknameInput').value = getNickname() || DEFAULT_NAME;
    ZhaoUI.open('#nicknameDialog', nicknamePill);
  };
  document.getElementById('nicknameForm').onsubmit = function (e) {
    e.preventDefault();
    var next = document.getElementById('nicknameInput').value.trim();
    if (!next) return;
    setNickname(next);
    refreshNicknamePill();
    ZhaoUI.close('#nicknameDialog');
    ZhaoUI.message('昵称已更新');
  };

  function getTimeRange(type) {
    var now = new Date();
    var start = new Date();
    switch(type) {
      case 'today':
        start.setHours(0, 0, 0, 0);
        return start.getTime() / 1000;
      case 'week':
        start.setDate(start.getDate() - start.getDay());
        start.setHours(0, 0, 0, 0);
        return start.getTime() / 1000;
      case 'month':
        start.setDate(1);
        start.setHours(0, 0, 0, 0);
        return start.getTime() / 1000;
      default:
        return 0;
    }
  }

  function applySearch() {
    var q = currentSearch.trim().toLowerCase();
    var items = feedEl.querySelectorAll('li[data-search]');
    var minTime = getTimeRange(currentTimeFilter);
    var rowNumber = 0;
    items.forEach(function (li) {
      var s = li.getAttribute('data-search') || '';
      var sender = li.querySelector('.msg-meta');
      var senderName = sender ? sender.textContent.split(' · ')[0] : '';
      var meta = li.getAttribute('data-meta') || '';
      var ts = parseFloat(meta) || 0;
      var matchSearch = !q || s.indexOf(q) !== -1;
      var matchSender = !currentSenderFilter || senderName.indexOf(currentSenderFilter) !== -1;
      var matchTime = currentTimeFilter === 'all' || ts >= minTime;
      li.style.display = (matchSearch && matchSender && matchTime) ? '' : 'none';
      if (li.style.display !== 'none' && sender) sender.setAttribute('data-row-number', ++rowNumber);
    });
  }
  var searchTimer = null;
  searchInput.oninput = function () {
    currentSearch = searchInput.value;
    applySearch();  // 先在已加载的内容里即时过滤，再等服务端给出完整结果
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { feedLimit = FEED_PAGE; refreshFeed(currentFilter === 'all' || currentFilter === 'text'); }, 250);
  };

  function isFeedNearBottom() {
    return feedEl.scrollHeight - feedEl.scrollTop - feedEl.clientHeight < 80;
  }
  function scrollFeedToBottom() {
    feedEl.scrollTop = feedEl.scrollHeight;
  }
  function renderFeed(htmlStr, stick, anchorBottom) {
    var prevHeight = feedEl.scrollHeight, prevTop = feedEl.scrollTop;
    feedEl.innerHTML = htmlStr;
    applySearch();
    updateExpiryLabels();
    if (stick) scrollFeedToBottom();
    else if (anchorBottom) feedEl.scrollTop = prevTop + (feedEl.scrollHeight - prevHeight);
  }
  // 搜索、时间范围、发送者都交给服务端筛选，这样分页后仍然覆盖全部记录
  function feedQuery() {
    var qs = 'filter=' + encodeURIComponent(currentFilter) + '&limit=' + feedLimit;
    var q = currentSearch.trim();
    if (q) qs += '&q=' + encodeURIComponent(q);
    var since = getTimeRange(currentTimeFilter);
    if (since) qs += '&since=' + since;
    if (currentSenderFilter) qs += '&sender=' + encodeURIComponent(currentSenderFilter);
    return qs;
  }
  function refreshFeed(forceBottom, anchorBottom) {
    var stick = forceBottom || isFeedNearBottom();
    fetch('/feed?' + feedQuery())
      .then(function (r) { return r.text(); })
      .then(function (htmlStr) { renderFeed(htmlStr, stick, anchorBottom); });
  }
  // 版本变化后一次请求拿齐 feed / 收藏 / 发送者
  function refreshAll() {
    var stick = isFeedNearBottom();
    return fetch('/bundle?' + feedQuery())
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderFeed(data.feed, stick, false);
        favoritesList.innerHTML = data.favorites;
        updateExpiryLabels();
        renderSenders(data.senders);
      });
  }

  function refreshFavorites() {
    fetch('/favorites')
      .then(function (r) { return r.text(); })
      .then(function (htmlStr) { favoritesList.innerHTML = htmlStr; updateExpiryLabels(); });
  }

  function renderSenders(senders) {
    var html = '';
    if (senders && senders.length > 0) {
      senders.forEach(function (sender) {
        var active = currentSenderFilter === sender.name ? ' active' : '';
        // 昵称由用户自己填写，必须转义，否则是存储型 XSS
        html += '<button class="zh-btn sender-btn' + active + '" data-sender="' + escapeHtml(sender.name) + '">' + escapeHtml(sender.name) + '</button>';
      });
    }
    sendersList.innerHTML = html;
    attachSenderListeners();
  }
  function refreshSenders() {
    fetch('/senders')
      .then(function (r) { return r.json(); })
      .then(function (data) { renderSenders(data.senders); });
  }

  function attachSenderListeners() {
    var senderBtns = sendersList.querySelectorAll('.sender-btn');
    senderBtns.forEach(function (btn) {
      btn.onclick = function () {
        var sender = btn.getAttribute('data-sender');
        if (currentSenderFilter === sender) {
          currentSenderFilter = null;
        } else {
          currentSenderFilter = sender;
        }
        refreshSenders();
        feedLimit = FEED_PAGE;
        refreshFeed(currentFilter === 'all' || currentFilter === 'text');
      };
    });
  }

  function formatRemaining(seconds) {
    if (seconds <= 0) return '即将清除';
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    if (days >= 1) return days + '天' + hours + '小时后清除';
    if (hours >= 1) return hours + '小时' + minutes + '分钟后清除';
    if (minutes >= 1) return minutes + '分钟后清除';
    return '即将清除';
  }
  function updateExpiryLabels() {
    if (document.hidden) return;
    var now = Date.now() / 1000;
    document.querySelectorAll('.expiry[data-expire]').forEach(function (el) {
      var remaining = parseFloat(el.getAttribute('data-expire')) - now;
      el.textContent = formatRemaining(remaining);
      el.classList.toggle('soon', remaining > 0 && remaining < 3600);
    });
  }
  setInterval(updateExpiryLabels, 30000);

  tabs.forEach(function (tab) {
    tab.onclick = function () {
      tabs.forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      currentFilter = tab.getAttribute('data-tab');
      feedLimit = FEED_PAGE;
      refreshFeed(currentFilter === 'all' || currentFilter === 'text');
    };
  });

  var timeTabs = document.querySelectorAll('.time-tab');
  timeTabs.forEach(function (tab) {
    tab.onclick = function () {
      timeTabs.forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      currentTimeFilter = tab.getAttribute('data-time');
      feedLimit = FEED_PAGE;
      refreshFeed(currentFilter === 'all' || currentFilter === 'text');
    };
  });
  if (timeTabs.length > 0) timeTabs[0].classList.add('active');

  var audioCtx = null;
  function beep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!audioCtx) audioCtx = new Ctx();  // 浏览器限制同时存在的 AudioContext 数量，复用一个
      var ctx = audioCtx;
      if (ctx.state === 'suspended') ctx.resume();
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = 'sine'; o.frequency.value = 880;
      o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.16, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
      o.start(); o.stop(ctx.currentTime + 0.3);
    } catch (e) {}
  }
  function notifyNewContent() {
    beep();
    var n = 0;
    var iv = setInterval(function () {
      document.title = (document.title === origTitle) ? '新内容 · SendFileDrop' : origTitle;
      n++;
      if (n >= 6) { clearInterval(iv); document.title = origTitle; }
    }, 500);
  }

  // 长轮询：服务端在没有新内容时挂起请求，有变化立刻返回，空闲时几乎没有流量
  function poll() {
    var url = '/status' + (lastVersion === null ? '' : '?since=' + lastVersion);
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      onlineCountEl.textContent = data.online;
      var changed = lastVersion !== null && data.version !== lastVersion;
      lastVersion = data.version;
      if (changed) {
        notifyNewContent();
        refreshAll().catch(function () {});
      }
      setTimeout(poll, 0);
    }).catch(function () { setTimeout(poll, 3000); });
  }
  scrollFeedToBottom();
  applySearch();
  window.addEventListener('load', scrollFeedToBottom);
  refreshSenders();
  poll();

  function escapeHtml(text) {
    var map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return text.replace(/[&<>"']/g, function (m) { return map[m]; });
  }

  function renderPendingFiles() {
    pendingFilesEl.innerHTML = '';
    pendingFiles.forEach(function (file, index) {
      var row = document.createElement('div');
      row.className = 'pending-file';
      var number = document.createElement('span');
      number.textContent = index + 1;
      var name = document.createElement('span');
      name.className = 'name';
      name.textContent = file.name + ' · 待发送';
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'zh-btn text sm';
      remove.textContent = '移除';
      remove.setAttribute('aria-label', '移除 ' + file.name);
      remove.onclick = function () { pendingFiles.splice(index, 1); renderPendingFiles(); };
      row.appendChild(number);
      row.appendChild(name);
      row.appendChild(remove);
      pendingFilesEl.appendChild(row);
    });
  }
  function queueFiles(files) {
    pendingFiles = pendingFiles.concat(files);
    renderPendingFiles();
  }
  async function doSendText() {
    if (sending) return;
    var val = textInput.value;
    if (!val.trim() && !pendingFiles.length) return;
    sending = true;
    sendBtn.disabled = true;
    try {
      if (val.trim()) {
        var res = await fetch('/text', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Nickname': nicknameHeader() },
          body: 'content=' + encodeURIComponent(val)
        });
        if (!res.ok) throw new Error('发送失败');
        if (textInput.value === val) { textInput.value = ''; textInput.style.height = ''; }
        refreshFeed(true);
      }
      var files = pendingFiles;
      pendingFiles = [];
      renderPendingFiles();
      var failed = await uploadFiles(files);
      if (failed.length) { queueFiles(failed); showError('部分文件发送失败，已保留在待发送列表，可重试'); }
    } catch (e) {
      showError('发送失败，请重试');
    } finally {
      sending = false;
      sendBtn.disabled = false;
    }
  }
  sendBtn.onclick = doSendText;
  textInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSendText(); }
  });
  textInput.addEventListener('input', function () {
    textInput.style.height = 'auto';
    textInput.style.height = Math.min(textInput.scrollHeight, 200) + 'px';
  });

  attachBtn.onclick = function () { fileInput.click(); };
  fileInput.onchange = function () {
    var files = Array.prototype.slice.call(fileInput.files);
    fileInput.value = '';
    if (files.length) queueFiles(files);
  };

  // 拖入、选择和粘贴文件都先进入草稿，统一由发送按钮提交。
  var mainEl = document.querySelector('.main');
  var dragCounter = 0;
  mainEl.addEventListener('dragenter', function (e) { e.preventDefault(); dragCounter++; mainEl.classList.add('drag-over'); });
  mainEl.addEventListener('dragover', function (e) { e.preventDefault(); });
  mainEl.addEventListener('dragleave', function (e) {
    e.preventDefault();
    dragCounter = Math.max(0, dragCounter - 1);
    if (dragCounter === 0) mainEl.classList.remove('drag-over');
  });
  mainEl.addEventListener('drop', function (e) {
    e.preventDefault();
    dragCounter = 0;
    mainEl.classList.remove('drag-over');
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      queueFiles(Array.prototype.slice.call(e.dataTransfer.files));
    }
  });

  // 剪切板粘贴图片功能：在编辑区任何地方 Ctrl+V 粘贴图片
  document.addEventListener('paste', function (e) {
    if (!e.clipboardData || !e.clipboardData.items) return;
    var files = [];
    for (var i = 0; i < e.clipboardData.items.length; i++) {
      var item = e.clipboardData.items[i];
      if (item.kind === 'file') {
        var file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      queueFiles(files);
    }
  });

  async function uploadFiles(files) {
    var failed = [];
    for (var i = 0; i < files.length; i++) {
      var ok = await new Promise(function (resolve) { uploadOne(files[i], resolve); });
      if (!ok) failed.push(files[i]);
    }
    refreshFeed(true);
    return failed;
  }
  function uploadOne(f, done) {
    var row = document.createElement('div');
    row.className = 'qitem';
    row.innerHTML = '<div class="top"><span class="name"></span><span class="state">上传中…</span></div><div class="qbar"><div></div></div>';
    row.querySelector('.name').textContent = f.name;
    uploadQueue.appendChild(row);
    var barIn = row.querySelector('.qbar div');
    var state = row.querySelector('.state');
    var fd = new FormData();
    fd.append('file', f);
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    xhr.setRequestHeader('X-Nickname', nicknameHeader());
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        var p = Math.round(e.loaded / e.total * 100);
        barIn.style.width = p + '%';
        state.textContent = p + '%';
      }
    };
    xhr.onload = function () {
      state.textContent = (xhr.status >= 200 && xhr.status < 400) ? '完成' : '失败';
      setTimeout(function () { row.remove(); }, 900);
      done(xhr.status >= 200 && xhr.status < 400);
    };
    xhr.onerror = function () { state.textContent = '失败'; setTimeout(function () { row.remove(); }, 900); done(false); };
    xhr.send(fd);
  }

  async function handleListClick(e) {
    if (e.target.closest('[data-load-more]')) {
      feedLimit += FEED_PAGE;
      refreshFeed(false, currentFilter === 'all' || currentFilter === 'text');
      return;
    }
    if (e.currentTarget === favoritesList && window.innerWidth <= 720 && (e.target.closest('.del') || e.target.closest('.reshare-btn') || e.target.closest('a.dl'))) {
      closeDrawer();
    }
    var starBtn = e.target.closest('.star-btn');
    if (starBtn) {
      var skind = starBtn.getAttribute('data-kind');
      var skey = starBtn.getAttribute('data-key');
      starBtn.disabled = true;
      fetch('/star/' + skind + '/' + encodeURIComponent(skey), { method: 'POST' }).then(function (res) {
        starBtn.disabled = false;
        if (res.ok) { refreshFeed(); refreshFavorites(); }
      }).catch(function () { starBtn.disabled = false; });
      return;
    }
    var copyBtn = e.target.closest('.copy-text');
    if (copyBtn) {
      var content = copyBtn.getAttribute('data-content') || '';
      if (navigator.clipboard) navigator.clipboard.writeText(content);
      copyBtn.classList.add('copied');
      setTimeout(function () { copyBtn.classList.remove('copied'); }, 1200);
      return;
    }
    var delBtn = e.target.closest('.del');
    if (delBtn) {
      var kind = delBtn.getAttribute('data-kind');
      var key = delBtn.getAttribute('data-key');
      var label = kind === 'text' ? '这条文字' : '这个文件';
      if (!await ZhaoUI.dialog({title: '删除' + label + '？', content: '删除后无法恢复。', ok: '删除', danger: true})) return;
      delBtn.disabled = true;
      var url = (kind === 'text' ? '/text/delete/' : '/file/delete/') + encodeURIComponent(key);
      fetch(url, { method: 'POST' }).then(function (res) {
        if (res.ok) { refreshFeed(); refreshFavorites(); }
        else { showError('删除失败'); delBtn.disabled = false; }
      }).catch(function () { showError('删除失败'); delBtn.disabled = false; });
      return;
    }
    var reBtn = e.target.closest('.reshare-btn');
    if (reBtn) {
      pendingReshareId = reBtn.getAttribute('data-key');
      reshareInput.click();
    }
  }
  feedEl.addEventListener('click', handleListClick);
  favoritesList.addEventListener('click', handleListClick);

  // 双击（手机上双击轻点）缩略图，放大预览原图；点击空白处、按 Esc 或点关闭按钮退出
  var lightboxEl = null;
  function closeLightbox() {
    if (!lightboxEl) return;
    lightboxEl.remove();
    lightboxEl = null;
    document.removeEventListener('keydown', onLightboxKey);
  }
  function onLightboxKey(e) { if (e.key === 'Escape') closeLightbox(); }
  function openLightbox(src, alt) {
    closeLightbox();
    lightboxEl = document.createElement('div');
    lightboxEl.className = 'lightbox';
    lightboxEl.setAttribute('role', 'dialog');
    lightboxEl.setAttribute('aria-label', '图片预览');
    var img = document.createElement('img');
    img.src = src;
    img.alt = alt || '';
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'zh-btn lightbox-close';
    close.textContent = '关闭';
    close.setAttribute('aria-label', '关闭预览');
    lightboxEl.appendChild(img);
    lightboxEl.appendChild(close);
    lightboxEl.onclick = function (e) { if (e.target !== img) closeLightbox(); };
    document.body.appendChild(lightboxEl);
    document.addEventListener('keydown', onLightboxKey);
  }
  function thumbOf(e) {
    var t = e.target && e.target.closest ? e.target.closest('.media-thumb img') : null;
    return t || null;
  }
  function onThumbDblClick(e) {
    var img = thumbOf(e);
    if (!img) return;
    e.preventDefault();
    openLightbox(img.getAttribute('src'), img.getAttribute('alt'));
  }
  feedEl.addEventListener('dblclick', onThumbDblClick);
  favoritesList.addEventListener('dblclick', onThumbDblClick);
  // 触屏不一定触发 dblclick，用 touchend 间隔自己判断双击
  var lastTap = { el: null, time: 0 };
  function onThumbTap(e) {
    var img = thumbOf(e);
    if (!img) { lastTap.el = null; return; }
    var now = Date.now();
    if (lastTap.el === img && now - lastTap.time < 350) {
      lastTap.el = null;
      e.preventDefault();
      openLightbox(img.getAttribute('src'), img.getAttribute('alt'));
    } else {
      lastTap.el = img;
      lastTap.time = now;
    }
  }
  feedEl.addEventListener('touchend', onThumbTap);
  favoritesList.addEventListener('touchend', onThumbTap);

  reshareInput.onchange = function () {
    var f = reshareInput.files[0];
    reshareInput.value = '';
    if (!f || !pendingReshareId) return;
    var fd = new FormData();
    fd.append('file', f);
    fetch('/file/reshare/' + encodeURIComponent(pendingReshareId), {
      method: 'POST', body: fd, headers: { 'X-Nickname': nicknameHeader() }
    }).then(function (res) {
      if (res.ok) { refreshFeed(); refreshFavorites(); } else showError('重新分享失败');
    }).catch(function () { showError('重新分享失败'); });
  };

  if (clearTextsBtn) {
    clearTextsBtn.onclick = async function () {
      if (!await ZhaoUI.dialog({title: '清空聊天记录？', content: '所有文字消息将被删除，此操作无法恢复。', ok: '清空记录', danger: true})) return;
      fetch('/texts/clear', { method: 'POST' }).then(function (res) {
        if (res.ok) { refreshFeed(); refreshFavorites(); } else showError('清空失败');
      }).catch(function () { showError('清空失败'); });
    };
  }

  document.getElementById('connectDevice').addEventListener('zh:popover-open', function (e) {
    e.detail.popover.querySelector('[data-close-connect]').onclick = function () {
      document.getElementById('connectDevice').click();
    };
    var button = e.detail.popover.querySelector('[data-copy-url]');
    button.onclick = async function () {
      var text = button.getAttribute('data-copy-url');
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          var field = document.createElement('textarea');
          field.value = text;
          e.detail.popover.appendChild(field);
          field.select();
          try { if (!document.execCommand('copy')) throw new Error('Copy failed'); }
          finally { field.remove(); }
        }
        ZhaoUI.message('地址已复制');
      } catch (err) { showError('复制失败，请手动选择上方地址复制'); }
    };
  });
})();
