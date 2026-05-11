(function(){
  const loader = document.getElementById('siteLoader');
  if (!loader) return;

  document.body.style.overflow = 'hidden';

  function kickHeroVideo() {
    const heroVideo = document.getElementById('heroLion');
    if (!heroVideo || typeof heroVideo.play !== 'function') return;
    heroVideo.muted = true;
    heroVideo.defaultMuted = true;
    heroVideo.setAttribute('muted', '');
    heroVideo.setAttribute('playsinline', '');
    heroVideo.playsInline = true;
    let played = false;
    const tryPlay = () => {
      if (played && !heroVideo.paused) return Promise.resolve();
      const p = heroVideo.play();
      return (p && typeof p.then === 'function') ? p.then(() => { played = true; }) : Promise.resolve();
    };
    const bindGesture = () => {
      const events = ['touchstart', 'touchend', 'click', 'scroll', 'keydown', 'pointerdown'];
      const resumeOnGesture = () => {
        tryPlay().catch(() => {});
        events.forEach(ev => window.removeEventListener(ev, resumeOnGesture, { capture: true }));
      };
      events.forEach(ev => window.addEventListener(ev, resumeOnGesture, { capture: true, passive: true }));
    };
    const retryEvents = ['loadedmetadata', 'loadeddata', 'canplay', 'canplaythrough'];
    retryEvents.forEach(ev => heroVideo.addEventListener(ev, () => { tryPlay().catch(() => {}); }, { once: true }));
    heroVideo.addEventListener('ended', () => {
      try {
        if (isFinite(heroVideo.duration)) {
          heroVideo.currentTime = Math.max(0, heroVideo.duration - 0.05);
        }
        heroVideo.pause();
      } catch(e) {}
    });
    try { heroVideo.load(); } catch(e) {}
    tryPlay().catch(() => { bindGesture(); });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && heroVideo.paused) tryPlay().catch(() => {});
    });
  }

  kickHeroVideo();

  function hideLoader() {
    loader.classList.add('is-hidden');
    document.body.style.overflow = '';
    kickHeroVideo();
    setTimeout(() => loader.remove(), 600);
  }

  const MIN_LOADER_MS = 1600;
  const startedAt = performance.now();
  function hideWithDelay() {
    const elapsed = performance.now() - startedAt;
    const wait = Math.max(0, MIN_LOADER_MS - elapsed);
    setTimeout(hideLoader, wait);
  }
  // Не ждем window.load (видео может грузиться долго на медленных соединениях):
  // прячем loader как только DOM готов + минимальная длительность анимации бара.
  if (document.readyState === 'interactive' || document.readyState === 'complete') {
    hideWithDelay();
  } else {
    document.addEventListener('DOMContentLoaded', hideWithDelay);
  }
  // Safety fallback на случай если что-то пошло не так.
  setTimeout(() => { if (!loader.classList.contains('is-hidden')) hideLoader(); }, 2500);
})();

const header = document.getElementById('header');
let lastScroll = 0;

const onScroll = () => {
  const y = window.scrollY;
  if (y > 80) {
    header.classList.add('is-collapsed');
  } else {
    header.classList.remove('is-collapsed');
  }
  lastScroll = y;
};

window.addEventListener('scroll', onScroll, { passive: true });
onScroll();


// Mobile burger menu
(function initBurger(){
  const burger = document.getElementById('headerBurger');
  const menu = document.getElementById('headerMobileMenu');
  if (!burger || !menu || !header) return;
  const close = () => {
    header.classList.remove('is-menu-open');
    burger.setAttribute('aria-expanded', 'false');
    menu.setAttribute('aria-hidden', 'true');
  };
  const toggle = () => {
    const open = !header.classList.contains('is-menu-open');
    header.classList.toggle('is-menu-open', open);
    burger.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-hidden', String(!open));
  };
  burger.addEventListener('click', toggle);
  menu.querySelectorAll('a[data-close]').forEach(a => a.addEventListener('click', close));
  document.addEventListener('click', (e) => {
    if (!header.classList.contains('is-menu-open')) return;
    if (e.target.closest('.header')) return;
    close();
  });
  window.addEventListener('scroll', () => {
    if (header.classList.contains('is-menu-open')) close();
  }, { passive: true });
})();

const lion = document.getElementById('heroLion');
if (lion) {
  let frozen = false;
  const freezeNow = () => {
    if (frozen) return;
    frozen = true;
    lion.pause();
  };

  lion.addEventListener('timeupdate', () => {
    if (!lion.duration || !isFinite(lion.duration)) return;
    if (lion.currentTime >= lion.duration - 0.12) {
      freezeNow();
    }
  });
}

// Cases: 3D vertical stack carousel (Safari-tabs style) / horizontal scroll on mobile
(function(){
  const stack = document.getElementById('casesStack');
  if (!stack) return;

  const tabs = [...stack.querySelectorAll('.case-tab')];
  const dotsEl = document.getElementById('casesDots');
  const hintEl = document.getElementById('casesHint');
  const N = tabs.length;

  const mq = window.matchMedia('(max-width: 900px)');
  const applyMobileMode = (isMobile) => {
    stack.classList.toggle('is-mobile-scroll', isMobile);
    if (isMobile) {
      tabs.forEach(t => {
        t.style.removeProperty('--y');
        t.style.removeProperty('--s');
        t.style.removeProperty('--o');
        t.style.zIndex = '';
        t.style.pointerEvents = 'auto';
      });
      if (hintEl) hintEl.style.display = 'none';
      if (dotsEl) dotsEl.style.display = 'none';
    } else {
      if (hintEl) hintEl.style.display = '';
      if (dotsEl) dotsEl.style.display = '';
    }
  };
  applyMobileMode(mq.matches);
  mq.addEventListener('change', (e) => {
    applyMobileMode(e.matches);
    if (!e.matches) location.reload();
    else initMobileInfinite();
  });

  // ===== МОБИЛКА: бесконечная автопрокрутка кейсов =====
  function initMobileInfinite() {
    if (!mq.matches) return;
    // Клонируем набор карточек, чтобы получить 3 копии подряд (оригинал + 2 клона)
    if (!stack.dataset.cloned) {
      const frag1 = document.createDocumentFragment();
      const frag2 = document.createDocumentFragment();
      tabs.forEach(t => { frag1.appendChild(t.cloneNode(true)); });
      tabs.forEach(t => { frag2.appendChild(t.cloneNode(true)); });
      stack.appendChild(frag1);
      stack.appendChild(frag2);
      stack.dataset.cloned = '1';
      // Клики по клонам не должны ломать навигацию - достаточно что они визуальны
      stack.querySelectorAll('.case-tab').forEach(el => {
        el.querySelectorAll('.case-tab__link').forEach(a => {
          // ссылки продолжают работать как есть
        });
      });
    }

    const getCopyWidth = () => {
      const cards = [...stack.children].slice(0, N);
      if (!cards.length) return 0;
      const first = cards[0].getBoundingClientRect();
      const last = cards[cards.length - 1].getBoundingClientRect();
      const gap = parseFloat(getComputedStyle(stack).gap) || 0;
      return (last.right - first.left) + gap;
    };

    // Старт в середине (начало 2-й копии)
    const jumpToMiddle = () => {
      const w = getCopyWidth();
      // отключаем snap, чтобы прыжок не дёргал
      const prevSnap = stack.style.scrollSnapType;
      stack.style.scrollSnapType = 'none';
      stack.scrollLeft = w;
      requestAnimationFrame(() => { stack.style.scrollSnapType = prevSnap || ''; });
    };
    // Ждём layout
    requestAnimationFrame(jumpToMiddle);
    setTimeout(jumpToMiddle, 300);

    // Бесконечный цикл: при пересечении границ перекидываем на середину
    stack.addEventListener('scroll', () => {
      const w = getCopyWidth();
      if (!w) return;
      if (stack.scrollLeft < w * 0.25) {
        const prev = stack.style.scrollSnapType;
        stack.style.scrollSnapType = 'none';
        stack.scrollLeft += w;
        requestAnimationFrame(() => { stack.style.scrollSnapType = prev || ''; });
      } else if (stack.scrollLeft > w * 1.75) {
        const prev = stack.style.scrollSnapType;
        stack.style.scrollSnapType = 'none';
        stack.scrollLeft -= w;
        requestAnimationFrame(() => { stack.style.scrollSnapType = prev || ''; });
      }
    }, { passive: true });

    // Автоскролл: плавное движение rAF
    let rafId = null;
    let paused = false;
    let lastTs = 0;
    const SPEED = 28; // px/сек
    const tick = (ts) => {
      if (!lastTs) lastTs = ts;
      const dt = (ts - lastTs) / 1000;
      lastTs = ts;
      if (!paused && document.visibilityState === 'visible') {
        const prev = stack.style.scrollSnapType;
        if (prev !== 'none') stack.style.scrollSnapType = 'none';
        stack.scrollLeft += SPEED * dt;
      }
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);

    // Пауза при касании / колесе
    let resumeTimer = null;
    const pause = () => {
      paused = true;
      clearTimeout(resumeTimer);
    };
    const scheduleResume = () => {
      clearTimeout(resumeTimer);
      resumeTimer = setTimeout(() => { paused = false; lastTs = 0; }, 2500);
    };
    stack.addEventListener('touchstart', pause, { passive: true });
    stack.addEventListener('touchend', scheduleResume, { passive: true });
    stack.addEventListener('touchcancel', scheduleResume, { passive: true });
    stack.addEventListener('wheel', () => { pause(); scheduleResume(); }, { passive: true });
    document.addEventListener('visibilitychange', () => { lastTs = 0; });
  }

  initMobileInfinite();
  if (mq.matches) return; // Bail out of 3D-stack behavior on mobile
  let active = Math.max(0, tabs.findIndex(t => t.classList.contains('is-active')));
  if (active < 0) active = 0;

  // Build dots
  if (dotsEl) {
    for (let i = 0; i < N; i++) {
      const d = document.createElement('span');
      d.className = 'cases__dot';
      d.dataset.idx = String(i);
      dotsEl.appendChild(d);
    }
  }

  function render() {
    const halfN = N / 2;
    tabs.forEach((t, i) => {
      // normalize to [0, N) then fold to (-halfN, halfN]
      let rel = ((i - active) % N + N) % N;
      if (rel > halfN) rel -= N;
      t.classList.toggle('is-active', rel === 0);

      let y, scale, opacity, zi, pe;
      if (rel < 0) {
        // Above — dismissed, off the top
        y = `calc(-100% - ${Math.min(Math.abs(rel), 3) * 10}px)`;
        scale = 0.92;
        opacity = 0;
        zi = 1;
        pe = 'none';
      } else if (rel === 0) {
        y = '0px';
        scale = 1;
        opacity = 1;
        zi = 100;
        pe = 'auto';
      } else {
        // Below — peek stack (show max 4 upcoming, rest hidden)
        const base = 492; // active card height + gap
        const step = 70;
        y = (base + (rel - 1) * step) + 'px';
        scale = Math.max(0.82, 1 - rel * 0.04);
        if (rel > 4) {
          opacity = 0;
          pe = 'none';
        } else {
          opacity = Math.max(0.25, 1 - (rel - 1) * 0.18);
          pe = rel <= 3 ? 'auto' : 'none';
        }
        zi = 100 - rel;
      }

      t.style.setProperty('--y', y);
      t.style.setProperty('--s', scale);
      t.style.setProperty('--o', opacity);
      t.style.zIndex = String(zi);
      t.style.pointerEvents = pe;
    });

    if (dotsEl) {
      dotsEl.querySelectorAll('.cases__dot').forEach((d, i) => {
        d.classList.toggle('is-active', i === active);
      });
    }
  }

  function setActive(idx) {
    idx = ((idx % N) + N) % N;
    if (idx === active) return;
    active = idx;
    markTouched();
    render();
  }

  function next() { setActive(active + 1); }
  function prev() { setActive(active - 1); }

  let touched = false;
  function markTouched() {
    if (touched) return;
    touched = true;
    if (hintEl) hintEl.style.opacity = '0';
    stack.classList.add('is-touched');
  }

  // Wheel / trackpad — advance on the first event of a fresh gesture (gap-based).
  // Any wheel events arriving <120ms apart are treated as the same gesture (inertia)
  // and ignored, so deliberate back-to-back scrolls always register.
  let lastActionTime = 0;
  let lastDelta = 0;
  stack.addEventListener('wheel', (e) => {
    // On narrow viewports: let the page scroll normally, use swipe instead
    if (window.matchMedia('(max-width: 900px)').matches) return;
    e.preventDefault();
    const dy = e.deltaY;
    if (Math.abs(dy) < 4) return;
    const now = performance.now();
    const sinceAction = now - lastActionTime;
    // Inertia detection: consecutive events with decaying magnitude in the same direction
    const decaying = lastDelta !== 0 && Math.sign(dy) === Math.sign(lastDelta) && Math.abs(dy) <= Math.abs(lastDelta) + 1;
    lastDelta = dy;
    if (sinceAction < 400 && decaying) return;
    lastActionTime = now;
    if (dy > 0) next();
    else prev();
  }, { passive: false });

  // Touch swipe (vertical)
  let touchStartY = null;
  let touchMoved = false;
  stack.addEventListener('touchstart', (e) => {
    touchStartY = e.touches[0].clientY;
    touchMoved = false;
  }, { passive: true });
  stack.addEventListener('touchmove', (e) => {
    if (touchStartY === null) return;
    if (Math.abs(e.touches[0].clientY - touchStartY) > 6) touchMoved = true;
  }, { passive: true });
  stack.addEventListener('touchend', (e) => {
    if (touchStartY === null) return;
    const dy = e.changedTouches[0].clientY - touchStartY;
    touchStartY = null;
    if (Math.abs(dy) < 40) return;
    if (dy < 0) next();
    else prev();
  });

  // Click on a peek card — bring to focus
  tabs.forEach((t, i) => {
    t.addEventListener('click', (e) => {
      if (e.target.closest('.case-tab__link')) return;
      if (i !== active) setActive(i);
    });
  });

  // Click on a dot — jump to that index
  if (dotsEl) {
    dotsEl.addEventListener('click', (e) => {
      const d = e.target.closest('.cases__dot');
      if (!d) return;
      setActive(+d.dataset.idx);
    });
    dotsEl.style.pointerEvents = 'auto';
  }

  // Keyboard when stack is hovered/focused
  stack.tabIndex = 0;
  stack.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); next(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); prev(); }
  });

  render();
})();

// Services: inject icons + scroll animation (stagger fade-in, fill vertical connectors, activate numbers)
(function initServicesAnim() {
  const steps = [...document.querySelectorAll('.services__step')];
  if (!steps.length) return;

  // Icon pool — варьируем по индексу внутри колонки
  const ICONS = [
    // chat
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 5h16v11H8l-4 4V5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    // star
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 3l2.8 5.9 6.2.9-4.5 4.4 1.1 6.3L12 17.8 6.4 20.5l1.1-6.3L3 9.8l6.2-.9L12 3z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    // document
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M6 3h8l4 4v14H6V3z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 3v4h4M9 12h6M9 16h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    // scales / gavel
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 3v18M5 21h14M4 8l4-2 4 2M20 8l-4-2-4 2M4 8l2 6h4L4 8zm16 0l-2 6h-4l6-6z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>',
    // check circle
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/><path d="M8 12.5l2.5 2.5L16 9.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
  ];

  document.querySelectorAll('.services__col').forEach(col => {
    const colSteps = col.querySelectorAll('.services__step');
    colSteps.forEach((step, i) => {
      if (step.querySelector('.services__step-icon')) return;
      const icon = document.createElement('div');
      icon.className = 'services__step-icon';
      icon.innerHTML = ICONS[i % ICONS.length];
      step.appendChild(icon);
    });
  });

  // Stagger index для задержки появления
  steps.forEach((step, idx) => {
    const colIndex = [...step.parentElement.children].indexOf(step);
    step.style.transitionDelay = `${colIndex * 90}ms`;
  });

  function checkSteps() {
    const vh = window.innerHeight;
    steps.forEach(step => {
      const rect = step.getBoundingClientRect();
      if (rect.top < vh * 0.88 && rect.bottom > 0) {
        step.classList.add('is-visible');
        step.classList.add('is-active');
        // Когда карточка появилась, рисуем линию от предыдущей цифры к этой
        const prev = step.previousElementSibling;
        if (prev && prev.classList.contains('services__step')) {
          prev.classList.add('is-line-drawn');
        }
      }
    });
  }

  window.addEventListener('scroll', checkSteps, { passive: true });
  window.addEventListener('resize', checkSteps, { passive: true });
  checkSteps();

  // Equalize heights of steps at the same row index across all columns
  const cols = [...document.querySelectorAll('.services__col')];
  function equalizeRows() {
    if (!cols.length) return;
    const rows = Math.max(...cols.map(c => c.querySelectorAll('.services__step').length));
    // reset
    cols.forEach(c => c.querySelectorAll('.services__step').forEach(s => { s.style.minHeight = ''; }));
    if (window.innerWidth < 900) return;
    for (let i = 0; i < rows; i++) {
      let max = 0;
      cols.forEach(c => {
        const s = c.querySelectorAll('.services__step')[i];
        if (s) max = Math.max(max, s.getBoundingClientRect().height);
      });
      cols.forEach(c => {
        const s = c.querySelectorAll('.services__step')[i];
        if (s) s.style.minHeight = max + 'px';
      });
    }
  }
  window.addEventListener('load', equalizeRows);
  window.addEventListener('resize', () => { clearTimeout(window.__eqRows); window.__eqRows = setTimeout(equalizeRows, 100); });
  setTimeout(equalizeRows, 100);
  setTimeout(equalizeRows, 600);
})();

// Cookie consent banner
(function initCookieBanner() {
  const banner = document.getElementById('cookieBanner');
  if (!banner) return;
  const KEY = 'cookie_consent_v1';
  try {
    if (localStorage.getItem(KEY) === 'accepted') return;
  } catch (e) {}
  banner.hidden = false;
  setTimeout(() => banner.classList.add('is-visible'), 600);
  const btn = document.getElementById('cookieAccept');
  if (btn) {
    btn.addEventListener('click', () => {
      try { localStorage.setItem(KEY, 'accepted'); } catch (e) {}
      banner.classList.remove('is-visible');
      setTimeout(() => { banner.hidden = true; }, 350);
    });
  }
})();

// Knowledge base: horizontal infinite carousel with category filters
(function initKnowledge() {
  const track = document.getElementById('knowledgeTrack');
  if (!track) return;

  function loadArticles() {
    return fetch('articles.json', { cache: 'no-cache' })
      .then(r => r.ok ? r.json() : null)
      .then(data => (data && data.articles) ? data.articles : null)
      .catch(() => null)
      .then(list => {
        if (list) return list;
        const tpl = document.getElementById('knowledgeData');
        const dataScript = tpl && tpl.content ? tpl.content.querySelector('script') : null;
        if (!dataScript) return [];
        try { return JSON.parse(dataScript.textContent) || []; } catch (e) { return []; }
      });
  }

  loadArticles().then(articles => {
    if (!articles || !articles.length) return;
    // ОПТИМИЗАЦИЯ: Показываем только последние 20 статей на главной
    // Это предотвращает создание 200+ DOM элементов при публикации
    const displayedArticles = articles.slice(0, 20);
    initSlider(displayedArticles);
  });

  function initSlider(articles) {

  const viewport = track.parentElement;
  const slider = viewport.parentElement;
  const prevBtn = slider.querySelector('.knowledge__arrow--prev');
  const nextBtn = slider.querySelector('.knowledge__arrow--next');
  const chips = [...document.querySelectorAll('.knowledge__chip')];

  // Media icons per tone
  const MEDIA_ICONS = {
    a: '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 3v18M5 21h14M4 9l4-2 4 2M20 9l-4-2-4 2M4 9l2 5h4L4 9zm16 0l-2 5h-4l6-5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>',
    b: '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="4" y="4" width="16" height="16" rx="2" stroke="currentColor" stroke-width="1.6"/><path d="M4 10h16M9 4v16" stroke="currentColor" stroke-width="1.6"/></svg>',
    c: '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 13c2-1 4-1 6 0s4 1 6 0 4-1 4-1M4 17c2-1 4-1 6 0s4 1 6 0 4-1 4-1M8 7l4-3 4 3" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>',
    d: '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/><path d="M12 7v5l3 2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    e: '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1v-9z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    f: '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="8" r="4" stroke="currentColor" stroke-width="1.6"/><path d="M4 21c1.5-5 4.5-7 8-7s6.5 2 8 7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    g: '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M6 4h8l4 4v12H6V4z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 4v4h4M9 13h6M9 17h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    h: '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 2l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1 3-6z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>'
  };

  function buildSeeAllCard() {
    const el = document.createElement('a');
    el.className = 'k-card k-card--all';
    el.href = '/category/all';
    el.innerHTML = `
      <div class="k-card__content k-card--all__content">
        <span class="k-card__tag">Все материалы</span>
        <h3 class="k-card__title">Смотреть все статьи</h3>
        <p class="k-card__desc">Полная подборка экспертных материалов о банкротстве, списании долгов и защите прав.</p>
        <span class="k-card--all__cta">
          <span>Перейти</span>
          <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </span>
      </div>
    `;
    return el;
  }

  function buildCard(a) {
    const el = document.createElement(a.url ? 'a' : 'article');
    el.className = 'k-card';
    el.dataset.cat = a.cat;
    el.dataset.tone = a.tone || 'a';
    if (a.url) { el.href = a.url; el.style.textDecoration = 'none'; el.style.color = 'inherit'; }
    const img = a.img || '';
    el.innerHTML = `
      <div class="k-card__content">
        <span class="k-card__tag">${a.catLabel}</span>
        <h3 class="k-card__title">${a.title}</h3>
        <p class="k-card__desc">${a.desc}</p>
        <div class="k-card__meta">
          <span class="k-card__meta-item">
            <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="3" y="5" width="18" height="16" rx="2" stroke="currentColor" stroke-width="1.6"/><path d="M3 9h18M8 3v4M16 3v4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
            ${a.date}
          </span>
          <span class="k-card__meta-item">
            <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/><path d="M12 7v5l3 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
            ${a.read}
          </span>
        </div>
      </div>
      <div class="k-card__media"${img ? ` style="background-image:url('${img}')"` : ''}>
        ${img ? '' : `<div class="k-card__media-inner">${MEDIA_ICONS[a.tone] || MEDIA_ICONS.a}</div>`}
      </div>
    `;
    return el;
  }

  // Initial filter — respect active chip from HTML (default: 'all')
  const initialChip = chips.find(c => c.classList.contains('is-active'));
  const initialFilter = (initialChip && initialChip.dataset.filter) || 'all';
  let activeCards = initialFilter === 'all'
    ? articles.slice()
    : articles.filter(a => a.cat === initialFilter);
  let index = 0;
  let isTransitioning = false;

  function getVisibleCount() {
    const w = viewport.clientWidth;
    if (w < 720) return 1;
    if (w < 1200) return 2;
    return 3;
  }

  function render() {
    if (!activeCards.length) {
      track.innerHTML = '<div style="padding:40px;color:rgba(255,255,255,0.5);text-align:center;width:100%">Нет статей в этой категории</div>';
      return;
    }

    const N = activeCards.length;
    const isMobile = window.matchMedia('(max-width: 900px)').matches;
    const visible = Math.min(getVisibleCount(), N);

    // ОПТИМИЗАЦИЯ: Используем DocumentFragment для быстрого добавления всех элементов сразу
    // вместо множественного innerHTML. Создаём только 1 копию вместо 3.
    const fragment = document.createDocumentFragment();
    activeCards.forEach(a => fragment.appendChild(buildCard(a)));

    // Очищаем и добавляем всё за одну операцию
    track.innerHTML = '';
    track.appendChild(fragment);

    index = 0;
    if (!isMobile && N > visible) applyTransform(false);
  }

  function getCardStep() {
    const card = track.querySelector('.k-card');
    if (!card) return 0;
    const gap = parseFloat(getComputedStyle(track).gap) || 0;
    return card.getBoundingClientRect().width + gap;
  }

  function applyTransform(animate) {
    const step = getCardStep();
    track.classList.toggle('no-anim', !animate);
    track.style.transform = `translate3d(${-index * step}px, 0, 0)`;
    if (!animate) {
      void track.offsetWidth;
      track.classList.remove('no-anim');
    }
  }

  function move(delta) {
    if (isTransitioning || !activeCards.length) return;
    const N = activeCards.length;
    const visible = Math.min(getVisibleCount(), N);
    if (N <= visible) return;
    isTransitioning = true;
    index += delta;
    // Wrap-around логика: циклируем индекс в пределах [0, N)
    index = ((index % N) + N) % N;
    applyTransform(true);
  }

  track.addEventListener('transitionend', (e) => {
    if (e.target !== track || e.propertyName !== 'transform') return;
    if (!isTransitioning) return;
    isTransitioning = false;
  });

  nextBtn.addEventListener('click', () => move(+1));
  prevBtn.addEventListener('click', () => move(-1));

  // Wheel: horizontal trackpad swipes or shift+wheel move the carousel.
  // Pure vertical wheel is left alone so the page scrolls normally.
  let wheelCooldown = 0;
  viewport.addEventListener('wheel', (e) => {
    if (window.matchMedia('(max-width: 900px)').matches) return;
    const absX = Math.abs(e.deltaX);
    const absY = Math.abs(e.deltaY);
    const horizontalIntent = absX > absY || e.shiftKey;
    if (!horizontalIntent) return;
    e.preventDefault();
    const now = performance.now();
    if (now - wheelCooldown < 350) return;
    const delta = e.shiftKey ? e.deltaY : e.deltaX;
    if (Math.abs(delta) < 4) return;
    wheelCooldown = now;
    move(delta > 0 ? +1 : -1);
  }, { passive: false });

  // Keyboard
  slider.tabIndex = 0;
  slider.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight') { e.preventDefault(); move(+1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); move(-1); }
  });

  // Touch swipe
  let touchX = null;
  viewport.addEventListener('touchstart', (e) => { touchX = e.touches[0].clientX; }, { passive: true });
  viewport.addEventListener('touchend', (e) => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(dx) < 40) return;
    move(dx < 0 ? +1 : -1);
  });

  // Filter chips
  chips.forEach(chip => {
    chip.addEventListener('click', () => {
      chips.forEach(c => c.classList.remove('is-active'));
      chip.classList.add('is-active');
      const f = chip.dataset.filter;
      activeCards = f === 'all' ? articles.slice() : articles.filter(a => a.cat === f);
      render();
    });
  });

  // Resize — only react to width changes (ignore mobile address bar show/hide which fires height-only resizes)
  let resizeTimer = null;
  let lastWidth = window.innerWidth;
  window.addEventListener('resize', () => {
    if (window.innerWidth === lastWidth) return;
    lastWidth = window.innerWidth;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      render();
    }, 150);
  });

  render();
  } // end initSlider
})();

/* ============ ЛИД-ФОРМА ============ */
(function () {
  const form = document.getElementById('leadForm');
  if (!form) return;
  const nameEl = document.getElementById('leadName');
  const phoneEl = document.getElementById('leadPhone');
  const consentEl = document.getElementById('leadConsent');
  const submitEl = document.getElementById('leadSubmit');
  const statusEl = document.getElementById('leadStatus');

  const MASK_EMPTY = '+7 (___) ___-__-__';

  const formatPhone = (digits) => {
    // digits: только те цифры, что ввел юзер (до 10)
    const d = digits.slice(0, 10);
    let out = '+7 (';
    out += (d.slice(0, 3) + '___').slice(0, 3);
    out += ') ';
    out += (d.slice(3, 6) + '___').slice(0, 3);
    out += '-';
    out += (d.slice(6, 8) + '__').slice(0, 2);
    out += '-';
    out += (d.slice(8, 10) + '__').slice(0, 2);
    return out;
  };

  const extractDigits = (value) => {
    let s = value.replace(/\D/g, '');
    if (s.startsWith('8')) s = '7' + s.slice(1);
    if (s.startsWith('7')) s = s.slice(1);
    return s.slice(0, 10);
  };

  const getPhoneDigits = () => extractDigits(phoneEl.value);

  const showMask = () => {
    const d = getPhoneDigits();
    phoneEl.value = formatPhone(d);
    // Поставить курсор после последней введённой цифры
    const pos = caretPosAfter(d.length);
    requestAnimationFrame(() => phoneEl.setSelectionRange(pos, pos));
  };

  const caretPosAfter = (n) => {
    // Позиции цифр в маске "+7 (XXX) XXX-XX-XX"
    const map = [4, 5, 6, 9, 10, 11, 13, 14, 16, 17];
    if (n <= 0) return 4;
    if (n >= 10) return 18;
    return map[n - 1] + 1;
  };

  const validate = () => {
    return nameEl.value.trim().length > 0
      && getPhoneDigits().length === 10
      && consentEl.checked;
  };

  const getError = () => {
    if (nameEl.value.trim().length === 0) return 'Введите имя';
    if (getPhoneDigits().length !== 10) return 'Введите корректный номер телефона';
    if (!consentEl.checked) return 'Подтвердите согласие на обработку данных';
    return '';
  };

  const clearError = () => {
    if (statusEl.classList.contains('is-error')) {
      statusEl.textContent = '';
      statusEl.className = 'contacts__status';
    }
  };

  phoneEl.addEventListener('focus', () => {
    if (!phoneEl.value) phoneEl.value = MASK_EMPTY;
    requestAnimationFrame(() => {
      const pos = caretPosAfter(getPhoneDigits().length);
      phoneEl.setSelectionRange(pos, pos);
    });
  });
  phoneEl.addEventListener('blur', () => {
    if (getPhoneDigits().length === 0) phoneEl.value = '';
    clearError();
  });
  phoneEl.addEventListener('input', () => {
    showMask();
    clearError();
  });
  phoneEl.addEventListener('keydown', (e) => {
    if (e.key === 'Backspace') {
      e.preventDefault();
      const d = getPhoneDigits();
      phoneEl.value = formatPhone(d.slice(0, -1));
      const pos = caretPosAfter(Math.max(0, d.length - 1));
      phoneEl.setSelectionRange(pos, pos);
      clearError();
    }
  });

  nameEl.addEventListener('input', clearError);
  consentEl.addEventListener('change', clearError);

  const hpEl = document.getElementById('leadHp');
  const API_URL = (window.__API_BASE || '') + '/api/lead';

  async function sendLead() {
    const payload = {
      name: nameEl.value.trim(),
      phone: phoneEl.value.trim(),
      consent: !!consentEl.checked,
      source: window.__leadSource || 'Не указан',
      page_url: location.href,
      page_title: document.title || '',
      _hp: hpEl ? hpEl.value : ''
    };
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 20000);
    let res;
    try {
      res = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        credentials: 'omit',
        signal: ctrl.signal
      });
    } catch (err) {
      clearTimeout(timer);
      if (err.name === 'AbortError') {
        throw new Error('Сервер не отвечает. Позвоните нам или попробуйте позже.');
      }
      throw err;
    }
    clearTimeout(timer);
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      const msg = (data && (data.detail || data.message)) || 'Не удалось отправить заявку. Попробуйте позже.';
      throw new Error(typeof msg === 'string' ? msg : 'Ошибка отправки');
    }
    return data;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!validate()) {
      statusEl.textContent = getError();
      statusEl.className = 'contacts__status is-error';
      return;
    }
    // loading state
    submitEl.disabled = true;
    const prevLabel = submitEl.innerHTML;
    submitEl.innerHTML = '<span>ОТПРАВКА...</span>';
    statusEl.textContent = '';
    statusEl.className = 'contacts__status';
    try {
      await sendLead();
      statusEl.textContent = 'Спасибо! Мы свяжемся с вами в течение 15 минут.';
      statusEl.className = 'contacts__status is-success';
      form.reset();
      // автозакрытие модалки через 3 сек
      setTimeout(() => {
        if (window.__contactsModal && window.__contactsModal.close) {
          window.__contactsModal.close();
        }
        statusEl.textContent = '';
        statusEl.className = 'contacts__status';
      }, 3000);
    } catch (err) {
      statusEl.textContent = err.message || 'Не удалось отправить заявку. Позвоните нам или попробуйте позже.';
      statusEl.className = 'contacts__status is-error';
    } finally {
      submitEl.disabled = false;
      submitEl.innerHTML = prevLabel;
    }
  });
})();

/* ============ МОДАЛКА ============ */
(function () {
  const modal = document.getElementById('contactsModal');
  if (!modal) return;
  const triggers = document.querySelectorAll('[data-open-modal="contactsModal"]');
  const closers = modal.querySelectorAll('[data-close-modal]');

  const open = () => {
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('is-modal-open');
    setTimeout(() => document.getElementById('leadName')?.focus(), 200);
  };
  const close = () => {
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('is-modal-open');
  };

  triggers.forEach(t => t.addEventListener('click', (e) => {
    e.preventDefault();
    // запоминаем, с какой кнопки открыли модалку, чтобы в письме указать источник
    window.__leadSource = t.getAttribute('data-source') || 'Не указан';
    open();
  }));
  closers.forEach(c => c.addEventListener('click', close));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal.classList.contains('is-open')) close();
  });

  window.__contactsModal = { open, close };
})();

/* ============ LAZY VIDEO (services section) ============ */
(function () {
  const v = document.getElementById('servicesVideo');
  if (!v || !v.dataset.src) return;

  let loaded = false;
  const load = () => {
    if (loaded) return;
    loaded = true;
    v.src = v.dataset.src;
    v.load();
    v.play().catch(() => { /* ignore autoplay block */ });
  };

  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          load();
          io.disconnect();
        }
      });
    }, { rootMargin: '200px 0px' });
    io.observe(v);
  } else {
    load();
  }
})();
